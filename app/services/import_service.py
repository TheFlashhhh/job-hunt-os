"""Manual job import — Channel C.

Four entry shapes are supported so the product is usable on day one without any
automated collection: pasted JD, URL + pasted JD, JSON, and CSV.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import DiscoveryChannel, SourceCapability
from app.services.pipeline import IngestOutcome, JobPipeline, RawJobInput

MANUAL_CONNECTOR = "manual"


@dataclass
class ImportResult:
    outcomes: list[IngestOutcome]
    errors: list[str]

    @property
    def created(self) -> int:
        return sum(1 for o in self.outcomes if o.created)

    @property
    def updated(self) -> int:
        return sum(1 for o in self.outcomes if o.merged_into_existing and not o.unchanged)

    @property
    def unchanged(self) -> int:
        return sum(1 for o in self.outcomes if o.unchanged)


class ManualImportService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.pipeline = JobPipeline(session)

    # -- A / B: pasted JD, optionally with a URL --------------------------
    def import_text(
        self,
        *,
        company_name: str,
        title: str,
        description: str,
        location: str | None = None,
        url: str | None = None,
        posted_date: date | None = None,
        deadline: date | None = None,
        is_demo: bool = False,
    ) -> IngestOutcome:
        payload = RawJobInput(
            company_name=company_name.strip(),
            title=title.strip(),
            description=description,
            location=(location or "").strip() or None,
            url=(url or "").strip() or None,
            posted_date=posted_date,
            deadline=deadline,
            connector=MANUAL_CONNECTOR,
            discovery_channel=DiscoveryChannel.C_MANUAL,
            source_capability=SourceCapability.MANUAL,
            source_type="MANUAL",
            is_demo=is_demo,
        )
        return self.pipeline.ingest(payload)

    def import_pasted_blob(
        self, blob: str, *, url: str | None = None, is_demo: bool = False
    ) -> IngestOutcome:
        """Best-effort parse of a raw paste where fields were not separated."""
        company, title, location = _guess_header_fields(blob)
        return self.import_text(
            company_name=company or "Unknown Company",
            title=title or "Untitled Role",
            description=blob,
            location=location,
            url=url,
            is_demo=is_demo,
        )

    # -- C: JSON ----------------------------------------------------------
    def import_json(self, payload: str | list[dict[str, Any]] | dict[str, Any]) -> ImportResult:
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                return ImportResult([], [f"Invalid JSON: {exc}"])
        else:
            data = payload

        records = data if isinstance(data, list) else data.get("jobs", [data])
        return self._import_records(records)

    # -- D: CSV -----------------------------------------------------------
    def import_csv(self, payload: str) -> ImportResult:
        try:
            reader = csv.DictReader(io.StringIO(payload))
            records = [dict(row) for row in reader]
        except csv.Error as exc:
            return ImportResult([], [f"Invalid CSV: {exc}"])
        return self._import_records(records)

    def _import_records(self, records: list[dict[str, Any]]) -> ImportResult:
        outcomes: list[IngestOutcome] = []
        errors: list[str] = []
        for index, record in enumerate(records, start=1):
            try:
                normalized = {str(k).strip().lower(): v for k, v in record.items()}
                company = _first(normalized, "company", "company_name", "employer")
                title = _first(normalized, "title", "job_title", "position", "role")
                description = _first(normalized, "description", "jd", "job_description", "body")
                if not company or not title:
                    errors.append(f"Row {index}: company and title are required.")
                    continue
                outcomes.append(
                    self.import_text(
                        company_name=str(company),
                        title=str(title),
                        description=str(description or ""),
                        location=_opt_str(_first(normalized, "location", "city", "job_location")),
                        url=_opt_str(_first(normalized, "url", "link", "job_url", "apply_url")),
                        posted_date=_parse_date(
                            _first(normalized, "posted_date", "posted", "date_posted")
                        ),
                        deadline=_parse_date(
                            _first(normalized, "deadline", "closes", "close_date")
                        ),
                    )
                )
            except Exception as exc:  # keep importing the rest of the batch
                errors.append(f"Row {index}: {type(exc).__name__}: {exc}")
        return ImportResult(outcomes, errors)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _opt_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


_LOCATION_HINT = re.compile(
    r"^(?:location|based in|office)\s*[:\-]\s*(.+)$|"
    r"^([A-Z][A-Za-z .'-]+,\s*(?:[A-Z]{2}|[A-Z][a-z]+))$",
    re.M,
)


def _guess_header_fields(blob: str) -> tuple[str | None, str | None, str | None]:
    """Pull company/title/location from the first lines of a raw paste."""
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()][:12]
    company = title = location = None

    for line in lines:
        low = line.lower()
        if company is None and low.startswith(("company:", "employer:")):
            company = line.split(":", 1)[1].strip()
        elif title is None and low.startswith(("title:", "position:", "role:", "job title:")):
            title = line.split(":", 1)[1].strip()
        elif location is None and low.startswith(("location:", "based in:", "office:")):
            location = line.split(":", 1)[1].strip()

    if title is None and lines:
        title = lines[0][:200]
    if company is None and len(lines) > 1:
        candidate = lines[1]
        if len(candidate) < 80 and not candidate.endswith((".", ":")):
            company = candidate
    if location is None:
        match = _LOCATION_HINT.search(blob[:2500])
        if match:
            location = (match.group(1) or match.group(2) or "").strip() or None

    return company, title, location
