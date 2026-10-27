"""The ``JobSourceConnector`` contract.

Every source — a documented ATS API today, an Apify Actor in Slice 4 — reduces
to the same thing: something that can be configured, checked, and asked for a
list of postings, each of which becomes a ``RawJobInput``. Nothing downstream
of ``collect()`` knows which source a job came from except through recorded
provenance, and that is deliberate. The moment the pipeline branches on
"if greenhouse ... elif lever ..." the connector abstraction has failed and
every future source becomes a pipeline change.

The four capability-relevant facts a connector must state about itself:

``connector_id``   stable identifier, recorded on every raw record
``source_type``    the ATS or platform family
``capability``     which access class this is, from the approved taxonomy
``version``        bumped when parsing changes, so stale analyses re-run

Three methods separate cleanly by cost:

``validate_config()``  no network at all — is this configuration coherent?
``health_check()``     one cheap read — is this source actually reachable?
``collect()``          the real fetch

Splitting them matters operationally: ``jobhunt source check`` across fifty
companies must be able to tell "misconfigured" from "unreachable" from "fine"
without pulling thousands of postings.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.connectors.http import ConnectorHTTPError, HttpClient
from app.models.enums import DiscoveryChannel, SourceCapability
from app.services.pipeline import RawJobInput

logger = logging.getLogger("jobhunt.connectors")


@dataclass
class SourceConfig:
    """One company's configuration for one connector.

    Built from ``data/config/companies.yaml``. ``is_fixture`` marks a board used
    only to prove connector behaviour; everything it produces is flagged and
    excluded from real analytics.
    """

    company_name: str
    ats_identifier: str
    ats_type: str
    company_id: str | None = None
    careers_url: str | None = None
    watchlist_scope: str = "BOTH"
    priority: int = 3
    locations: list[str] = field(default_factory=list)
    target_role_families: list[str] = field(default_factory=list)
    connector_config: dict[str, Any] = field(default_factory=dict)
    #: On the watchlist and wanted in the search universe.
    active: bool = False
    #: Whether Channel A can actually poll this company today. A great target on
    #: an unsupported ATS stays on the watchlist with this False — company value
    #: and automation capability are separate axes, and conflating them would
    #: quietly delete good employers from the search universe.
    automated_collection_enabled: bool = False
    tier: str = "TIER_B"
    fall_signal: str = "UNKNOWN"
    industry_category: str | None = None
    last_validated_at: str | None = None
    is_fixture: bool = False
    notes: str | None = None
    #: How the company entered the registry. Seed entries come from
    #: ``companies.yaml``; everything else from a ``company`` row.
    registry_source: str = "CORE_SEED"

    @property
    def label(self) -> str:
        return f"{self.company_name} ({self.ats_type}:{self.ats_identifier})"


@dataclass
class ValidationResult:
    ok: bool
    problems: list[str] = field(default_factory=list)

    @classmethod
    def valid(cls) -> ValidationResult:
        return cls(ok=True)

    @classmethod
    def invalid(cls, *problems: str) -> ValidationResult:
        return cls(ok=False, problems=list(problems))


@dataclass
class HealthResult:
    """Whether a source is usable right now, and why not if it isn't."""

    reachable: bool
    #: True when the source answered but this company is not publicly exposed.
    #: Distinct from unreachable: a tier-gated SmartRecruiters company is a
    #: capability fact about that company, not an outage.
    capability_failure: bool = False
    status: str = "UNKNOWN"
    detail: str = ""
    postings_visible: int | None = None
    checked_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "capability_failure": self.capability_failure,
            "status": self.status,
            "detail": self.detail,
            "postings_visible": self.postings_visible,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass
