"""Apify broad-discovery connector — Channel B.

Answers a different question from Channel A. The watchlist connectors ask
"what are my 55 curated companies posting?"; this asks "what relevant jobs
exist *outside* that universe?" — which is the only reason it earns its cost
and its risk.

Structurally it is just another :class:`JobSourceConnector`. Actor dataset
records become ``RawJobInput`` exactly like a Greenhouse posting does, and
nothing downstream branches on Channel B. That is deliberate: the moment the
pipeline needs to know a job came from Apify, the abstraction has failed.

**Safety properties enforced here, not by convention:**

* Only an Actor listed **and approved** in ``approved_apify_actors.yaml`` may
  run. An Actor ID arriving from scraped text, an LLM, or a dataset field is
  never executed.
* An Actor requiring a LinkedIn login, password or session cookie is refused,
  regardless of its approval status.
* Budget is checked **before** launch, not after.
* Dataset records are untrusted input and are sanitized by the same pipeline
  that handles pasted text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.connectors.base import (
    CollectionResult,
    HealthResult,
    JobSourceConnector,
    RateLimitMetadata,
    SourceConfig,
    ValidationResult,
)
from app.models.enums import DiscoveryChannel, SourceCapability
from app.services.pipeline import RawJobInput

logger = logging.getLogger("jobhunt.connectors.apify")

APIFY_API_BASE = "https://api.apify.com/v2"


class ActorNotApproved(RuntimeError):
    """An Actor was requested that the registry does not approve."""


class ActorRequiresCredentials(RuntimeError):
    """An Actor needs a login or session cookie. Automatic reject."""


@dataclass
class ActorRunRequest:
    """What we intend to ask an Actor to do. Reviewable before anything runs."""

    actor_id: str
    search_profile: str
    actor_input: dict[str, Any] = field(default_factory=dict)
    max_results: int = 20
    purpose: str = ""

    def describe(self) -> str:
        return (
            f"{self.actor_id} · profile={self.search_profile} · "
            f"max_results={self.max_results} · {self.purpose}"
        )


class ApifyConnector(JobSourceConnector):
    """Consumes Apify Actor dataset records. Channel B.

    The class is usable **without any credentials**: ``normalize_dataset_record``
    and ``collect_from_records`` operate on records already in hand, which is
    how the whole Channel B path is tested with no token, no network and no
    spend.
    """

    connector_id = "apify"
    source_type = "APIFY"
    capability = SourceCapability.APIFY_DISCOVERY_CANDIDATE
    discovery_channel = DiscoveryChannel.B_APIFY_BROAD
    version = "0.1.0"
    docs_url = "https://docs.apify.com/api/v2"

    def __init__(self, client: Any = None, *, registry: Any = None) -> None:
        super().__init__(client)
        from app.services.actor_registry import ActorRegistry

        self.registry = registry or ActorRegistry()

    # -- contract --------------------------------------------------------
    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=True,
            requests_per_minute=None,
            min_interval_seconds=1.0,
            notes=(
                "Apify enforces per-account concurrency and memory limits rather than a "
                "simple request rate. The binding constraint for this project is the "
                "self-imposed budget: 4 runs/day, 250 records/run."
            ),
        )

    def validate_config(self, config: SourceConfig) -> ValidationResult:
        """Channel B is profile-driven, not company-driven."""
        actor_id = str(config.connector_config.get("actor_id") or "")
        if not actor_id:
            return ValidationResult.invalid("connector_config.actor_id is required.")

        approval = self.registry.check(actor_id)
        if not approval.approved:
            return ValidationResult.invalid(approval.reason)
        return ValidationResult.valid()

    def assert_runnable(self, actor_id: str) -> None:
        """Gate every launch. Raises rather than returning a soft failure.

        Called before any network activity so an unapproved Actor cannot be
        reached even by accident.
        """
        approval = self.registry.check(actor_id)
        if approval.requires_credentials:
            raise ActorRequiresCredentials(
                f"{actor_id} requires a LinkedIn login or session cookie. "
                f"This is an automatic reject: no credential is ever supplied to an Actor."
            )
        if not approval.approved:
            raise ActorNotApproved(approval.reason)

    def health_check(self, config: SourceConfig) -> HealthResult:
        """Registry-only check. Deliberately performs no network call."""
        from app.models.base import utcnow

        actor_id = str(config.connector_config.get("actor_id") or "")
        approval = self.registry.check(actor_id)
        return HealthResult(
            reachable=False,
            capability_failure=not approval.approved,
            status="APPROVED_NOT_CONNECTED" if approval.approved else "NOT_APPROVED",
            detail=approval.reason,
            checked_at=utcnow(),
        )

    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        """Launching an Actor spends money and reaches an external service.

        It stays unimplemented until the connection gate is approved. Until
        then Channel B runs entirely through ``collect_from_records``.
        """
        raise NotImplementedError(
            "Live Actor execution is not enabled. It requires explicit approval "
            "of the Apify connection gate. Use collect_from_records() with dataset "
            "records already in hand."
        )

    # -- the path that works today ---------------------------------------
    def collect_from_records(
        self,
        records: list[dict[str, Any]],
        config: SourceConfig,
        *,
        limit: int | None = None,
    ) -> CollectionResult:
        """Normalize dataset records that are already in hand.

        This is the whole Channel B ingestion path, exercisable with fixtures
        and no credentials.
        """
        result = CollectionResult(config=config, records_received=len(records))
        for record in records:
            if limit is not None and len(result.records) >= limit:
                break
            try:
                normalized = self.normalize_raw_record(record, config)
            except Exception as exc:  # noqa: BLE001 — one bad row must not end the run
                result.records_rejected += 1
                result.errors.append(f"Malformed dataset record skipped: {exc}")
                continue
            if normalized is None:
                result.records_rejected += 1
                continue
            result.records.append(normalized)
        result.records_valid = len(result.records)
        return result

    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        """Map one Actor dataset record onto the common ingestion contract.

        Actor schemas vary, so each field is read through a list of plausible
        keys. A record missing a title or company is dropped rather than
        guessed at — a job with an invented employer is worse than no job.
        """
        title = _first_str(raw, "title", "jobTitle", "job_title", "position", "name")
        company = _first_str(
            raw, "companyName", "company_name", "company", "employer", "organization"
        )
        if not title or not company:
            return None

        description = _first_str(
            raw,
            "descriptionText",
            "description",
            "job_description",
            "jobDescription",
            "snippet",
            default="",
        )
        # Canonical resolution needs an EMPLOYER-side URL. A LinkedIn URL only
        # tells us where the job was discovered, not who is hiring, so an
        # applyUrl/companyUrl pointing at an ATS is preferred for `url` and the
        # LinkedIn link is kept as discovery provenance in the raw payload.
        #
        # Getting this backwards would label LinkedIn as the canonical source
        # for jobs whose employer posting we can actually reach — and canonical
        # resolution is how Channel B's real value is measured.
        employer_url = _employer_url(raw)
        discovery_url = _first_str(raw, "jobUrl", "job_url", "url", "link")
        url = employer_url or discovery_url

        return RawJobInput(
            company_name=company,
            title=title,
            description=description or title,
            location=_first_str(
                raw, "location", "job_location", "jobLocation", "formattedLocation", "place"
            ),
            url=url,
            external_id=_first_str(raw, "id", "jobId", "job_id", "jobPostingId", "externalId"),
            posted_date=_parse_date(
                _first_str(
                    raw, "postedAt", "publishedAt", "datePosted", "postedDate", "posted_date"
                )
            ),
            connector=self.connector_id,
            discovery_channel=self.discovery_channel,
            source_capability=self.capability,
            source_type=self.source_type,
            apify_actor_id=str(config.connector_config.get("actor_id") or "") or None,
            apify_run_id=str(config.connector_config.get("run_id") or "") or None,
            raw_payload=raw,
            is_demo=config.is_fixture,
        )


#: Hosts that are job boards rather than employers. A URL on one of these
#: identifies where a posting was seen, not who is hiring.
_AGGREGATOR_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com")


def _employer_url(raw: dict[str, Any]) -> str | None:
    """The first URL that points at an employer rather than an aggregator."""
    for key in ("applyUrl", "applicationUrl", "apply_url", "companyUrl", "company_url"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        lowered = value.lower()
        if any(host in lowered for host in _AGGREGATOR_HOSTS):
            continue
        return value.strip()
    return None


def _first_str(raw: dict[str, Any], *keys: str, default: str | None = None) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        # Some Actors nest the value one level down.
        if isinstance(value, dict):
            for inner in ("name", "text", "value", "displayName"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return default


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    # Relative strings ("3 days ago") are common and deliberately NOT converted:
    # a guessed posted date would be indistinguishable from a real one.
    return None
