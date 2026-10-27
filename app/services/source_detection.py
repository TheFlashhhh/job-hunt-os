"""Careers URL → which structured source it is, and how sure we are.

Used by Sources → Add Company and by ``python -m app.cli company add``. The
person types a company name and a careers URL; nobody should need to know what
a Greenhouse board token or a Workday pod is.

Three confidence states, and only one of them can lead to monitoring:

``CONFIRMED``      The URL itself names a supported board (a Greenhouse,
                   Lever, Ashby, SmartRecruiters or Workday URL), and one cheap
                   request to that board answered. Safe to offer "Monitor".
``LIKELY_REVIEW``  The URL is a company page, and the page links to exactly one
                   supported board. Probably right — but a careers page can link
                   to a partner's or a subsidiary's board, so it is shown for a
                   human to confirm by opening the board itself, never activated.
``UNKNOWN``        No supported board could be identified. Kept as MANUAL_ONLY
                   if the person wants the employer tracked.

Nothing here guesses a board from a company name, and nothing enumerates ATS
namespaces. The only boards ever probed are ones a URL named.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.connectors import get_connector
from app.connectors.base import SourceConfig
from app.connectors.workday import parse_workday_url
from app.models.enums import SourceVerification

#: Board URL shapes → (provider, identifier). Order matters only for clarity;
#: the hosts do not overlap.
_BOARD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GREENHOUSE", re.compile(r"^(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("GREENHOUSE", re.compile(r"^boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("LEVER", re.compile(r"^jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("LEVER", re.compile(r"^api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)", re.I)),
    ("ASHBY", re.compile(r"^jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("ASHBY", re.compile(r"^api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", re.I)),
    ("SMARTRECRUITERS", re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("SMARTRECRUITERS", re.compile(r"^api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)", re.I)),
]

#: The same shapes, findable inside a page's HTML.
_EMBEDDED = re.compile(
    r"(?:https?:)?//("
    r"(?:boards|job-boards)\.greenhouse\.io/[A-Za-z0-9_-]+"
    r"|boards\.greenhouse\.io/embed/job_board\?for=[A-Za-z0-9_-]+"
    r"|jobs\.lever\.co/[A-Za-z0-9_.-]+"
    r"|jobs\.ashbyhq\.com/[A-Za-z0-9_.%-]+"
    r"|(?:jobs|careers)\.smartrecruiters\.com/[A-Za-z0-9_-]+"
    r"|[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+"
    r"|wd\d+\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
    r")",
    re.I,
)

#: Path words that are a board's own furniture, not a board token.
_NOT_A_TOKEN = {"embed", "v1", "jobs", "job", "api", "careers", "search"}


@dataclass
class Detection:
    """What a careers URL resolves to."""

    confidence: str
    provider: str | None = None
    identifier: str | None = None
    board_url: str | None = None
    #: How it was found: URL_SHAPE, PAGE_LINK or NONE.
    method: str = "NONE"
    detail: str = ""
    postings_visible: int | None = None
    #: Other boards a page linked to, when it linked to more than one.
    alternatives: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.confidence == SourceVerification.CONFIRMED.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "provider": self.provider,
            "identifier": self.identifier,
            "board_url": self.board_url,
            "method": self.method,
            "detail": self.detail,
            "postings_visible": self.postings_visible,
            "alternatives": self.alternatives,
        }


def board_from_url(url: str | None) -> tuple[str, str] | None:
    """(provider, identifier) when the URL itself is a supported board. No network."""
    if not url:
        return None
    text = url.strip()
    workday = parse_workday_url(text)
    if workday is not None:
        return "WORKDAY", workday[0].identifier
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    location = f"{(parts.hostname or '').lower()}{parts.path}"
    if parts.query:
        location += f"?{parts.query}"
    for provider, pattern in _BOARD_PATTERNS:
        match = pattern.match(location)
        if match and match.group(1).lower() not in _NOT_A_TOKEN:
            return provider, match.group(1)
    return None


def detect_source(
    careers_url: str,
    *,
    company_name: str = "",
    connector_factory: Any = get_connector,
    page_fetcher: Any = None,
) -> Detection:
    """Resolve a careers URL. At most one board request and one page fetch."""
    direct = board_from_url(careers_url)
    if direct is not None:
        provider, identifier = direct
        return _confirm(provider, identifier, company_name, connector_factory, method="URL_SHAPE")

    page = _fetch_page(careers_url, page_fetcher)
    if page is None:
        return Detection(
            confidence=SourceVerification.UNKNOWN.value,
            detail=(
                "This is not a job-board URL Job Hunt OS recognises, and the page could not "
                "be read. It can be kept as manual-only."
            ),
        )
    boards: list[tuple[str, str]] = []
    for match in _EMBEDDED.finditer(page):
        found = board_from_url(match.group(1))
        if found is not None and found not in boards:
            boards.append(found)
    if not boards:
        return Detection(
            confidence=SourceVerification.UNKNOWN.value,
            detail=(
                "This career site is not currently supported for automatic monitoring: no "
                "Greenhouse, Lever, Ashby, SmartRecruiters or Workday board is linked from it."
            ),
        )
    provider, identifier = boards[0]
    checked = _confirm(provider, identifier, company_name, connector_factory, method="PAGE_LINK")
    # A page link is a strong hint, not proof of which employer the board is.
    if checked.confirmed:
        checked.confidence = SourceVerification.LIKELY_REVIEW.value
        checked.detail = (
            f"The careers page links to a {provider.title()} board ({identifier}), which "
            "answered. Open the board to confirm it is this employer's own, then add it by "
            "its board URL."
        )
    checked.alternatives = [f"{p}:{i}" for p, i in boards[1:]]
    return checked


def _confirm(
    provider: str, identifier: str, company_name: str, connector_factory: Any, *, method: str
) -> Detection:
    config = SourceConfig(
        company_name=company_name or identifier, ats_type=provider, ats_identifier=identifier
    )
    connector = connector_factory(provider)
    try:
        validation = connector.validate_config(config)
        if not validation.ok:
            return Detection(
                confidence=SourceVerification.UNKNOWN.value,
                provider=provider,
                identifier=identifier,
                method=method,
                detail="; ".join(validation.problems),
            )
        health = connector.health_check(config)
    finally:
        connector.close()
    board_url = _board_url(provider, identifier)
    if not health.reachable:
        return Detection(
            confidence=SourceVerification.UNKNOWN.value,
            provider=provider,
            identifier=identifier,
            board_url=board_url,
            method=method,
            detail=f"{provider.title()} did not answer for this board: {health.detail}",
        )
    return Detection(
        confidence=SourceVerification.CONFIRMED.value,
        provider=provider,
        identifier=identifier,
        board_url=board_url,
        method=method,
        detail=f"{provider.title()} board answered. {health.detail}".strip(),
        postings_visible=health.postings_visible,
    )


def _board_url(provider: str, identifier: str) -> str:
    return {
        "GREENHOUSE": f"https://job-boards.greenhouse.io/{identifier}",
        "LEVER": f"https://jobs.lever.co/{identifier}",
        "ASHBY": f"https://jobs.ashbyhq.com/{identifier}",
        "SMARTRECRUITERS": f"https://jobs.smartrecruiters.com/{identifier}",
        "WORKDAY": f"https://{identifier}",
    }.get(provider, identifier)


def _fetch_page(url: str, page_fetcher: Any) -> str | None:
    """The careers page's HTML, through the same SSRF-guarded fetcher import uses."""
    from app.services.url_fetch import UrlFetchError, fetch_url

    try:
        page = (page_fetcher or fetch_url)(url)
    except UrlFetchError:
        return None
    text = getattr(page, "text", page)
    return text if isinstance(text, str) else None
