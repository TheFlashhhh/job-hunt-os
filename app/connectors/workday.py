"""Workday career-site connector.

Workday publishes no documented public job API. Every Workday career site,
though, is a JavaScript page that reads its listings from a public JSON
endpoint on the same host — the one a browser on that page calls, with no
login, cookie or account:

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs          list (20 per page)
    GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath} one posting

That is what this connector reads, and nothing else. It is recorded as
``UNDOCUMENTED_PUBLIC_ENDPOINT`` because that is what it is: public, stable in
practice across every tenant verified on 2026-10-16, and not a contract.

**Two hosts, one endpoint.** A site lives either at
``{tenant}.wd{N}.myworkdayjobs.com/{site}`` or at
``wd{N}.myworkdaysite.com/recruiting/{tenant}/{site}``. The pod number matters:
the same tenant on the wrong pod answers 422. So a board is configured by the
careers URL it actually lives at, and its identity is that URL's host and path —
``capitalone.wd12.myworkdayjobs.com/Capital_One`` — never a tenant name alone.

**Behaviour verified against live boards, not assumed:**

* the list endpoint caps ``limit`` at 20, and only the FIRST page carries the
  real ``total`` — later pages report 0;
* a wrong site answers 404; a wrong tenant or pod answers 422. Both mean "this
  board does not exist", not "the source is down";
* list entries carry title, ``externalPath``, a location summary and a relative
  "Posted 3 Days Ago"; the body, requisition id, real location, time type and
  posting date need the per-posting request.

**Cost control.** A large Workday employer lists 1,000–2,000 postings worldwide.
Two bounds keep that polite:

* when the site offers a country facet with a United States value, the listing
  is narrowed to it — the search is for US roles, and this is a filter the site
  itself offers, not a judgement about relevance. Sites without one are listed
  in full.
* bodies are fetched only for postings the collector does not already hold
  (``reuses_known_postings``), up to ``max_detail_fetches`` per run. A known
  posting that is still listed is reported as observed — still open — without
  a request. New postings over the bound wait for the next run.

Disappearance is judged only from a complete listing: if the page bound cut the
walk short, no posting is aged out that run.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from app.connectors.base import (
    HealthResult,
    JobSourceConnector,
    RateLimitMetadata,
    SourceConfig,
    ValidationResult,
)
from app.connectors.http import ConnectorHTTPError, SourceNotFound, SourceUnavailable
from app.models.base import utcnow
from app.models.enums import SourceCapability
from app.services.pipeline import RawJobInput

#: The list endpoint refuses anything larger.
PAGE_SIZE = 20
#: 100 pages = 2,000 postings, the most any verified tenant listed.
DEFAULT_MAX_PAGES = 100
#: Bodies fetched per company per run. Steady state needs a handful (new
#: postings since the last poll); only a first poll of a big board hits this.
DEFAULT_MAX_DETAIL_FETCHES = 100

_JOBS_HOST = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com$", re.I)
_SITE_HOST = re.compile(r"^(?P<pod>wd\d+)\.myworkdaysite\.com$", re.I)
_LOCALE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.I)
_US_NAMES = {"united states", "united states of america", "usa", "us"}


@dataclass(frozen=True)
class WorkdayBoard:
    """One Workday career site, located exactly."""

    host: str
    tenant: str
    site: str

    @property
    def on_site_host(self) -> bool:
        return bool(_SITE_HOST.match(self.host))

    @property
    def identifier(self) -> str:
        """The board identity stored as ``ats_identifier``."""
        if self.on_site_host:
            return f"{self.host}/recruiting/{self.tenant}/{self.site}"
        return f"{self.host}/{self.site}"

    @property
    def board_url(self) -> str:
        return f"https://{self.identifier}"

    @property
    def api_base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    def posting_url(self, external_path: str) -> str:
        return f"{self.board_url}{external_path}"


def parse_workday_url(url: str | None) -> tuple[WorkdayBoard, str | None] | None:
    """A careers or posting URL → (board, posting path or None). No network.

    Returns None for anything that is not unambiguously a Workday career site.
    A company-branded domain that merely links to Workday is NOT resolved here:
    its tenant and site cannot be read from its URL, and guessing them would be
    inventing a board.
    """
    if not url:
        return None
    text = url.strip()
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]

    # The JSON endpoint itself: /wday/cxs/{tenant}/{site}/...
    if len(segments) >= 4 and segments[0] == "wday" and segments[1] == "cxs":
        if not (_JOBS_HOST.match(host) or _SITE_HOST.match(host)):
            return None
        tenant, site, rest = segments[2], segments[3], segments[4:]
        path = "/" + "/".join(rest) if rest and rest[0] == "job" else None
        return WorkdayBoard(host, tenant, site), path

    if segments and _LOCALE.match(segments[0]):
        segments = segments[1:]

    jobs_host = _JOBS_HOST.match(host)
    if jobs_host:
        if not segments:
            return None
        tenant, site, rest = jobs_host.group("tenant").lower(), segments[0], segments[1:]
    elif _SITE_HOST.match(host):
        if len(segments) < 3 or segments[0] != "recruiting":
            return None
        tenant, site, rest = segments[1], segments[2], segments[3:]
    else:
        return None

    if site.lower() in {"wday", "job", "details"}:
        return None
    path = None
    if rest and rest[0] == "job" and len(rest) >= 2:
        path = "/" + "/".join(rest)
    return WorkdayBoard(host, tenant, site), path


def parse_board_identifier(identifier: str | None) -> WorkdayBoard | None:
    parsed = parse_workday_url(identifier)
    return parsed[0] if parsed else None


def posting_id(external_path: str | None) -> str | None:
    """The stable id in a posting path: its last segment.

    ``/job/McLean-VA/Data-Scientist_R123`` → ``Data-Scientist_R123``. It equals
    the posting's own ``jobPostingId`` and is what canonical resolution reads
    from a pasted Workday URL, so a collected and a pasted copy of one posting
    carry the same identity.
    """
    if not external_path:
        return None
    last = external_path.rstrip("/").rsplit("/", 1)[-1]
    return last.split("?", 1)[0] or None


class WorkdayConnector(JobSourceConnector):
    connector_id = "workday"
    source_type = "WORKDAY"
    capability = SourceCapability.UNDOCUMENTED_PUBLIC_ENDPOINT
    version = "0.1.0"
    docs_url = ""
    reuses_known_postings = True

    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=False,
            min_interval_seconds=0.5,
            notes=(
                "Workday publishes no limit for its career-site endpoint. Pages of "
                "20, bodies only for unseen postings, bounded per run, spaced 0.5s."
            ),
        )

    # -- configuration ---------------------------------------------------
    def validate_config(self, config: SourceConfig) -> ValidationResult:
        result = super().validate_config(config)
        if not result.ok:
            return result
        if parse_board_identifier(config.ats_identifier) is None:
            return ValidationResult.invalid(
                f"{config.ats_identifier!r} is not a Workday career site "
                "(expected {tenant}.wdN.myworkdayjobs.com/{site} or "
                "wdN.myworkdaysite.com/recruiting/{tenant}/{site})."
            )
        for key in ("max_detail_fetches", "max_pages"):
            value = config.connector_config.get(key)
            if value is not None and (not isinstance(value, int) or value < 0):
                return ValidationResult.invalid(f"{key} must be a non-negative integer.")
        return result

    def _board(self, config: SourceConfig) -> WorkdayBoard:
        board = parse_board_identifier(config.ats_identifier)
        if board is None:  # validate_config runs first; this is a guard
            raise SourceNotFound("Not a Workday career site.", url=config.ats_identifier)
        return board

    # -- requests --------------------------------------------------------
    def _list_page(
        self, board: WorkdayBoard, *, offset: int, facets: dict[str, list[str]], limit: int = PAGE_SIZE
    ) -> dict[str, Any]:
        url = f"{board.api_base}/jobs"
        try:
            payload = self.client.post_json(
                url,
                body={"appliedFacets": facets, "limit": limit, "offset": offset, "searchText": ""},
            )
        except SourceUnavailable as exc:
            # Workday answers a wrong tenant or pod with 422. That is "no such
            # board" — a configuration fact — not an outage worth retrying.
            if exc.status == 422:
                raise SourceNotFound(
                    "Workday does not recognise this tenant/site (HTTP 422).",
                    url=url,
                    status=422,
                ) from exc
            raise
        return payload if isinstance(payload, dict) else {}

    def _detail(self, board: WorkdayBoard, external_path: str) -> dict[str, Any] | None:
        payload = self.client.get_json(f"{board.api_base}{external_path}")
        if not isinstance(payload, dict):
            return None
        info = payload.get("jobPostingInfo")
        return info if isinstance(info, dict) else None

    # -- health ----------------------------------------------------------
    def health_check(self, config: SourceConfig) -> HealthResult:
        """One list request. No bodies."""
        validation = self.validate_config(config)
        if not validation.ok:
            return HealthResult(
                reachable=False,
                status="INVALID_CONFIG",
                detail="; ".join(validation.problems),
                checked_at=utcnow(),
            )
        board = self._board(config)
        try:
            first = self._list_page(board, offset=0, facets={}, limit=1)
        except ConnectorHTTPError as exc:
            return HealthResult(
                reachable=False,
                capability_failure=isinstance(exc, SourceNotFound),
                status=type(exc).__name__,
                detail=exc.summary,
                checked_at=utcnow(),
            )
        total = first.get("total")
        us = _us_facet(first.get("facets"))
        return HealthResult(
            reachable=True,
            status="OK",
            detail=(
                f"{total if isinstance(total, int) else '?'} posting(s) listed"
                + (f"; US filter available ({us[0]})." if us else "; no country filter offered.")
            ),
            postings_visible=total if isinstance(total, int) else None,
            checked_at=utcnow(),
        )

    # -- collection ------------------------------------------------------
    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        board = self._board(config)
        max_pages = int(config.connector_config.get("max_pages", DEFAULT_MAX_PAGES))
        max_detail = int(
            config.connector_config.get("max_detail_fetches", DEFAULT_MAX_DETAIL_FETCHES)
        )

        first = self._list_page(board, offset=0, facets={})
        facets: dict[str, list[str]] = {}
        us = _us_facet(first.get("facets"))
        if us is not None and config.connector_config.get("us_only", True):
            facets = {us[0]: [us[1]]}
            first = self._list_page(board, offset=0, facets=facets)

        total = first.get("total")
        total = total if isinstance(total, int) and total >= 0 else None
        summaries = _postings(first)
        offset = PAGE_SIZE
        pages = 1
        while summaries and (total is None or offset < total):
            if pages >= max_pages:
                self._listing_complete = False
                break
            page = _postings(self._list_page(board, offset=offset, facets=facets))
            pages += 1
            if not page:
                break
            summaries.extend(page)
            offset += PAGE_SIZE
        if total is not None and len(summaries) < total and self._listing_complete:
            # The board said N and handed over fewer. Treat the gap as unread.
            self._listing_complete = False

        out: list[dict[str, Any]] = []
        fetched = 0
        seen: set[str] = set()
        for summary in summaries:
            path = summary.get("externalPath")
            pid = posting_id(path if isinstance(path, str) else None)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            if pid in self._known_external_ids or fetched >= max_detail:
                # Still listed, so still open. Known: nothing to re-read.
                # Unknown past the bound: next run's work.
                self._observed_external_ids.add(pid)
                continue
            try:
                info = self._detail(board, str(path))
            except ConnectorHTTPError:
                # One unreadable posting must not fail the company, and a
                # listed posting is open whether or not its body loaded.
                self._observed_external_ids.add(pid)
                continue
            fetched += 1
            if info is None:
                self._observed_external_ids.add(pid)
                continue
            out.append(
                {
                    **summary,
                    "_posting_id": pid,
                    "_detail": info,
                    "_board": board.identifier,
                    "_us_filter": facets or None,
                }
            )
        return out

    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        detail = raw.get("_detail")
        info: dict[str, Any] = detail if isinstance(detail, dict) else {}
        title = str(info.get("title") or raw.get("title") or "").strip()
        if not title:
            return None
        board = parse_board_identifier(str(raw.get("_board") or config.ats_identifier))
        path = raw.get("externalPath")
        external_url = info.get("externalUrl")
        if not (isinstance(external_url, str) and external_url.startswith("http")):
            external_url = board.posting_url(str(path)) if board and isinstance(path, str) else None

        return self._base_input(
            config,
            title=title,
            description=_strip_html(info.get("jobDescription")).strip() or title,
            location=_location(info, raw),
            url=external_url,
            # The path-derived id, not jobPostingId: it is what the listing
            # offers, so it is what "already known" is checked against.
            external_id=str(raw.get("_posting_id") or info.get("jobPostingId") or "") or None,
            posted_date=_iso_date(info.get("startDate")),
            raw_payload=raw,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _postings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("jobPostings")
    return [p for p in items if isinstance(p, dict)] if isinstance(items, list) else []


def _us_facet(facets: Any) -> tuple[str, str] | None:
    """(facetParameter, value id) for a country facet's United States value.

    Tenants name the facet differently — ``locationCountry``,
    ``Location_Country``, ``locationHierarchy1``, a custom field — and nest it
    differently, so it is found by its VALUE, not its name: a facet one of whose
    values is the United States. Location facets listing cities ("Austin, Texas,
    United States of America") do not match; only the country itself does.
    """
    if not isinstance(facets, list):
        return None
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        values = facet.get("values")
        if not isinstance(values, list) or not values:
            continue
        if isinstance(values[0], dict) and "values" in values[0]:
            nested = _us_facet(values)
            if nested:
                return nested
            continue
        parameter = facet.get("facetParameter")
        for value in values:
            if not isinstance(value, dict):
                continue
            name = str(value.get("descriptor") or "").strip().lower()
            if name in _US_NAMES and value.get("id") and parameter:
                return str(parameter), str(value["id"])
    return None


def _location(info: dict[str, Any], raw: dict[str, Any]) -> str | None:
    parts: list[str] = []
    primary = info.get("location") or raw.get("locationsText")
    if isinstance(primary, str) and primary.strip():
        parts.append(primary.strip())
    extra = info.get("additionalLocations")
    if isinstance(extra, list):
        parts.extend(str(x).strip() for x in extra[:3] if str(x).strip())
    text = "; ".join(parts)
    # Like Lever's workplaceType: folded into the location string so the
    # existing remote classifier reads it without a Workday branch.
    remote = info.get("remoteType")
    if isinstance(remote, str) and remote.strip():
        text = f"{text} ({remote.strip()})".strip()
    return text or None


def _iso_date(value: Any) -> date | None:
    """Workday's ``startDate`` is the date the posting went up."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|ul|ol)>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)