class CollectionResult:
    """What one connector produced for one company."""

    config: SourceConfig
    records: list[RawJobInput] = field(default_factory=list)
    records_received: int = 0
    records_valid: int = 0
    records_rejected: int = 0
    errors: list[str] = field(default_factory=list)
    capability_failure: bool = False
    http_stats: dict[str, Any] = field(default_factory=dict)
    #: Postings the board listed but the connector did not re-fetch, because
    #: they are already stored (``reuses_known_postings``). They are still open
    #: — the listing says so — and lifecycle reconciliation counts them as seen.
    observed_external_ids: set[str] = field(default_factory=set)
    #: False when the listing was cut short (a page bound hit, a page failed).
    #: Disappearance is only judged from a COMPLETE listing: a posting missing
    #: from a truncated one has not disappeared, we just did not read that far.
    listing_complete: bool = True

    @property
    def ok(self) -> bool:
        return not self.errors and not self.capability_failure


@dataclass
class RateLimitMetadata:
    """What this connector believes about the source's limits.

    Documented values where the vendor publishes them; conservative guesses
    otherwise, marked as such — an assumed limit must never be presented as a
    documented one.
    """

    documented: bool
    requests_per_minute: int | None = None
    min_interval_seconds: float = 0.0
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "documented": self.documented,
            "requests_per_minute": self.requests_per_minute,
            "min_interval_seconds": self.min_interval_seconds,
            "notes": self.notes,
        }


