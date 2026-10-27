"""Ashby job-posting connector.

Uses the documented public posting API, no authentication:

    https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true

Ashby returns structured compensation on boards that publish it, which is worth
requesting: pay transparency data is otherwise only available by parsing prose.
It is optional per board, so its absence is normal and never inferred.

The documented API returns everything needed, so this connector does not fall
back to HTML scraping — a scraped board would silently change shape under us
and produce wrong data rather than an honest failure.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

from app.connectors.base import JobSourceConnector, RateLimitMetadata, SourceConfig
from app.models.enums import SourceCapability
from app.services.pipeline import RawJobInput

POSTING_API = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyConnector(JobSourceConnector):
    connector_id = "ashby"
    source_type = "ASHBY"
    capability = SourceCapability.DOCUMENTED_PUBLIC_API
    version = "0.1.0"
    docs_url = "https://developers.ashbyhq.com/reference/job-posting-api"

    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=False,
            min_interval_seconds=0.5,
            notes=(
                "Ashby documents no explicit limit for the public posting API. "
                "One request per board; spacing is a courtesy default."
            ),
        )

    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        url = f"{POSTING_API}/{config.ats_identifier}"
        payload = self.client.get_json(url, params={"includeCompensation": "true"})
        if not isinstance(payload, dict):
            return []
        jobs = payload.get("jobs")
        return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []

    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        title = str(raw.get("title") or "").strip()
        if not title:
            return None

        # isListed=false means the board is still serving a posting the employer
        # has unpublished. Collecting it would surface a job that cannot be
        # applied to.
        if raw.get("isListed") is False:
            return None

        description = _description(raw) or title
        compensation = _compensation_summary(raw)
        if compensation:
            # Appended rather than stored separately: the pipeline's own
            # compensation extraction reads the description, so this keeps one
            # code path instead of an Ashby-specific branch.
            description = f"{description}\n\nCompensation: {compensation}"

        return self._base_input(
            config,
            title=title,
            description=description,
            location=_location(raw),
            url=raw.get("jobUrl") or raw.get("applyUrl"),
            external_id=str(raw.get("id")) if raw.get("id") else None,
            posted_date=_parse_date(raw.get("publishedAt") or raw.get("updatedAt")),
            raw_payload=raw,
        )


def _location(raw: dict[str, Any]) -> str | None:
    parts: list[str] = []
    location = raw.get("location")
    if isinstance(location, str) and location.strip():
        parts.append(location.strip())

    if raw.get("isRemote") is True:
        parts.append("(Remote)")

    # secondaryLocations lets one posting cover several offices.
    secondary = raw.get("secondaryLocations")
    if isinstance(secondary, list):
        extra = [
            str(entry.get("location")).strip()
            for entry in secondary
            if isinstance(entry, dict) and entry.get("location")
        ]
        if extra:
            parts.append("/ " + ", ".join(extra[:3]))
    return " ".join(parts) if parts else None


def _description(raw: dict[str, Any]) -> str:
    plain = raw.get("descriptionPlain")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    return _strip_html(raw.get("descriptionHtml") or raw.get("description")).strip()


def _compensation_summary(raw: dict[str, Any]) -> str:
    """Flatten Ashby's structured compensation, when the board publishes it."""
    compensation = raw.get("compensation")
    if not isinstance(compensation, dict):
        return ""

    summary = compensation.get("summaryComponents")
    if isinstance(summary, list):
        parts: list[str] = []
        for component in summary:
            if not isinstance(component, dict):
                continue
            minimum = component.get("minValue")
            maximum = component.get("maxValue")
            currency = component.get("currencyCode") or ""
            interval = component.get("interval") or ""
            if minimum is None and maximum is None:
                continue
            if minimum is not None and maximum is not None:
                parts.append(f"{currency} {minimum:,.0f}–{maximum:,.0f} {interval}".strip())
            else:
                value = minimum if minimum is not None else maximum
                parts.append(f"{currency} {value:,.0f} {interval}".strip())
        if parts:
            return "; ".join(parts)

    text = compensation.get("compensationTierSummary")
    return str(text).strip() if isinstance(text, str) else ""


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
