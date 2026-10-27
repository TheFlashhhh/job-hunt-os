"""Collection orchestration.

Runs connectors, stages what they return, and hands it to the existing
pipeline. Four properties matter more than throughput:

**One company's failure is not the run's failure.** A watchlist of fifty
companies will always contain a board that is down, renamed, or newly gated.
Each company is isolated; the run ends ``PARTIAL`` with the failures recorded,
not ``FAILED`` with forty-nine successful collections thrown away.

**Everything is staged before it is normalized.** Connector output becomes a
``RawJobSourceRecord`` first, so a parser change can be replayed without asking
the source again.

**The content-hash gate is not bypassed.** A nightly poll of an unchanged board
must cost nothing: unchanged postings update ``last_seen`` and stop.

**Fixtures are inert.** A fixture board's jobs are flagged on the way in and
never reach a real KPI.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors import get_connector
from app.connectors.base import CollectionResult, HealthResult, JobSourceConnector, SourceConfig
from app.models.base import utcnow
from app.models.enums import (
    CollectionRunStatus,
    DiscoveryChannel,
    JobLifecycleStatus,
)
from app.models.ingestion import CollectionRun, Company, Job, JobSourceAssociation
from app.services import monitored_companies
from app.services.pipeline import JobPipeline
from app.services.watchlist import WatchlistLoadResult, load_watchlist

logger = logging.getLogger("jobhunt.collection")

COLLECTOR_VERSION = "0.3.0"

#: Ingested records between commits. See ``commit_every_records``. Was 25:
#: during a first poll every record is a new job analysed on the way in, and a
#: 25-record batch held the lock for up to 13.5 s (measured 2026-10-16, 200-board
#: sweep, p99 8.7 s). Ten bounds a waiting write to a few seconds; the extra
#: commits are cheap.
COMMIT_EVERY_RECORDS = 10

#: Wall-clock ceiling on one board's fetch. The slowest board measured took
#: ~4 min (a first Workday poll); a request that hangs past this is abandoned.
BOARD_DEADLINE_SECONDS = 15 * 60

#: Pause after each commit so a waiting writer can take the lock. See
#: ``CollectionService._yield_write_lock``.
YIELD_AFTER_COMMIT_SECONDS = 0.05

#: Successful polls that must miss a job before it is even *suspected* closed.
DEFAULT_MISSES_BEFORE_SUSPECT = 2
#: Further successful polls before it is treated as closed.
DEFAULT_MISSES_BEFORE_CLOSED = 5


@dataclass
class CompanyOutcome:
    """What happened for one company in one run."""

    config: SourceConfig
    status: str = CollectionRunStatus.COMPLETED.value
    records_received: int = 0
    records_valid: int = 0
    jobs_new: int = 0
    jobs_changed: int = 0
    jobs_unchanged: int = 0
    jobs_failed: int = 0
    jobs_screened_out: int = 0
    errors: list[str] = field(default_factory=list)
    capability_failure: bool = False
    http_stats: dict[str, Any] = field(default_factory=dict)
    #: Wall-clock seconds for this company. Recorded so a slow or hanging
    #: source is visible rather than inferred.
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == CollectionRunStatus.COMPLETED.value


@dataclass
class RunSummary:
    """Aggregate across every company in one invocation."""

    outcomes: list[CompanyOutcome] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.outcomes:
            return CollectionRunStatus.SKIPPED.value
        if all(o.ok for o in self.outcomes):
            return CollectionRunStatus.COMPLETED.value
        if any(o.ok for o in self.outcomes):
            return CollectionRunStatus.PARTIAL.value
        return CollectionRunStatus.FAILED.value

    def total(self, field_name: str) -> int:
        return sum(getattr(o, field_name) for o in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "companies": len(self.outcomes),
            "records_received": self.total("records_received"),
            "records_valid": self.total("records_valid"),
            "jobs_new": self.total("jobs_new"),
            "jobs_changed": self.total("jobs_changed"),
            "jobs_unchanged": self.total("jobs_unchanged"),
            "jobs_failed": self.total("jobs_failed"),
            "jobs_screened_out": self.total("jobs_screened_out"),
            "capability_failures": [
                o.config.company_name for o in self.outcomes if o.capability_failure
            ],
            "errors": [e for o in self.outcomes for e in o.errors],
            "skipped": self.skipped,
            "run_ids": self.run_ids,
        }


class CollectionService:
    """Drives connectors and feeds the existing ingestion pipeline."""

    def __init__(
        self,
        session: Session,
        *,
        connector_factory: Any = get_connector,
        misses_before_suspect: int = DEFAULT_MISSES_BEFORE_SUSPECT,
        misses_before_closed: int = DEFAULT_MISSES_BEFORE_CLOSED,
        triggered_by: str = "cli",
    ) -> None:
        self.session = session
        self.pipeline = JobPipeline(session)
        self._connector_factory = connector_factory
        self.misses_before_suspect = misses_before_suspect
        self.misses_before_closed = misses_before_closed
        #: Recorded on every run row. "cli", "browser", "scheduler" — so a
        #: stale corpus can be traced to who was (not) triggering collection.
        self.triggered_by = triggered_by
        #: Commit every this many ingested records. Committing only between
        #: companies is not enough on its own: one large board — Datadog's
        #: Greenhouse feed returns hundreds of postings, each analyzed on the
        #: way in — holds the write lock for minutes, which is far longer than
        #: any caller's busy timeout. Bounding the transaction to a batch keeps
        #: the rest of the application writable throughout a sweep.
        self.commit_every_records = COMMIT_EVERY_RECORDS
        self.yield_after_commit_seconds = YIELD_AFTER_COMMIT_SECONDS
        self.board_deadline_seconds: float = BOARD_DEADLINE_SECONDS
        #: Commit (and so release the write lock) at the sweep's natural
        #: boundaries. Off only when the caller owns an atomic transaction.
        self.commit_during_sweep = True

    # -- entry points ----------------------------------------------------
    def collect_watchlist(
        self,
        *,
        lane: str | None = None,
        include_fixtures: bool = False,
        only_fixtures: bool = False,
        company_name: str | None = None,
        ats_type: str | None = None,
        limit: int | None = None,
        analyze: bool = True,
        dry_run: bool = False,
        watchlist: WatchlistLoadResult | None = None,
        include_discovered: bool = True,
        include_experimental: bool = False,
        commit_between_companies: bool = True,
    ) -> RunSummary:
        """Collect from the configured watchlist, plus any company the user
        chose to monitor from the browser.

        The curated file and the discovered set are two populations of one
        search universe (``monitored_companies``). They are collected the same
        way — a board found by pasting a URL is not a second-class source, it
        is just an employer nobody had put on the list yet.

        **Each company is committed as it finishes.** SQLite allows one writer,
        and the last full sweep took fifty-one minutes: holding a single
        transaction across it meant that while collection ran, nothing else in
        the application could write. Adding a contact during a sweep failed with
        "database is locked" — which is exactly the moment a user is most likely
        to be doing something else, since the collection was started from the
        browser and takes a coffee break to finish.

        Committing per company is safe because that is already the isolation
        boundary: one company's failure never invalidates another's, and a
        re-run of an unchanged board writes nothing. Pass
        ``commit_between_companies=False`` when the caller owns the transaction
        and wants the whole sweep to be atomic.

        Targets come from the company registry (``monitored_companies``): the
        curated seed, then registry companies, minus anything paused or on a
        provider whose rollout switch is off. An EXPERIMENTAL canary is polled
        only when named (``company_name``) or ``include_experimental`` is set.
        """
        loaded = watchlist or load_watchlist()
        summary = RunSummary()
        self.commit_during_sweep = commit_between_companies

        if only_fixtures:
            targets = list(loaded.fixtures)
        else:
            targets = monitored_companies.collection_targets(
                self.session,
                lane=lane,
                watchlist=loaded,
                include_registry=include_discovered,
                include_experimental=include_experimental or bool(company_name),
            )
            if include_fixtures:
                targets = targets + list(loaded.fixtures)

        if company_name:
            needle = company_name.strip().lower()
            targets = [t for t in targets if needle in t.company_name.lower()]
        if ats_type:
            wanted = ats_type.strip().upper()
            targets = [t for t in targets if t.ats_type == wanted]

        if not targets:
            summary.skipped.append(
                "No matching active companies. Real companies stay inactive until "
                "approved; use --fixtures to exercise connectors."
            )
            return summary

        for config in targets:
            summary.outcomes.append(
                self.collect_company(
                    config, limit=limit, analyze=analyze, dry_run=dry_run, summary=summary
                )
            )
            if commit_between_companies:
                # Release SQLite's single write lock between boards so the rest
                # of the application stays usable while a sweep runs.
                self.session.commit()
                self._yield_write_lock()
        return summary

    def collect_company(
        self,
        config: SourceConfig,
        *,
        limit: int | None = None,
        analyze: bool = True,
        dry_run: bool = False,
        summary: RunSummary | None = None,
    ) -> CompanyOutcome:
        """Collect one company. Never raises for a source-side failure."""
        import time

        started = time.monotonic()
        outcome = CompanyOutcome(config=config)

        try:
            connector = self._connector_factory(config.ats_type)
        except ValueError as exc:
            outcome.status = CollectionRunStatus.FAILED.value
            outcome.errors.append(str(exc))
            return outcome

        run = self._start_run(connector, config)
        if summary is not None:
            summary.run_ids.append(run.collection_run_id)
        if self.commit_during_sweep:
            # Inserting the run row opened a write transaction. The fetch below
            # is network time — one request for Greenhouse, but dozens for a
            # SmartRecruiters board and well over a hundred for a first Workday
            # poll — and holding SQLite's only write lock across it is what
            # left a CRM save mid-sweep failing with "database is locked"
            # after its full 30-second wait (measured 2026-10-16: 6 of 53 probe
            # writes during a 100-board sweep). The row is complete enough to
            # persist now; it is finished after the fetch.
            self.session.commit()

        try:
            known = (
                self._known_external_ids(connector.connector_id, config)
                if getattr(connector, "reuses_known_postings", False)
                else None
            )
            result = self._fetch_with_deadline(connector, config, limit=limit, known=known)
            outcome.records_received = result.records_received
            outcome.records_valid = result.records_valid
            outcome.http_stats = result.http_stats
            outcome.capability_failure = result.capability_failure
            outcome.errors.extend(result.errors)

            if result.capability_failure:
                # Not an outage: this company is not publicly available.
                outcome.status = CollectionRunStatus.SKIPPED.value
                self._record_company_health(
                    config, reachable=False, detail="; ".join(result.errors)
                )
            elif result.errors and not result.records and not result.observed_external_ids:
                outcome.status = CollectionRunStatus.FAILED.value
                self._record_company_health(
                    config, reachable=False, detail="; ".join(result.errors), failed=True
                )
            else:
                if dry_run:
                    outcome.status = CollectionRunStatus.SKIPPED.value
                    outcome.errors.append(
                        f"Dry run: {result.records_valid} record(s) fetched, nothing written."
                    )
                else:
                    self._ingest(result, outcome, analyze=analyze, run=run)
                    self._confirm_observed(config, connector.connector_id, result, outcome)
                    self._reconcile_lifecycle(config, result, outcome)
                    self._record_company_health(
                        config,
                        reachable=True,
                        detail=(
                            "Collected."
                            if result.listing_complete
                            else "Collected; the listing was cut short, so no posting "
                            "was aged out this run."
                        ),
                        jobs_seen=result.records_received,
                        jobs_new=outcome.jobs_new,
                    )
                    if outcome.errors:
                        outcome.status = CollectionRunStatus.PARTIAL.value
        except Exception as exc:  # noqa: BLE001 — one company must not end the run
            logger.exception("Collection failed for %s", config.label)
            outcome.status = CollectionRunStatus.FAILED.value
            outcome.errors.append(f"Unexpected failure: {exc}")
        finally:
            outcome.duration_seconds = round(time.monotonic() - started, 2)
            connector.close()
            self._finish_run(run, outcome)

        return outcome

    # -- ingestion -------------------------------------------------------
    def _ingest(
        self,
        result: CollectionResult,
        outcome: CompanyOutcome,
        *,
        analyze: bool,
        run: CollectionRun,
    ) -> None:
        seen_external_ids: list[str] = []
        since_commit = 0

        for record in result.records:
            try:
                ingest = self.pipeline.ingest(record, collection_run=run, analyze=analyze)
            except Exception as exc:  # noqa: BLE001
                outcome.jobs_failed += 1
                outcome.errors.append(f"Ingestion failed for {record.title!r}: {exc}")
                continue

            if record.external_id:
                seen_external_ids.append(record.external_id)

            if ingest.job is None:
                outcome.jobs_failed += 1
                continue
            if ingest.created:
                outcome.jobs_new += 1
            elif ingest.unchanged:
                outcome.jobs_unchanged += 1
            else:
                outcome.jobs_changed += 1

            if ingest.job.screen_status in ("REJECTED", "ARCHIVED"):
                outcome.jobs_screened_out += 1

            # A job returned by the source is unambiguously open.
            ingest.job.consecutive_misses = 0
            ingest.job.last_confirmed_open_at = utcnow()
            if ingest.job.lifecycle_status in (
                JobLifecycleStatus.POSSIBLY_CLOSED.value,
                JobLifecycleStatus.DISAPPEARED.value,
            ):
                ingest.job.lifecycle_status = JobLifecycleStatus.OPEN.value

            since_commit += 1
            if self.commit_every_records and since_commit >= self.commit_every_records:
                # Each record's ingestion is complete and idempotent at this
                # point, so a commit here loses nothing and hands the write
                # lock back to whatever else the user is doing.
                self.session.commit()
                self._yield_write_lock()
                since_commit = 0

        self._seen_ids = seen_external_ids
        self.session.flush()

    def _fetch_with_deadline(
        self,
        connector: JobSourceConnector,
        config: SourceConfig,
        *,
        limit: int | None,
        known: set[str] | None,
    ) -> CollectionResult:
        """Run the connector's fetch with a wall-clock ceiling.

        httpx's timeout bounds each socket operation, not a request: on
        2026-10-16 a Workday request sat on an open TLS connection for an hour
        with no timeout firing, and the whole sweep — and its lock — waited
        with it. The fetch touches no database session, so it runs on a worker
        thread; past the deadline the board is recorded as a failure (never as
        a disappearance), its client is closed to abort the socket, and the
        sweep moves on.
        """
        import threading

        box: dict[str, Any] = {}

        def run() -> None:
            try:
                if known is not None:
                    box["result"] = connector.collect(config, limit=limit, known_external_ids=known)
                else:
                    box["result"] = connector.collect(config, limit=limit)
            except BaseException as exc:  # noqa: BLE001 — re-raised on the sweep's thread
                box["error"] = exc

        worker = threading.Thread(target=run, name=f"fetch-{config.company_name}", daemon=True)
        worker.start()
        worker.join(self.board_deadline_seconds)
        if worker.is_alive():
            logger.error(
                "%s exceeded the %.0fs board deadline; abandoned", config.label, self.board_deadline_seconds
            )
            # Best effort: the thread is abandoned either way.
            with contextlib.suppress(Exception):
                connector.close()
            return CollectionResult(
                config=config,
                errors=[
                    f"Timed out: the board did not finish within {self.board_deadline_seconds:.0f}s "
                    "and was skipped this run."
                ],
                listing_complete=False,
            )
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _yield_write_lock(self) -> None:
        """Give a waiting writer a real chance at the lock.

        Committing releases SQLite's write lock, but SQLite has no queue for it:
        a waiting writer's busy handler sleeps and retries, and a sweep that
        starts its next write the instant it commits takes the lock back during
        that sleep. Measured on 2026-10-16: with commits every 25 records, a
        write issued mid-sweep still waited out its whole 30-second busy timeout
        and failed with "database is locked" — short transactions, starved
        writer. A pause longer than the busy handler's retry interval lets the
        waiting write in; at 50 ms per commit it adds seconds to a sweep.
        """
        if self.yield_after_commit_seconds > 0:
            import time

            time.sleep(self.yield_after_commit_seconds)

    # -- lifecycle -------------------------------------------------------
    def _reconcile_lifecycle(
        self, config: SourceConfig, result: CollectionResult, outcome: CompanyOutcome
    ) -> None:
        """Age out jobs the source stopped returning.

        Only runs after a *successful* collection. A source that errored tells
        us nothing about which jobs still exist, and treating its silence as
        closure would quietly delete a live opportunity.
        """
        if result.errors or result.capability_failure or not result.listing_complete:
            return

        returned = {r.external_id for r in result.records if r.external_id}
        returned |= result.observed_external_ids
        rows = (
            self.session.execute(
                select(Job).where(
                    Job.company_name == config.company_name,
                    Job.discovery_source == config.ats_type,
                )
            )
            .scalars()
            .all()
        )

        for job in rows:
            if job.ats_job_id and job.ats_job_id in returned:
                continue
            if job.lifecycle_status == JobLifecycleStatus.CLOSED.value:
                continue

            job.consecutive_misses = (job.consecutive_misses or 0) + 1
            if job.consecutive_misses >= self.misses_before_closed:
                job.lifecycle_status = JobLifecycleStatus.CLOSED.value
                job.closed_at = job.closed_at or utcnow()
            elif job.consecutive_misses >= self.misses_before_suspect:
                job.lifecycle_status = JobLifecycleStatus.POSSIBLY_CLOSED.value
                job.disappeared_at = job.disappeared_at or utcnow()
        self.session.flush()

    # -- run bookkeeping -------------------------------------------------
    def _start_run(self, connector: JobSourceConnector, config: SourceConfig) -> CollectionRun:
        run = CollectionRun(
            connector=connector.connector_id,
            source=config.ats_identifier,
            discovery_channel=DiscoveryChannel.A_ATS_WATCHLIST.value,
            company_name=config.company_name,
            company_id=config.company_id,
            source_capability=connector.capability.value,
            is_fixture=config.is_fixture,
            status=CollectionRunStatus.STARTED.value,
            connector_version=connector.version,
            triggered_by=self.triggered_by,
            # Structured public sources cost nothing. Only Apify changes this.
            estimated_cost_usd=0.0,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def _finish_run(self, run: CollectionRun, outcome: CompanyOutcome) -> None:
        run.status = outcome.status
        run.completed_at = utcnow()
        run.records_received = outcome.records_received
        run.records_valid = outcome.records_valid
        run.jobs_seen = outcome.records_valid
        run.jobs_new = outcome.jobs_new
        run.jobs_changed = outcome.jobs_changed
        run.jobs_unchanged = outcome.jobs_unchanged
        run.jobs_failed = outcome.jobs_failed
        run.http_stats = outcome.http_stats or None
        run.error_summary = "\n".join(outcome.errors[:20]) or None
        if outcome.ok:
            run.last_successful_run = run.completed_at
        run.last_attempted_run = run.completed_at
        self.session.flush()

    def _record_company_health(
        self,
        config: SourceConfig,
        *,
        reachable: bool,
        detail: str,
        failed: bool = False,
        jobs_seen: int | None = None,
        jobs_new: int | None = None,
    ) -> None:
        monitored_companies.record_poll(
            self.session,
            config,
            ok=reachable,
            capability_failure=not reachable and not failed,
            detail=detail,
            jobs_seen=jobs_seen,
            jobs_new=jobs_new,
        )

    def _find_company(self, config: SourceConfig) -> Company | None:
        return monitored_companies.find_row(
            self.session,
            company_name=config.company_name,
            ats_type=config.ats_type,
            ats_identifier=config.ats_identifier,
            company_id=config.company_id,
        )

    # -- postings known before this poll ----------------------------------
    def _known_external_ids(self, connector_id: str, config: SourceConfig) -> set[str]:
        """Source ids this connector has already stored for this company."""
        rows = self.session.execute(
            select(JobSourceAssociation.source_external_id)
            .join(Job, Job.job_id == JobSourceAssociation.job_id)
            .where(
                JobSourceAssociation.connector == connector_id,
                JobSourceAssociation.source_external_id.is_not(None),
                Job.company_name == config.company_name,
            )
        ).all()
        return {str(external_id) for (external_id,) in rows}

    def _confirm_observed(
        self,
        config: SourceConfig,
        connector_id: str,
        result: CollectionResult,
        outcome: CompanyOutcome,
    ) -> None:
        """Mark listed-but-not-refetched postings as seen and open.

        The board listed them, which is all "still open" means. Their bodies
        were not re-read, so they count as unchanged — the same verdict the
        content-hash gate would reach for an unedited posting.
        """
        observed = result.observed_external_ids - {
            r.external_id for r in result.records if r.external_id
        }
        if not observed:
            return
        now = utcnow()
        jobs = (
            self.session.execute(
                select(Job)
                .join(JobSourceAssociation, JobSourceAssociation.job_id == Job.job_id)
                .where(
                    JobSourceAssociation.connector == connector_id,
                    JobSourceAssociation.source_external_id.in_(observed),
                    Job.company_name == config.company_name,
                )
            )
            .scalars()
            .unique()
            .all()
        )
        for job in jobs:
            job.consecutive_misses = 0
            job.last_confirmed_open_at = now
            job.last_seen = now
            if job.lifecycle_status in (
                JobLifecycleStatus.POSSIBLY_CLOSED.value,
                JobLifecycleStatus.DISAPPEARED.value,
            ):
                job.lifecycle_status = JobLifecycleStatus.OPEN.value
        outcome.jobs_unchanged += len(jobs)
        self.session.flush()

    # -- health ----------------------------------------------------------
    def check_sources(
        self, *, watchlist: WatchlistLoadResult | None = None, include_fixtures: bool = True
    ) -> list[dict[str, Any]]:
        """One cheap read per configured source. Ingests nothing."""
        loaded = watchlist or load_watchlist()
        targets = list(loaded.companies) + (list(loaded.fixtures) if include_fixtures else [])

        out: list[dict[str, Any]] = []
        for config in targets:
            row: dict[str, Any] = {
                "company": config.company_name,
                "ats_type": config.ats_type,
                "ats_identifier": config.ats_identifier,
                "active": config.active,
                "is_fixture": config.is_fixture,
                "watchlist_scope": config.watchlist_scope,
            }
            try:
                connector = self._connector_factory(config.ats_type)
            except ValueError as exc:
                row.update({"capability": "UNSUPPORTED", "health": {"status": str(exc)}})
                out.append(row)
                continue

            row["capability"] = connector.capability.value
            validation = connector.validate_config(config)
            row["config_valid"] = validation.ok
            row["config_problems"] = validation.problems

            if not validation.ok:
                row["health"] = HealthResult(
                    reachable=False, status="INVALID_CONFIG", detail="; ".join(validation.problems)
                ).as_dict()
            else:
                row["health"] = connector.health_check(config).as_dict()
            connector.close()

            company = self._find_company(config)
            row["last_successful_collection_at"] = (
                company.last_successful_collection_at.isoformat()
                if company and company.last_successful_collection_at
                else None
            )
            row["last_failure_at"] = (
                company.last_failure_at.isoformat() if company and company.last_failure_at else None
            )
            out.append(row)
        return out