class JobSourceConnector(ABC):
    """Base class for every job source."""

    connector_id: str = "base"
    source_type: str = "UNKNOWN"
    capability: SourceCapability = SourceCapability.MANUAL
    discovery_channel: DiscoveryChannel = DiscoveryChannel.A_ATS_WATCHLIST
    version: str = "0.1.0"
    #: Documentation for the endpoint this connector uses. Recorded in source
    #: metadata so a future reader can check the contract without guessing.
    docs_url: str = ""
    #: True for a source whose listing is cheap and whose bodies cost one
    #: request each (Workday). The collector then passes the ids it already
    #: holds, and the connector fetches bodies only for postings it has not
    #: seen. Sources that return bodies in the listing leave this False.
    reuses_known_postings: bool = False

    def __init__(self, client: HttpClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._known_external_ids: set[str] = set()
        self._observed_external_ids: set[str] = set()
        self._listing_complete = True

    # -- client ----------------------------------------------------------
    @property
    def client(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient(min_interval=self.rate_limit_metadata().min_interval_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> JobSourceConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- contract --------------------------------------------------------
    def validate_config(self, config: SourceConfig) -> ValidationResult:
        """Check configuration coherence. Never touches the network."""
        problems: list[str] = []
        if not config.company_name.strip():
            problems.append("company_name is required.")
        if not config.ats_identifier.strip():
            problems.append(f"ats_identifier is required for {self.source_type}.")
        if config.ats_type.upper() != self.source_type:
            problems.append(
                f"ats_type {config.ats_type!r} does not match this connector ({self.source_type})."
            )
        return ValidationResult.valid() if not problems else ValidationResult.invalid(*problems)

    @abstractmethod
    def _fetch(self, config: SourceConfig) -> list[dict[str, Any]]:
        """Return the source's raw posting dicts. One network call path."""

    @abstractmethod
    def normalize_raw_record(self, raw: dict[str, Any], config: SourceConfig) -> RawJobInput | None:
        """Map one source record onto the common ingestion contract.

        Returns None when the record cannot be used — a posting with no title
        is dropped rather than guessed at.
        """

    def health_check(self, config: SourceConfig) -> HealthResult:
        """One cheap read to confirm the source answers for this company."""
        from app.models.base import utcnow

        validation = self.validate_config(config)
        if not validation.ok:
            return HealthResult(
                reachable=False,
                status="INVALID_CONFIG",
                detail="; ".join(validation.problems),
                checked_at=utcnow(),
            )
        try:
            postings = self._fetch(config)
        except ConnectorHTTPError as exc:
            return HealthResult(
                reachable=False,
                capability_failure=_is_capability_failure(exc),
                status=type(exc).__name__,
                detail=exc.summary,
                checked_at=utcnow(),
            )
        return HealthResult(
            reachable=True,
            status="OK",
            detail=f"{len(postings)} posting(s) visible.",
            postings_visible=len(postings),
            checked_at=utcnow(),
        )

    def collect(
        self,
        config: SourceConfig,
        *,
        limit: int | None = None,
        known_external_ids: set[str] | None = None,
    ) -> CollectionResult:
        """Fetch and normalize. Never raises for an expected source failure.

        A connector that raised would take down a whole multi-company run, so
        source failures are returned as a result with errors recorded and the
        run continues to the next company.
        """
        result = CollectionResult(config=config)
        self._known_external_ids = set(known_external_ids or ())
        self._observed_external_ids = set()
        self._listing_complete = True

        validation = self.validate_config(config)
        if not validation.ok:
            result.errors.extend(validation.problems)
            return result

        try:
            postings = self._fetch(config)
        except ConnectorHTTPError as exc:
            result.errors.append(exc.summary)
            result.capability_failure = _is_capability_failure(exc)
            result.http_stats = self.client.stats.as_dict()
            return result

        result.observed_external_ids = set(self._observed_external_ids)
        result.listing_complete = self._listing_complete
        result.records_received = len(postings) + len(result.observed_external_ids)
        for posting in postings:
            if limit is not None and len(result.records) >= limit:
                break
            try:
                record = self.normalize_raw_record(posting, config)
            except Exception as exc:  # noqa: BLE001 — one bad record must not end the run
                result.records_rejected += 1
                result.errors.append(f"Malformed record skipped: {exc}")
                continue
            if record is None:
                result.records_rejected += 1
                continue
            result.records.append(record)

        result.records_valid = len(result.records)
        result.http_stats = self.client.stats.as_dict()
        return result

    # -- metadata --------------------------------------------------------
    def rate_limit_metadata(self) -> RateLimitMetadata:
        return RateLimitMetadata(
            documented=False,
            min_interval_seconds=0.5,
            notes="No documented limit; conservative default spacing applied.",
        )

    def source_metadata(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "source_type": self.source_type,
            "capability": self.capability.value,
            "discovery_channel": self.discovery_channel.value,
            "version": self.version,
            "docs_url": self.docs_url,
            "rate_limit": self.rate_limit_metadata().as_dict(),
        }

    # -- helpers for subclasses ------------------------------------------
    def _base_input(
        self,
        config: SourceConfig,
        *,
        title: str,
        description: str,
        location: str | None,
        url: str | None,
        external_id: str | None,
        posted_date: date | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> RawJobInput:
        """Build the common ingestion contract with provenance attached."""
        return RawJobInput(
            company_name=config.company_name,
            title=title,
            description=description,
            location=location,
            url=url,
            external_id=external_id,
            posted_date=posted_date,
            connector=self.connector_id,
            discovery_channel=self.discovery_channel,
            source_capability=self.capability,
            source_type=self.source_type,
            raw_payload=raw_payload,
            # Fixture boards exist only to prove the connector works. Their
            # output must never reach a real KPI.
            is_demo=config.is_fixture,
        )


def _is_capability_failure(exc: ConnectorHTTPError) -> bool:
    """403/404 mean *this company* is not available, not that the source is down."""
    from app.connectors.http import SourceForbidden, SourceNotFound

    return isinstance(exc, SourceForbidden | SourceNotFound)


def iter_pages(
    client: HttpClient, url: str, *, params: dict[str, Any], page_size: int, max_pages: int = 20
) -> Iterator[list[dict[str, Any]]]:
    """Offset-paginated JSON list endpoint.

    Bounded by ``max_pages`` so a source that ignores paging parameters cannot
    produce an unbounded loop.
    """
    for page in range(max_pages):
        payload = client.get_json(url, params={**params, "skip": page * page_size})
        batch = payload if isinstance(payload, list) else []
        if not batch:
            return
        yield batch
        if len(batch) < page_size:
            return
