"""The company registry: every employer structured collection knows about.

**The original 52-company list is a seed set, not the market.** This module is
the one place that answers "which companies does Job Hunt OS cover, and how
well?" — the curated seed and everything added since, merged into one set.

Two stores, one registry:

``CORE_SEED``                    ``data/config/companies.yaml``. Human-curated,
                                 reviewed, version-controlled. The user edits it;
                                 the application only reads it. Its board
                                 configuration always comes from the file.
everything else                  ``company`` rows with ``registry_source`` set:
                                 ``DISCOVERED`` (monitored by a click from a
                                 job), ``USER_ADDED`` (Sources → Add Company),
                                 ``IMPORTED_FROM_JOB`` and ``CURATED_EXPANSION``
                                 (a reviewed expansion batch).

The split exists so the app never edits a reviewed config file. A registry row
is application state: it was added by a decision, it can be paused by one, and
losing it costs one click rather than a merge conflict.

Operational state — paused or not, last poll, last failure, what the board
returned — lives on the ``company`` row for both populations. Pausing a core
company is therefore possible without touching the file; deleting one is not.

What this module does NOT do:

* enumerate ATS namespaces looking for boards. A company enters because a
  person, a real posting, or a reviewed batch put it there.
* monitor anything on a guess. Only a ``CONFIRMED`` careers source can be
  activated, and activation is always a human decision.
* accept an ATS no connector implements as monitored. Such a company is kept
  ``MANUAL_ONLY`` so coverage diagnostics can show the gap instead of hiding it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.connectors.base import SourceConfig
from app.models.base import as_utc, utcnow
from app.models.enums import MonitoringStatus, RegistrySource, SourceVerification
from app.models.ingestion import Company
from app.services.text import normalize_company
from app.services.watchlist import (
    AUTOMATABLE_ATS,
    WatchlistLoadResult,
    entries_for_lane,
    load_watchlist,
)

logger = logging.getLogger("jobhunt.monitored")

#: Marks a ``company`` row as one the user chose to monitor. Predates the
#: registry columns; still written so older readers keep working.
WATCHLIST_SOURCE_KEY = "watchlist_source"
DISCOVERED = "DISCOVERED"
CORE = "CORE_WATCHLIST"

#: Registry sources stored in the database (everything but the YAML seed).
DB_SOURCES = frozenset(s.value for s in RegistrySource if s is not RegistrySource.CORE_SEED)

# -- health states, derived, never stored --------------------------------
HEALTHY = "HEALTHY"
STALE = "STALE"
FAILING = "FAILING"
UNSUPPORTED = "UNSUPPORTED"
PAUSED = "PAUSED"
EXPERIMENTAL = "EXPERIMENTAL"
#: Configured and polled, but no poll has ever succeeded or failed. Kept apart
#: from HEALTHY on purpose: a board that has never run has proven nothing.
NOT_YET_COLLECTED = "NOT_YET_COLLECTED"

#: A board last collected longer ago than this is STALE. The structured cadence
#: is six hours, so a day is four missed sweeps.
STALE_AFTER_HOURS = 24.0


@dataclass
class MonitorOutcome:
    ok: bool
    company: Company | None = None
    created: bool = False
    reason: str = ""
    detail: str = ""


@dataclass
class RegistryEntry:
    """One employer as the registry sees it: seed or row, merged."""

    company_name: str
    ats_type: str
    ats_identifier: str
    registry_source: str
    monitoring_status: str
    source_verification: str
    company_id: str | None = None
    careers_url: str | None = None
    watchlist_scope: str = "BOTH"
    priority: int = 3
    tier: str = "TIER_C"
    connector_config: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None
    discovered_from_job_id: str | None = None
    last_successful_collection_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None
    last_jobs_seen: int | None = None
    last_new_jobs: int | None = None
    #: The seed entry this came from, when it is one. Its board config wins.
    seed: SourceConfig | None = None

    @property
    def is_core(self) -> bool:
        return self.registry_source == RegistrySource.CORE_SEED.value

    @property
    def collectable(self) -> bool:
        """A connector exists and a board identity is known."""
        return self.ats_type in AUTOMATABLE_ATS and bool(self.ats_identifier)

    def health(self, now: datetime | None = None) -> str:
        """HEALTHY / STALE / FAILING / UNSUPPORTED / PAUSED / EXPERIMENTAL /
        NOT_YET_COLLECTED — derived from recorded polls only."""
        status = self.monitoring_status
        if status == MonitoringStatus.PAUSED.value:
            return PAUSED
        if status in (MonitoringStatus.MANUAL_ONLY.value, MonitoringStatus.VERIFY_LATER.value):
            return UNSUPPORTED
        if not self.collectable:
            return UNSUPPORTED
        if status == MonitoringStatus.EXPERIMENTAL.value:
            return EXPERIMENTAL
        success = as_utc(self.last_successful_collection_at)
        failure = as_utc(self.last_failure_at)
        if failure is not None and (success is None or failure >= success):
            return FAILING
        if success is None:
            return NOT_YET_COLLECTED
        now = now or utcnow()
        if now - success > timedelta(hours=STALE_AFTER_HOURS):
            return STALE
        return HEALTHY

    def as_config(self) -> SourceConfig:
        if self.seed is not None:
            return self.seed
        return SourceConfig(
            company_name=self.company_name,
            company_id=self.company_id,
            ats_type=self.ats_type,
            ats_identifier=self.ats_identifier,
            careers_url=self.careers_url,
            watchlist_scope=self.watchlist_scope,
            priority=self.priority,
            connector_config=dict(self.connector_config),
            active=True,
            automated_collection_enabled=True,
            # Registry companies collect after the curated seed. Tier is
            # scheduling order only; it never touches eligibility or fit.
            tier="TIER_C",
            fall_signal="UNKNOWN",
            is_fixture=False,
            notes=self.notes,
            registry_source=self.registry_source,
        )


# ---------------------------------------------------------------------------
# Provider rollout
# ---------------------------------------------------------------------------


def provider_policy(ats_type: str) -> dict[str, Any]:
    """``collection_policy.yaml`` → ``providers.<ATS>``. Empty when unset."""
    providers = load_yaml_config("collection_policy.yaml").get("providers") or {}
    entry = providers.get((ats_type or "").upper()) or {}
    return entry if isinstance(entry, dict) else {}


def provider_enabled(ats_type: str) -> bool:
    """The rollout switch. A provider can land without joining the sweep."""
    return bool(provider_policy(ats_type).get("enabled", True))


# ---------------------------------------------------------------------------
# Reading the registry
# ---------------------------------------------------------------------------


def core_watchlist(watchlist: WatchlistLoadResult | None = None) -> WatchlistLoadResult:
    return watchlist or load_watchlist()


def registry_rows(session: Session) -> list[Company]:
    """Database registry entries (everything but the seed), oldest first."""
    rows = (
        session.execute(
            select(Company).where(
                Company.is_demo.is_(False),
                Company.is_fixture.is_(False),
            )
        )
        .scalars()
        .all()
    )
    out = [c for c in rows if _row_source(c) in DB_SOURCES]
    return sorted(out, key=lambda c: as_utc(c.created_at) or utcnow())


def registry_entries(
    session: Session, *, watchlist: WatchlistLoadResult | None = None
) -> list[RegistryEntry]:
    """The whole registry: the seed (with its operational state), then rows.

    A row naming a board the seed already covers is dropped — the seed entry
    is the same monitoring target and polling it twice would double the
    requests for no new postings.
    """
    core = core_watchlist(watchlist)
    rows = session.execute(select(Company)).scalars().all()
    by_board: dict[tuple[str, str], Company] = {}
    by_name: dict[str, Company] = {}
    for row in rows:
        if row.is_demo or row.is_fixture:
            continue
        if row.ats_type and row.ats_identifier:
            by_board.setdefault(board_key(row.ats_type, row.ats_identifier), row)
        for key in _name_keys(row.company_name):
            by_name.setdefault(key, row)

    entries: list[RegistryEntry] = []
    seen: set[tuple[str, str]] = set()
    seen_rows: set[str] = set()
    for seed in core.watchlist_companies:
        seed_row: Company | None = None
        if seed.ats_identifier:
            seed_row = by_board.get(board_key(seed.ats_type, seed.ats_identifier))
        if seed_row is None:
            seed_row = next((by_name[k] for k in _name_keys(seed.company_name) if k in by_name), None)
        if (
            seed_row is not None
            and not seed.automated_collection_enabled
            and _row_source(seed_row) in DB_SOURCES
            and (seed_row.ats_type or "").upper() in AUTOMATABLE_ATS
            and seed_row.ats_identifier
        ):
            # A seed employer the file could only list as manual-only, now
            # covered by a registry row with a real board (Fidelity: "UNKNOWN"
            # in companies.yaml, Workday in the registry). The row IS that
            # employer's monitoring; listing the seed too would count one
            # covered company as a coverage gap.
            continue
        if seed_row is not None and _row_source(seed_row) in DB_SOURCES and seed_row.ats_identifier and (
            board_key(seed_row.ats_type or "", seed_row.ats_identifier)
            != board_key(seed.ats_type, seed.ats_identifier)
        ):
            # Same employer, different board: the row is its own target (for
            # example a replacement for a seed token that stopped resolving).
            seed_row = None
        if seed_row is not None:
            seen_rows.add(seed_row.company_id)
        paused = seed_row is not None and seed_row.monitoring_status == MonitoringStatus.PAUSED.value
        if paused:
            status = MonitoringStatus.PAUSED.value
        elif seed.automated_collection_enabled:
            status = MonitoringStatus.ACTIVE.value
        else:
            status = MonitoringStatus.MANUAL_ONLY.value
        entry = RegistryEntry(
            company_name=seed.company_name,
            ats_type=seed.ats_type,
            ats_identifier=seed.ats_identifier,
            registry_source=RegistrySource.CORE_SEED.value,
            monitoring_status=status,
            source_verification=(
                SourceVerification.CONFIRMED.value
                if seed.automated_collection_enabled
                else SourceVerification.UNKNOWN.value
            ),
            company_id=seed_row.company_id if seed_row is not None else seed.company_id,
            careers_url=seed.careers_url,
            watchlist_scope=seed.watchlist_scope,
            priority=seed.priority,
            tier=seed.tier,
            connector_config=dict(seed.connector_config),
            notes=seed.notes,
            seed=seed,
        )
        _overlay_state(entry, seed_row)
        entries.append(entry)
        if seed.ats_identifier:
            seen.add(board_key(seed.ats_type, seed.ats_identifier))

    for db_row in registry_rows(session):
        if db_row.company_id in seen_rows:
            continue
        board = board_key(db_row.ats_type or "", db_row.ats_identifier or "")
        if db_row.ats_identifier and board in seen:
            continue
        if db_row.ats_identifier:
            seen.add(board)
        entry = RegistryEntry(
            company_name=db_row.company_name,
            ats_type=(db_row.ats_type or "UNKNOWN").upper(),
            ats_identifier=db_row.ats_identifier or "",
            registry_source=_row_source(db_row) or RegistrySource.DISCOVERED.value,
            monitoring_status=_row_status(db_row),
            source_verification=db_row.source_verification or SourceVerification.CONFIRMED.value,
            company_id=db_row.company_id,
            careers_url=db_row.careers_url,
            watchlist_scope=db_row.watchlist_scope or "BOTH",
            priority=db_row.priority or 3,
            connector_config=dict(db_row.connector_config or {}),
            notes=db_row.notes,
            discovered_from_job_id=db_row.discovered_from_job_id,
        )
        _overlay_state(entry, db_row)
        entries.append(entry)
    return entries


def collection_targets(
    session: Session,
    *,
    lane: str | None = None,
    watchlist: WatchlistLoadResult | None = None,
    include_core: bool = True,
    include_registry: bool = True,
    include_experimental: bool = False,
) -> list[SourceConfig]:
    """What a sweep polls: seed first (tier-ordered), then registry companies.

    Skipped, each for a recorded reason: a paused company, a provider whose
    rollout switch is off, and — unless named explicitly — an EXPERIMENTAL
    canary that has not yet passed its gate.
    """
    core = core_watchlist(watchlist)
    entries = registry_entries(session, watchlist=core)
    # One policy read per provider, not per company: the policy file is
    # parsed on every read, and a sweep has hundreds of companies.
    switches: dict[str, bool] = {}

    def enabled(ats_type: str) -> bool:
        if ats_type not in switches:
            switches[ats_type] = provider_enabled(ats_type)
        return switches[ats_type]

    paused_seed = {
        board_key(e.ats_type, e.ats_identifier)
        for e in entries
        if e.is_core and e.monitoring_status == MonitoringStatus.PAUSED.value
    }

    out: list[SourceConfig] = []
    if include_core:
        for config in entries_for_lane(core, lane):
            if board_key(config.ats_type, config.ats_identifier) in paused_seed:
                continue
            if not enabled(config.ats_type):
                continue
            out.append(config)

    if include_registry:
        wanted = {MonitoringStatus.ACTIVE.value}
        if include_experimental:
            wanted.add(MonitoringStatus.EXPERIMENTAL.value)
        registry = [
            e
            for e in entries
            if not e.is_core
            and e.monitoring_status in wanted
            and e.collectable
            and enabled(e.ats_type)
        ]
        if lane and lane != "ALL":
            scope = "FALL" if lane.startswith("FALL") else "FULL_TIME"
            registry = [e for e in registry if e.watchlist_scope in (scope, "BOTH")]
        out.extend(e.as_config() for e in sorted(registry, key=lambda e: e.company_name))
    return out


# -- compatibility readers ---------------------------------------------------


def discovered_companies(session: Session) -> list[Company]:
    """Registry rows currently polled, newest first."""
    rows = [
        r
        for r in registry_rows(session)
        if _row_status(r) == MonitoringStatus.ACTIVE.value and r.on_watchlist and r.active
    ]
    return sorted(rows, key=lambda c: as_utc(c.created_at) or utcnow(), reverse=True)


def discovered_configs(
    session: Session, *, watchlist: WatchlistLoadResult | None = None
) -> list[SourceConfig]:
    """Registry companies the sweep polls, minus anything the seed covers."""
    return collection_targets(session, watchlist=watchlist, include_core=False)


def discovered_for_lane(
    session: Session, lane: str | None, *, watchlist: WatchlistLoadResult | None = None
) -> list[SourceConfig]:
    return collection_targets(session, lane=lane, watchlist=watchlist, include_core=False)


def is_monitored(
    session: Session,
    *,
    ats_type: str | None,
    ats_identifier: str | None,
    company_name: str | None = None,
    watchlist: WatchlistLoadResult | None = None,
) -> str | None:
    """``CORE_WATCHLIST``, ``DISCOVERED`` (any polled registry row) or ``None``.

    Board identity wins over the display name — "Acme" and "Acme Business
    Corporation" are one employer if they are one Ashby tenant — but a name
    match still counts, because a company on the watchlist with an unsupported
    ATS is monitored in the sense that matters here: it was already considered.
    """
    ats = (ats_type or "").upper()
    identifier = (ats_identifier or "").strip().lower()
    names = _name_keys(company_name or "")

    core = core_watchlist(watchlist)
    for entry in core.companies:
        if not entry.active:
            continue
        if identifier and entry.ats_type.upper() == ats and (entry.ats_identifier or "").lower() == identifier:
            return CORE
        if names & _name_keys(entry.company_name):
            return CORE

    for company in discovered_companies(session):
        same_board = (
            bool(identifier)
            and (company.ats_type or "").upper() == ats
            and (company.ats_identifier or "").lower() == identifier
        )
        same_name = bool(names & _name_keys(company.company_name))
        if same_board or same_name:
            return DISCOVERED
    return None


# ---------------------------------------------------------------------------
# Writing the registry
# ---------------------------------------------------------------------------


def monitor(
    session: Session,
    *,
    company_name: str,
    ats_type: str,
    ats_identifier: str,
    careers_url: str | None = None,
    watchlist_scope: str = "BOTH",
    notes: str | None = None,
    registry_source: str = RegistrySource.DISCOVERED.value,
    monitoring_status: str = MonitoringStatus.ACTIVE.value,
    discovered_from_job_id: str | None = None,
    connector_config: dict[str, Any] | None = None,
) -> MonitorOutcome:
    """Start polling one employer's board. Explicit, per company, reversible.

    Refuses an ATS no connector implements rather than storing a row that will
    never collect: an entry that silently never runs is worse than a visible
    "this board is not supported yet". Use :func:`record_unsupported` for that.
    """
    name = (company_name or "").strip()
    ats = (ats_type or "").strip().upper()
    identifier = (ats_identifier or "").strip()

    if not name:
        return MonitorOutcome(ok=False, reason="NO_COMPANY_NAME", detail="A company needs a name.")
    if ats not in AUTOMATABLE_ATS:
        return MonitorOutcome(
            ok=False,
            reason="UNSUPPORTED_ATS",
            detail=(
                f"{ats or 'This employer'} is not a source Job Hunt OS can poll. "
                f"Supported: {', '.join(sorted(AUTOMATABLE_ATS))}."
            ),
        )
    if not identifier:
        return MonitorOutcome(
            ok=False,
            reason="NO_BOARD_IDENTIFIER",
            detail=f"No {ats.title()} board could be read from this posting.",
        )
    if monitoring_status not in (MonitoringStatus.ACTIVE.value, MonitoringStatus.EXPERIMENTAL.value):
        return MonitorOutcome(
            ok=False,
            reason="NOT_A_MONITORING_STATUS",
            detail=f"{monitoring_status} does not poll a board; use record_unsupported.",
        )
    # The connector decides what a valid board is. A Workday tenant name on its
    # own, for example, is not one: the site and pod are part of the identity.
    from app.connectors import get_connector

    validation = get_connector(ats).validate_config(
        SourceConfig(company_name=name, ats_type=ats, ats_identifier=identifier)
    )
    if not validation.ok:
        return MonitorOutcome(
            ok=False, reason="INVALID_BOARD", detail="; ".join(validation.problems)
        )

    existing = find_row(session, company_name=name, ats_type=ats, ats_identifier=identifier)
    if existing is not None:
        prior_source = _row_source(existing)
        already = (
            prior_source in DB_SOURCES
            and _row_status(existing) == monitoring_status
            and existing.on_watchlist
        )
        metadata = dict(existing.source_metadata or {})
        metadata[WATCHLIST_SOURCE_KEY] = DISCOVERED
        metadata.setdefault("monitored_since", utcnow().isoformat())
        existing.source_metadata = metadata
        existing.on_watchlist = True
        existing.active = True
        existing.ats_type = ats
        existing.ats_identifier = identifier
        existing.ats_capability = existing.ats_capability or _capability(ats)
        existing.careers_url = existing.careers_url or careers_url
        existing.watchlist_scope = watchlist_scope
        existing.monitoring_status = monitoring_status
        existing.source_verification = SourceVerification.CONFIRMED.value
        # Keep how a registry company first entered; a pipeline-created name
        # row enters now. Read before the metadata marker above was written.
        existing.registry_source = (
            prior_source if prior_source in DB_SOURCES else registry_source
        )
        existing.discovered_from_job_id = existing.discovered_from_job_id or discovered_from_job_id
        if connector_config:
            existing.connector_config = {**(existing.connector_config or {}), **connector_config}
        if notes:
            existing.notes = notes
        session.flush()
        return MonitorOutcome(ok=True, company=existing, created=not already)

    company = Company(
        company_name=name,
        normalized_name=normalize_company(name),
        ats_type=ats,
        ats_identifier=identifier,
        ats_capability=_capability(ats),
        careers_url=careers_url,
        on_watchlist=True,
        watchlist_scope=watchlist_scope,
        active=True,
        notes=notes,
        connector_config=connector_config or None,
        registry_source=registry_source,
        monitoring_status=monitoring_status,
        source_verification=SourceVerification.CONFIRMED.value,
        discovered_from_job_id=discovered_from_job_id,
        source_metadata={
            WATCHLIST_SOURCE_KEY: DISCOVERED,
            "monitored_since": utcnow().isoformat(),
        },
    )
    session.add(company)
    session.flush()
    logger.info("Now monitoring %s (%s/%s)", name, ats, identifier)
    return MonitorOutcome(ok=True, company=company, created=True)


def record_unsupported(
    session: Session,
    *,
    company_name: str,
    careers_url: str | None = None,
    ats_type: str | None = None,
    monitoring_status: str = MonitoringStatus.MANUAL_ONLY.value,
    source_verification: str = SourceVerification.UNKNOWN.value,
    registry_source: str = RegistrySource.USER_ADDED.value,
    notes: str | None = None,
) -> MonitorOutcome:
    """Keep a relevant employer whose careers source cannot be polled.

    Stored as MANUAL_ONLY or VERIFY_LATER, never as monitored, so the coverage
    report counts it as a gap rather than as coverage.
    """
    name = (company_name or "").strip()
    if not name:
        return MonitorOutcome(ok=False, reason="NO_COMPANY_NAME", detail="A company needs a name.")
    if monitoring_status not in (
        MonitoringStatus.MANUAL_ONLY.value,
        MonitoringStatus.VERIFY_LATER.value,
    ):
        return MonitorOutcome(
            ok=False, reason="NOT_AN_UNSUPPORTED_STATUS", detail=f"{monitoring_status} polls."
        )
    existing = find_row(session, company_name=name)
    if existing is not None and _row_status(existing) in (
        MonitoringStatus.ACTIVE.value,
        MonitoringStatus.EXPERIMENTAL.value,
    ):
        return MonitorOutcome(
            ok=False,
            company=existing,
            reason="ALREADY_MONITORED",
            detail=f"{existing.company_name} is already monitored.",
        )
    company = existing or Company(company_name=name, normalized_name=normalize_company(name))
    company.careers_url = company.careers_url or careers_url
    if ats_type:
        company.ats_type = ats_type.upper()
    company.registry_source = _row_source(company) if _row_source(company) in DB_SOURCES else registry_source
    company.monitoring_status = monitoring_status
    company.source_verification = source_verification
    company.on_watchlist = False
    if notes:
        company.notes = notes
    if existing is None:
        session.add(company)
    session.flush()
    return MonitorOutcome(ok=True, company=company, created=existing is None)


def set_monitoring(
    session: Session,
    *,
    status: str,
    company_id: str | None = None,
    company_name: str | None = None,
    watchlist: WatchlistLoadResult | None = None,
) -> MonitorOutcome:
    """Pause or resume one registry company — seed or row.

    Pausing a seed company records the decision on its ``company`` row and
    leaves ``companies.yaml`` alone. Resuming refuses anything whose careers
    source is not CONFIRMED or has no connector: activation is never a guess.
    """
    if status not in (MonitoringStatus.PAUSED.value, MonitoringStatus.ACTIVE.value):
        return MonitorOutcome(ok=False, reason="BAD_STATUS", detail=f"Cannot set {status}.")
    entries = registry_entries(session, watchlist=watchlist)
    entry = next(
        (
            e
            for e in entries
            if (company_id and e.company_id == company_id)
            or (company_name and _name_keys(company_name) & _name_keys(e.company_name))
        ),
        None,
    )
    if entry is None:
        return MonitorOutcome(ok=False, reason="NOT_FOUND", detail="No such company in the registry.")
    if status == MonitoringStatus.ACTIVE.value:
        if not entry.collectable:
            return MonitorOutcome(
                ok=False,
                reason="UNSUPPORTED_ATS",
                detail=f"{entry.company_name} has no careers source Job Hunt OS can poll.",
            )
        if entry.source_verification != SourceVerification.CONFIRMED.value:
            return MonitorOutcome(
                ok=False,
                reason="SOURCE_NOT_CONFIRMED",
                detail=(
                    f"{entry.company_name}'s careers source is "
                    f"{entry.source_verification}. Confirm it before monitoring."
                ),
            )

    row = session.get(Company, entry.company_id) if entry.company_id else None
    if row is None:
        row = _ensure_row(session, entry)
    row.monitoring_status = status
    if not entry.is_core:
        row.on_watchlist = status == MonitoringStatus.ACTIVE.value
    session.flush()
    return MonitorOutcome(ok=True, company=row)


def unmonitor(session: Session, company_id: str) -> MonitorOutcome:
    """Stop polling a registry company. The core seed file is not touched.

    The row survives — paused — because jobs already collected point at it and
    deleting it would orphan them. Seed companies are paused through
    :func:`set_monitoring`; this older entry point still refuses them.
    """
    company = session.get(Company, company_id)
    if company is None:
        return MonitorOutcome(ok=False, reason="NOT_FOUND", detail="No such company.")
    if _row_source(company) not in DB_SOURCES:
        return MonitorOutcome(
            ok=False,
            reason="CORE_WATCHLIST",
            detail=(
                "This company is on the curated watchlist in companies.yaml. "
                "Pause it instead — the application does not edit that file."
            ),
        )
    company.on_watchlist = False
    company.monitoring_status = MonitoringStatus.PAUSED.value
    session.flush()
    return MonitorOutcome(ok=True, company=company)


def record_poll(
    session: Session,
    config: SourceConfig,
    *,
    ok: bool,
    capability_failure: bool = False,
    detail: str = "",
    jobs_seen: int | None = None,
    jobs_new: int | None = None,
) -> Company | None:
    """Write one poll's outcome onto the company's registry row.

    A failure is recorded as a failure — including a board that answered with
    errors and no records, which previously left no trace on the company.
    """
    company = find_row(
        session,
        company_name=config.company_name,
        ats_type=config.ats_type,
        ats_identifier=config.ats_identifier,
        company_id=config.company_id,
    )
    if company is None:
        if config.is_fixture:
            return None
        company = Company(
            company_name=config.company_name,
            normalized_name=normalize_company(config.company_name),
            registry_source=config.registry_source,
        )
        session.add(company)
    if config.registry_source == RegistrySource.CORE_SEED.value and not company.registry_source:
        company.registry_source = RegistrySource.CORE_SEED.value
    now = utcnow()
    company.last_validated_at = now
    company.last_health_detail = detail[:2000]
    if ok:
        company.last_health_status = "OK"
        company.last_successful_collection_at = now
        company.last_jobs_seen = jobs_seen
        company.last_new_jobs = jobs_new
    else:
        company.last_health_status = "UNAVAILABLE" if capability_failure else "FAILED"
        company.last_failure_at = now
    session.flush()
    return company


# ---------------------------------------------------------------------------
# Lookup and identity
# ---------------------------------------------------------------------------


def find_row(
    session: Session,
    *,
    company_name: str | None = None,
    ats_type: str | None = None,
    ats_identifier: str | None = None,
    company_id: str | None = None,
) -> Company | None:
    """The company row for an employer: id, then board, then name.

    Board identity outranks the name, so two employers with similar names but
    different boards are never merged, and one board under two spellings of a
    name is one company. Name matching uses both the pipeline's normaliser
    ("Brightpath, Inc." → "brightpath") and a squashed form ("Bright Path" → "brightpath").
    """
    if company_id:
        row = session.get(Company, company_id)
        if row is not None:
            return row
    rows = session.execute(select(Company)).scalars().all()
    if ats_type and ats_identifier:
        key = board_key(ats_type, ats_identifier)
        for row in rows:
            if row.ats_type and row.ats_identifier and board_key(row.ats_type, row.ats_identifier) == key:
                return row
    names = _name_keys(company_name or "")
    if not names:
        return None
    matches = [r for r in rows if names & _name_keys(r.company_name)]
    if ats_type and ats_identifier:
        # A name match must never land on a row that already names a
        # DIFFERENT board — that is another monitoring target.
        matches = [r for r in matches if not r.ats_identifier]
    # Registry rows before pipeline-created name rows.
    matches.sort(key=lambda r: _row_source(r) is None)
    return matches[0] if matches else None


def board_key(ats_type: str, ats_identifier: str) -> tuple[str, str]:
    return ((ats_type or "").upper(), (ats_identifier or "").strip().lower())


def _name_keys(name: str) -> set[str]:
    base = normalize_company(name or "")
    if not base:
        return set()
    return {base, base.replace(" ", "").replace("-", "")}


def _row_source(row: Company) -> str | None:
    if row.registry_source:
        return row.registry_source
    if (row.source_metadata or {}).get(WATCHLIST_SOURCE_KEY) == DISCOVERED:
        return RegistrySource.DISCOVERED.value
    return None


def _row_status(row: Company) -> str:
    if row.monitoring_status:
        return row.monitoring_status
    if _row_source(row) == RegistrySource.DISCOVERED.value:
        return (
            MonitoringStatus.ACTIVE.value
            if row.on_watchlist and row.active
            else MonitoringStatus.PAUSED.value
        )
    return MonitoringStatus.VERIFY_LATER.value


def _overlay_state(entry: RegistryEntry, row: Company | None) -> None:
    if row is None:
        return
    entry.last_successful_collection_at = row.last_successful_collection_at
    entry.last_failure_at = row.last_failure_at
    entry.last_failure_reason = (
        row.last_health_detail if row.last_health_status in ("FAILED", "UNAVAILABLE") else None
    )
    entry.last_jobs_seen = row.last_jobs_seen
    entry.last_new_jobs = row.last_new_jobs


def _ensure_row(session: Session, entry: RegistryEntry) -> Company:
    row = find_row(
        session,
        company_name=entry.company_name,
        ats_type=entry.ats_type,
        ats_identifier=entry.ats_identifier,
    )
    if row is None:
        row = Company(
            company_name=entry.company_name,
            normalized_name=normalize_company(entry.company_name),
        )
        session.add(row)
    row.registry_source = row.registry_source or entry.registry_source
    session.flush()
    return row


def _capability(ats_type: str) -> str:
    return "UNDOCUMENTED_PUBLIC_ENDPOINT" if ats_type == "WORKDAY" else "DOCUMENTED_PUBLIC_API"


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def summary(session: Session, *, watchlist: WatchlistLoadResult | None = None) -> dict[str, Any]:
    """Counts for the collection page. Cheap: no network, no connector."""
    core = core_watchlist(watchlist)
    targets = collection_targets(session, watchlist=core)
    core_targets = [t for t in targets if t.registry_source == RegistrySource.CORE_SEED.value]
    return {
        "core_watchlist": len(core.watchlist_companies),
        "core_automated": len(core_targets),
        "core_manual_only": len(core.manual_only_companies),
        "discovered_monitored": len(targets) - len(core_targets),
        "total_polled": len(targets),
    }


def coverage(
    session: Session,
    *,
    watchlist: WatchlistLoadResult | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Coverage of the registry — the number that matters more than job count.

    Every figure is derived from registry state and recorded polls; a board
    that has never run counts as NOT_YET_COLLECTED, never as healthy.
    """
    now = now or utcnow()
    entries = registry_entries(session, watchlist=watchlist)
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_health: dict[str, int] = {}
    providers: dict[str, dict[str, int]] = {}
    for entry in entries:
        by_status[entry.monitoring_status] = by_status.get(entry.monitoring_status, 0) + 1
        by_source[entry.registry_source] = by_source.get(entry.registry_source, 0) + 1
        health = entry.health(now)
        by_health[health] = by_health.get(health, 0) + 1
        bucket = providers.setdefault(
            entry.ats_type,
            {"registry": 0, "monitored": 0, HEALTHY: 0, STALE: 0, FAILING: 0, NOT_YET_COLLECTED: 0},
        )
        bucket["registry"] += 1
        if entry.monitoring_status == MonitoringStatus.ACTIVE.value and entry.collectable:
            bucket["monitored"] += 1
            if health in bucket:
                bucket[health] += 1

    monitored = [
        e for e in entries if e.monitoring_status == MonitoringStatus.ACTIVE.value and e.collectable
    ]
    failed_recently = [
        e.company_name
        for e in entries
        if e.last_failure_at is not None
        and now - (as_utc(e.last_failure_at) or now) <= timedelta(hours=STALE_AFTER_HOURS)
    ]
    return {
        "total": len(entries),
        "monitored": len(monitored),
        "healthy": sum(1 for e in monitored if e.health(now) == HEALTHY),
        "stale": sum(1 for e in monitored if e.health(now) == STALE),
        "failing": sum(1 for e in monitored if e.health(now) == FAILING),
        "not_yet_collected": sum(1 for e in monitored if e.health(now) == NOT_YET_COLLECTED),
        "paused": by_status.get(MonitoringStatus.PAUSED.value, 0),
        "experimental": by_status.get(MonitoringStatus.EXPERIMENTAL.value, 0),
        "manual_only": by_status.get(MonitoringStatus.MANUAL_ONLY.value, 0),
        "verify_later": by_status.get(MonitoringStatus.VERIFY_LATER.value, 0),
        "supported_provider": sum(1 for e in entries if e.collectable),
        "unknown_ats": sum(1 for e in entries if e.ats_type in ("", "UNKNOWN")),
        "failed_recently": sorted(failed_recently),
        "by_monitoring_status": dict(sorted(by_status.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_health": dict(sorted(by_health.items())),
        "by_provider": dict(sorted(providers.items())),
    }
