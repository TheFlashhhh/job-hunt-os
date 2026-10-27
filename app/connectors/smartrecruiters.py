"""SmartRecruiters connector — per-company capability validation required.

    https://api.smartrecruiters.com/v1/companies/{company_id}/postings

The architecture already established that SmartRecruiters public access is
**not universal**: availability depends on the employer's plan and settings.
That single fact shapes this whole connector.

A company whose feed is not publicly exposed answers 403 or 404. That is not an
outage and not a bug — it is a *capability fact about that company*, and
treating it as a run failure would be actively misleading: a watchlist of fifty
companies would show a red collection run every night because three of them are
on the wrong plan. So this connector reports capability failures separately,
records them against the company, and lets the rest of the run succeed.

The list endpoint returns summaries. Full posting bodies need one detail request
each, which is why ``max_detail_fetches`` is bounded — an unbounded expansion
across a large board is exactly the impolite traffic pattern the HTTP client
exists to prevent.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

from app.connectors.base import (
    HealthResult,
    JobSourceConnector,
    RateLimitMetadata,
    SourceConfig,
    ValidationResult,
)
from app.connectors.http import ConnectorHTTPError, SourceForbidden, SourceNotFound
from app.models.base import utcnow
from app.models.enums import SourceCapability
from app.services.pipeline import RawJobInput

POSTINGS_API = "https://api.smartrecruiters.com/v1/companies"

#: Postings per list page (the API's documented maximum).
PAGE_SIZE = 100
#: Bound on detail requests per company per run. A board with 400 open roles
#: would otherwise mean 400 extra requests every poll.
DEFAULT_MAX_DETAIL_FETCHES = 60


class SmartRecruitersConnector(JobSourceConnector):
    connector_id = "smartrecruiters"
    source_type = "SMARTRECRUITERS"
    #: Documented, but tier-gated per company — hence validation before use.
    capability = SourceCapability.DOCUMENTED_PUBLIC_API
    version = "0.1.0"
    docs_url = "https://developers.smartrecruiters.com/reference/postings-1"

    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=False,
            min_interval_seconds=0.75,
            notes=(
                "SmartRecruiters publishes no explicit public limit. This connector "
                "issues one list request plus bounded detail requests, spaced more "
                "conservatively than the other ATS connectors because of that fan-out."
            ),
        )

    def validate_config(self, config: SourceConfig) -> ValidationResult:
        result = super().validate_config(config)
        if not result.ok:
            return result
        limit = config.connector_config.get("max_detail_fetches", DEFAULT_MAX_DETAIL_FETCHES)
        if not isinstance(limit, int) or limit < 0:
            return ValidationResult.invalid("max_detail_fetches must be a non-negative integer.")
        return result

    # -- capability ------------------------------------------------------
    def check_public_availability(self, config: SourceConfig) -> HealthResult:
        """Confirm this specific company's feed is publicly readable.

        Separated from ``health_check`` so the collection path can call it
        before spending any detail requests.
        """
        validation = self.validate_config(config)
        if not validation.ok:
            return HealthResult(
                reachable=False,
                status="INVALID_CONFIG",
                detail="; ".join(validation.problems),
                checked_at=utcnow(),
            )
        url = f"{POSTINGS_API}/{config.ats_identifier}/postings"
        try:
            payload = self.client.get_json(url, params={"limit": 1, "offset": 0})
        except (SourceForbidden, SourceNotFound) as exc:
            return HealthResult(
                reachable=False,
                capability_failure=True,
                status="PUBLIC_FEED_UNAVAILABLE",
                detail=(
                    f"{config.company_name} does not expose a public SmartRecruiters "
                    f"feed ({exc}). This is a per-company plan setting, not an outage."
                ),
                checked_at=utcnow(),
            )
        except ConnectorHTTPError as exc:
            return HealthResult(
                reachable=False,
                status=type(exc).__name__,
                detail=exc.summary,
                checked_at=utcnow(),
            )

        total = payload.get("totalFound") if isinstance(payload, dict) else None
        return HealthResult(
            reachable=True,
            status="OK",
            detail=f"Public feed available. {total if total is not None else '?'} posting(s).",
            postings_visible=int(total) if isinstance(total, int) else None,
            checked_at=utcnow(),
        )

    def health_check(self, config: SourceConfig) -> HealthResult:
        return self.check_public_availability(config)

    # -- collection ------------------------------------------------------
    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        url = f"{POSTINGS_API}/{config.ats_identifier}/postings"
        summaries: list[dict[str, Any]] = []
        offset = 0
        # Bounded page walk: a source that ignores offset cannot loop forever.
        for _ in range(20):
            payload = self.client.get_json(url, params={"limit": PAGE_SIZE, "offset": offset})
            if not isinstance(payload, dict):
                break
            content = payload.get("content")
            if not isinstance(content, list) or not content:
                break
            summaries.extend(item for item in content if isinstance(item, dict))
            if len(content) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        max_detail = config.connector_config.get("max_detail_fetches", DEFAULT_MAX_DETAIL_FETCHES)
        return self._expand(summaries, config, max_detail=int(max_detail))

    def _expand(
        self, summaries: list[dict[str, Any]], config: SourceConfig, *, max_detail: int
    ) -> list[dict[str, Any]]:
        """Attach full posting bodies, up to the configured bound.

        Summaries beyond the bound are still returned — a job discoverable by
        title with a thin body is better than a job not discovered at all. The
        record records that its body was not fetched.
        """
        out: list[dict[str, Any]] = []
        fetched = 0
        for summary in summaries:
            posting_id = summary.get("id")
            if not posting_id or fetched >= max_detail:
                summary["_detail_fetched"] = False
                out.append(summary)
                continue
            url = f"{POSTINGS_API}/{config.ats_identifier}/postings/{posting_id}"
            try:
                detail = self.client.get_json(url)
            except ConnectorHTTPError:
                # One unreadable posting must not fail the company.
                summary["_detail_fetched"] = False
                out.append(summary)
                continue
            fetched += 1
            if isinstance(detail, dict):
                merged = {**summary, **detail, "_detail_fetched": True}
                out.append(merged)
            else:
                summary["_detail_fetched"] = False
                out.append(summary)
        return out

    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        title = str(raw.get("name") or "").strip()
        if not title:
            return None

        return self._base_input(
            config,
            title=title,
            description=_description(raw) or title,
            location=_location(raw),
            url=_url(raw, config),
            external_id=str(raw.get("id")) if raw.get("id") else None,
            posted_date=_parse_date(raw.get("releasedDate") or raw.get("createdOn")),
            raw_payload=raw,
        )


def _location(raw: dict[str, Any]) -> str | None:
    location = raw.get("location")
    if not isinstance(location, dict):
        return None
    parts = [
        str(location.get(key)).strip() for key in ("city", "region", "country") if location.get(key)
    ]
    text = ", ".join(parts)
    if location.get("remote") is True:
        text = f"{text} (Remote)".strip()
    return text or None


def _description(raw: dict[str, Any]) -> str:
    """Reassemble the ad from SmartRecruiters' section structure."""
    ad = raw.get("jobAd")
    if not isinstance(ad, dict):
        return _strip_html(raw.get("jobDescription"))

    sections = ad.get("sections")
    if not isinstance(sections, dict):
        return _strip_html(raw.get("jobDescription"))

    chunks: list[str] = []
    # Ordered so the parser sees company blurb, then duties, then requirements.
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        body = _strip_html(section.get("text")).strip()
        if not body:
            continue
        chunks.append(f"{title}\n{body}" if title else body)
    return "\n\n".join(chunks).strip()


def _url(raw: dict[str, Any], config: SourceConfig) -> str | None:
    ref = raw.get("ref")
    if isinstance(ref, str) and ref.startswith("http"):
        return ref
    posting_url = raw.get("postingUrl") or raw.get("applyUrl")
    if isinstance(posting_url, str) and posting_url.startswith("http"):
        return posting_url
    posting_id = raw.get("id")
    if posting_id:
        return f"https://jobs.smartrecruiters.com/{config.ats_identifier}/{posting_id}"
    return None


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return html.unescape(text)
