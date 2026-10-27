"""Lever postings connector.

Uses the documented public postings endpoint, no authentication:

    https://api.lever.co/v0/postings/{site}?mode=json

Lever's shape differs from Greenhouse in a way that matters: the body arrives
split across ``descriptionPlain`` and a ``lists`` array of titled bullet groups
("Requirements", "What you'll do"). Reassembling those groups with their
headings intact is what lets the rebuilt JD parser read them as sections rather
than as one undifferentiated blob — so this connector rebuilds the document
rather than concatenating text.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from typing import Any

from app.connectors.base import JobSourceConnector, RateLimitMetadata, SourceConfig
from app.models.enums import SourceCapability
from app.services.pipeline import RawJobInput

POSTINGS_API = "https://api.lever.co/v0/postings"


class LeverConnector(JobSourceConnector):
    connector_id = "lever"
    source_type = "LEVER"
    capability = SourceCapability.DOCUMENTED_PUBLIC_API
    version = "0.1.0"
    docs_url = "https://github.com/lever/postings-api"

    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=False,
            min_interval_seconds=0.5,
            notes=(
                "Lever documents no explicit limit for the public postings API. "
                "One request per site; spacing is a courtesy default."
            ),
        )

    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        url = f"{POSTINGS_API}/{config.ats_identifier}"
        payload = self.client.get_json(url, params={"mode": "json"})
        if not isinstance(payload, list):
            return []
        return [p for p in payload if isinstance(p, dict)]

    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        title = str(raw.get("text") or "").strip()
        if not title:
            return None

        raw_categories = raw.get("categories")
        categories: dict[str, Any] = raw_categories if isinstance(raw_categories, dict) else {}
        return self._base_input(
            config,
            title=title,
            description=_build_description(raw) or title,
            location=_location(categories, raw),
            # hostedUrl is the canonical Lever posting; applyUrl is the form.
            url=raw.get("hostedUrl") or raw.get("applyUrl"),
            external_id=str(raw.get("id")) if raw.get("id") else None,
            posted_date=_epoch_date(raw.get("createdAt")),
            raw_payload=raw,
        )


def _location(categories: dict[str, Any], raw: dict[str, Any]) -> str | None:
    parts: list[str] = []
    location = categories.get("location")
    if isinstance(location, str) and location.strip():
        parts.append(location.strip())

    # workplaceType ("remote"/"hybrid"/"onsite") is a separate field. Folding it
    # into the location string lets the existing remote classifier see it
    # without a Lever-specific branch in the pipeline.
    workplace = raw.get("workplaceType")
    if isinstance(workplace, str) and workplace.strip():
        parts.append(f"({workplace.strip()})")
    return " ".join(parts) if parts else None


def _build_description(raw: dict[str, Any]) -> str:
    """Reassemble the posting, preserving Lever's section headings."""
    chunks: list[str] = []

    opening = raw.get("descriptionPlain") or _strip_html(raw.get("description"))
    if opening:
        chunks.append(opening.strip())

    lists = raw.get("lists")
    if isinstance(lists, list):
        for group in lists:
            if not isinstance(group, dict):
                continue
            heading = str(group.get("text") or "").strip()
            content = group.get("content")
            bullets = _bullets(content)
            if not bullets:
                continue
            # The heading is what tells the parser this is a requirements
            # section rather than prose about the company.
            chunks.append(f"{heading}\n{bullets}" if heading else bullets)

    closing = raw.get("additionalPlain") or _strip_html(raw.get("additional"))
    if closing:
        chunks.append(closing.strip())

    return "\n\n".join(c for c in chunks if c).strip()


def _bullets(content: Any) -> str:
    """Lever list content is an HTML ``<li>`` string. Emit real bullet lines."""
    if not content or not isinstance(content, str):
        return ""
    text = html.unescape(content)
    items = re.findall(r"<li[^>]*>(.*?)</li>", text, flags=re.I | re.S)
    if items:
        cleaned = [_strip_html(item).strip() for item in items]
        return "\n".join(f"- {c}" for c in cleaned if c)
    stripped = _strip_html(text).strip()
    return f"- {stripped}" if stripped else ""


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    return html.unescape(text)


def _epoch_date(value: Any) -> date | None:
    """Lever timestamps are epoch milliseconds."""
    if not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC).date()
    except (ValueError, OverflowError, OSError):
        return None
