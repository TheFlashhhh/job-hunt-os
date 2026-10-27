"""Greenhouse job-board connector.

Uses the documented public job-board API, which needs no authentication:

    https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

``content=true`` returns the full posting body in the list response, which
avoids one extra request per job — on a fifty-company watchlist that is the
difference between ~50 requests and several thousand.

Field availability varies by board. Greenhouse lets employers configure which
metadata they publish, so ``departments``, ``offices``, ``updated_at`` and
compensation are all optional. Every read here treats them that way; a missing
field is recorded as unknown, never guessed.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

from app.connectors.base import JobSourceConnector, RateLimitMetadata, SourceConfig
from app.models.enums import SourceCapability
from app.services.pipeline import RawJobInput

BOARDS_API = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseConnector(JobSourceConnector):
    connector_id = "greenhouse"
    source_type = "GREENHOUSE"
    capability = SourceCapability.DOCUMENTED_PUBLIC_API
    version = "0.1.0"
    docs_url = "https://developers.greenhouse.io/job-board.html"

    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=False,
            requests_per_minute=None,
            min_interval_seconds=0.5,
            notes=(
                "Greenhouse publishes no explicit limit for the public board API. "
                "One request per board with content=true; spacing is a courtesy "
                "default, not a documented figure."
            ),
        )

    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        url = f"{BOARDS_API}/{config.ats_identifier}/jobs"
        payload = self.client.get_json(url, params={"content": "true"})
        if not isinstance(payload, dict):
            return []
        jobs = payload.get("jobs")
        return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []

    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        title = str(raw.get("title") or "").strip()
        if not title:
            return None

        external_id = raw.get("id")
        description = _clean_content(raw.get("content"))
        if not description:
            # A posting with no body cannot be analysed against the Evidence
            # Bank. Keep the title so the record is still discoverable.
            description = title

        return self._base_input(
            config,
            title=title,
            description=description,
            location=_location(raw),
            url=raw.get("absolute_url"),
            external_id=str(external_id) if external_id is not None else None,
            posted_date=_first_date(raw.get("first_published"), raw.get("updated_at")),
            raw_payload=raw,
        )


def _location(raw: dict[str, Any]) -> str | None:
    """Prefer the posting's own location; fall back to the first office."""
    location = raw.get("location")
    if isinstance(location, dict):
        name = str(location.get("name") or "").strip()
        if name:
            return name

    offices = raw.get("offices")
    if isinstance(offices, list):
        names = [
            str(o.get("name")).strip() for o in offices if isinstance(o, dict) and o.get("name")
        ]
        # "No offices" is a real Greenhouse value meaning fully remote; it is
        # not a location and should not be presented as one.
        names = [n for n in names if n.lower() not in {"no offices", "n/a"}]
        if names:
            return ", ".join(names[:3])
    return None


def _first_date(*values: Any) -> date | None:
    for value in values:
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_content(content: Any) -> str:
    """Greenhouse returns HTML-escaped HTML. Unescape once, then strip tags.

    Full sanitization still happens in the pipeline; this only turns the
    double-encoded payload into something the sanitizer can read.
    """
    if not content or not isinstance(content, str):
        return ""
    text = html.unescape(content)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
