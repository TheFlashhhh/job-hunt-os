"""How fresh is this, when did it last run, and what is it watching?

Every number here is derived from ``collection_run`` rows and the monitored
company set. Nothing is estimated, and nothing is read from a chat window: if
the system cannot tell when something last succeeded, it says ``None`` rather
than guessing a plausible timestamp.

The one thing this module refuses to do is imply a schedule that does not
exist. A cadence written in ``collection_policy.yaml`` is an intent, and intent
is reported as intent: no next-run time is ever projected from one.

``next_scheduled_at`` comes from the operating system's scheduler
(``schedule_state.py``), which is where a registered task actually lives. It
used to come from a ``system_task`` column nothing ever wrote, so a task that
was registered and due to run reported "Not scheduled" — D-45.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.connectors import CONNECTORS, DEFERRED_SOURCES
from app.models.base import as_utc, utcnow
from app.models.enums import CollectionRunStatus, JobLifecycleStatus
from app.models.ingestion import CollectionRun, Job
from app.models.misc import SystemTask
from app.services.monitored_companies import summary as monitored_summary
from app.services.schedule_state import ScheduleState, read_schedule_state
from app.services.watchlist import WatchlistLoadResult, load_watchlist

POLICY_NAME = "collection_policy.yaml"

#: The ``system_task`` name structured collection records itself under. One row,
#: updated in place, so "is a collection running right now" survives a restart.
STRUCTURED_TASK = "STRUCTURED_ATS_REFRESH"

FRESH = "FRESH"
AGING = "AGING"
STALE = "STALE"
NEVER = "NEVER_COLLECTED"


def load_policy() -> dict[str, Any]:
    return load_yaml_config(POLICY_NAME)


@dataclass
class ProviderStatus:
    """One ATS connector: is it implemented, is it used, when did it last work."""

    provider: str
    implemented: bool
    monitored_companies: int
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_status: str | None
    jobs_collected: int
    note: str = ""
    enabled: bool = True
    healthy_boards: int = 0
    stale_boards: int = 0
    failing_boards: int = 0
    never_collected_boards: int = 0
    open_jobs: int = 0
    new_jobs_24h: int = 0


@dataclass
class SweepSummary:
    """The last GROUP of runs, not the last company.

    The watchlist scale-out ran in checkpointed tiers, so a single
    ``collection_run`` row is one company. Reporting that as "the last run"
    would say 60 jobs were seen when 52 boards were polled.
    """

    started_at: datetime | None = None
    completed_at: datetime | None = None
    companies: int = 0
    jobs_new: int = 0
    jobs_changed: int = 0
    jobs_unchanged: int = 0
    jobs_failed: int = 0
    failures: list[str] = field(default_factory=list)
    duration_seconds: float | None = None
    triggered_by: str = ""
    #: Jobs the sweep's own lifecycle reconciliation moved off OPEN.
    closure_candidates: int = 0


@dataclass
class CollectionStatus:
    last_successful_at: datetime | None
    freshness: str
    hours_since_success: float | None
    interval_hours: float
    next_scheduled_at: datetime | None
    #: The scheduler's own state, e.g. REGISTERED_NEXT_RUN_KNOWN. Never
    #: NOT_SCHEDULED for a task that exists — those are different facts.
    schedule_source: str
    schedule: ScheduleState
    run_in_progress: bool
    monitored: dict[str, Any]
    providers: list[ProviderStatus]
    latest_sweep: SweepSummary
    broad_discovery: dict[str, Any]
    corpus: dict[str, Any]
    cadences: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_successful_at": _iso(self.last_successful_at),
            "freshness": self.freshness,
            "hours_since_success": self.hours_since_success,
            "interval_hours": self.interval_hours,
            "next_scheduled_at": _iso(self.next_scheduled_at),
            "schedule_source": self.schedule_source,
            "schedule": self.schedule.as_dict(),
            "run_in_progress": self.run_in_progress,
            "monitored": self.monitored,
            "providers": [
                {
                    "provider": p.provider,
                    "implemented": p.implemented,
                    "monitored_companies": p.monitored_companies,
                    "last_success_at": _iso(p.last_success_at),
                    "last_attempt_at": _iso(p.last_attempt_at),
                    "last_status": p.last_status,
                    "jobs_collected": p.jobs_collected,
                    "note": p.note,
                    "enabled": p.enabled,
                    "healthy_boards": p.healthy_boards,
                    "stale_boards": p.stale_boards,
                    "failing_boards": p.failing_boards,
                    "never_collected_boards": p.never_collected_boards,
                    "open_jobs": p.open_jobs,
                    "new_jobs_24h": p.new_jobs_24h,
                }
                for p in self.providers
            ],
            "latest_sweep": {
                "started_at": _iso(self.latest_sweep.started_at),
                "completed_at": _iso(self.latest_sweep.completed_at),
                "companies": self.latest_sweep.companies,
                "jobs_new": self.latest_sweep.jobs_new,
                "jobs_changed": self.latest_sweep.jobs_changed,
                "jobs_unchanged": self.latest_sweep.jobs_unchanged,
                "jobs_failed": self.latest_sweep.jobs_failed,
                "failures": self.latest_sweep.failures,
                "duration_seconds": self.latest_sweep.duration_seconds,
                "triggered_by": self.latest_sweep.triggered_by,
                "closure_candidates": self.latest_sweep.closure_candidates,
            },
            "broad_discovery": self.broad_discovery,
            "corpus": self.corpus,
            "cadences": self.cadences,
        }


class CollectionStatusService:
    def __init__(
        self,
        session: Session,
        *,
        policy: dict[str, Any] | None = None,
        watchlist: WatchlistLoadResult | None = None,
        schedule_query: Any = None,
    ) -> None:
        self.session = session
        self.policy = policy or load_policy()
        self.watchlist = watchlist or load_watchlist()
        #: Injected in tests. Real scheduler state comes from the OS, which CI
        #: does not have.
        self._schedule_query = schedule_query

    # -- public ----------------------------------------------------------
    def status(self) -> CollectionStatus:
        runs = self._real_runs()
        last_success = self._last_success(runs)
        interval = self._interval_hours()
        hours = None
        if last_success is not None:
            hours = (utcnow() - last_success).total_seconds() / 3600.0

        task = self.session.get(SystemTask, STRUCTURED_TASK)
        schedule = self._next_scheduled()
        sweep = self._latest_sweep(runs)

        return CollectionStatus(
            last_successful_at=last_success,
            freshness=self._freshness(hours, interval),
            hours_since_success=round(hours, 2) if hours is not None else None,
            interval_hours=interval,
            next_scheduled_at=schedule.next_run_at,
            schedule_source=schedule.state,
            schedule=schedule,
            run_in_progress=bool(task is not None and task.status == "RUNNING"),
            monitored=monitored_summary(self.session, watchlist=self.watchlist),
            providers=self._providers(runs),
            latest_sweep=sweep,
            broad_discovery=self._broad_discovery(),
            corpus=self._corpus(sweep.started_at),
            cadences=dict(self.policy.get("cadences") or {}),
        )

    # -- pieces ----------------------------------------------------------
    def _real_runs(self) -> list[CollectionRun]:
        return list(
            self.session.execute(
                select(CollectionRun)
                .where(CollectionRun.is_fixture.is_(False))
                .order_by(CollectionRun.started_at.desc())
            )
            .scalars()
            .all()
        )

    def _last_success(self, runs: list[CollectionRun]) -> datetime | None:
        stamps = _stamps(r.last_successful_run for r in runs)
        return max(stamps) if stamps else None

    def _interval_hours(self) -> float:
        cadences = self.policy.get("cadences") or {}
        structured = cadences.get("STRUCTURED_ATS_REFRESH") or {}
        return float(structured.get("interval_hours") or 6)

    def _freshness(self, hours: float | None, interval: float) -> str:
        if hours is None:
            return NEVER
        thresholds = self.policy.get("freshness") or {}
        fresh = float(thresholds.get("fresh_within_intervals") or 1.4) * interval
        aging = float(thresholds.get("aging_within_intervals") or 4.0) * interval
        if hours <= fresh:
            return FRESH
        if hours <= aging:
            return AGING
        return STALE

    def _next_scheduled(self) -> ScheduleState:
        """Ask the operating system, because that is where the answer lives.

        This used to read a `schedule` column on the `system_task` row — which
        nothing ever wrote, because `schtasks /Create` registers with Windows
        and not with this database. A task that was registered, enabled and due
        at 01:00 therefore reported "Not scheduled", while the Scheduling card
        on the same page said "Scheduled task registered". D-45.

        Still true, and still the point of the original rule: a cadence in
        config is an intent, and no next-run time is ever *projected* from one.
        Every timestamp here comes from the scheduler.
        """
        return read_schedule_state(query=self._schedule_query)

    def _providers(self, runs: list[CollectionRun]) -> list[ProviderStatus]:
        from app.services import monitored_companies

        monitored_counts: dict[str, int] = {}
        for config in monitored_companies.collection_targets(self.session, watchlist=self.watchlist):
            monitored_counts[config.ats_type] = monitored_counts.get(config.ats_type, 0) + 1
        board_health = monitored_companies.coverage(self.session, watchlist=self.watchlist)[
            "by_provider"
        ]

        jobs_by_source: dict[str, int] = {}
        open_by_source: dict[str, int] = {}
        new_by_source: dict[str, int] = {}
        # One scan of the job table, not three: rows carry full descriptions,
        # so each pass over 15k of them costs most of a second.
        since = utcnow() - timedelta(hours=24)
        rows = self.session.execute(
            select(
                Job.discovery_source,
                _count(),
                func.sum(case((Job.lifecycle_status == JobLifecycleStatus.OPEN.value, 1), else_=0)),
                func.sum(case((Job.first_seen >= since, 1), else_=0)),
            )
            .where(Job.is_demo.is_(False))
            .group_by(Job.discovery_source)
        )
        for source, total, open_count, new_count in rows:
            key = str(source or "").upper()
            jobs_by_source[key] = int(total or 0)
            open_by_source[key] = int(open_count or 0)
            new_by_source[key] = int(new_count or 0)

        out: list[ProviderStatus] = []
        for provider in sorted(CONNECTORS):
            provider_runs = [r for r in runs if (r.connector or "").upper() == provider]
            successes = _stamps(r.last_successful_run for r in provider_runs)
            attempts = _stamps(r.started_at for r in provider_runs)
            monitored = monitored_counts.get(provider, 0)
            note = ""
            enabled = monitored_companies.provider_enabled(provider)
            if not enabled:
                note = "Switched off in collection_policy.yaml; its companies are not polled."
            elif monitored == 0:
                note = "Implemented and tested, but no monitored company uses it."
            health = board_health.get(provider, {})
            out.append(
                ProviderStatus(
                    provider=provider,
                    implemented=True,
                    monitored_companies=monitored,
                    last_success_at=max(successes) if successes else None,
                    last_attempt_at=max(attempts) if attempts else None,
                    last_status=provider_runs[0].status if provider_runs else None,
                    jobs_collected=jobs_by_source.get(provider, 0),
                    note=note,
                    enabled=enabled,
                    healthy_boards=health.get(monitored_companies.HEALTHY, 0),
                    stale_boards=health.get(monitored_companies.STALE, 0),
                    failing_boards=health.get(monitored_companies.FAILING, 0),
                    never_collected_boards=health.get(monitored_companies.NOT_YET_COLLECTED, 0),
                    open_jobs=open_by_source.get(provider, 0),
                    new_jobs_24h=new_by_source.get(provider, 0),
                )
            )
        for provider, reason in sorted(DEFERRED_SOURCES.items()):
            out.append(
                ProviderStatus(
                    provider=provider,
                    implemented=False,
                    monitored_companies=0,
                    last_success_at=None,
                    last_attempt_at=None,
                    last_status=None,
                    jobs_collected=0,
                    note=reason,
                )
            )
        return out

    def _latest_sweep(self, runs: list[CollectionRun]) -> SweepSummary:
        if not runs:
            return SweepSummary()
        gap = timedelta(
            minutes=float((self.policy.get("freshness") or {}).get("sweep_gap_minutes") or 45)
        )
        newest = as_utc(runs[0].started_at)
        sweep: list[CollectionRun] = []
        previous = newest
        for run in runs:
            started = as_utc(run.started_at)
            if started is None or previous is None:
                break
            if previous - started > gap:
                break
            sweep.append(run)
            previous = started

        started_stamps = _stamps(r.started_at for r in sweep)
        completed_stamps = _stamps(r.completed_at for r in sweep)
        start = min(started_stamps) if started_stamps else None
        end = max(completed_stamps) if completed_stamps else None
        failures = [
            f"{r.company_name or r.source}: {(r.error_summary or r.status).splitlines()[0][:200]}"
            for r in sweep
            if r.status not in (CollectionRunStatus.COMPLETED.value,)
        ]
        return SweepSummary(
            started_at=start,
            completed_at=end,
            companies=len(sweep),
            jobs_new=sum(r.jobs_new or 0 for r in sweep),
            jobs_changed=sum(r.jobs_changed or 0 for r in sweep),
            jobs_unchanged=sum(r.jobs_unchanged or 0 for r in sweep),
            jobs_failed=sum(r.jobs_failed or 0 for r in sweep),
            failures=failures,
            duration_seconds=round((end - start).total_seconds(), 1) if start and end else None,
            triggered_by=sweep[0].triggered_by if sweep else "",
            closure_candidates=self._closure_candidates(),
        )

    def _closure_candidates(self) -> int:
        """Jobs a recent poll stopped returning.

        Lifecycle reconciliation runs after EVERY successful company collection,
        never as a separate pass — a source that errored says nothing about
        which jobs still exist, so its silence is not treated as closure.
        """
        rows = self.session.execute(
            select(_count()).where(
                Job.is_demo.is_(False),
                Job.lifecycle_status.in_(
                    [
                        JobLifecycleStatus.POSSIBLY_CLOSED.value,
                        JobLifecycleStatus.DISAPPEARED.value,
                    ]
                ),
            )
        ).scalar_one()
        return int(rows or 0)

    def _broad_discovery(self) -> dict[str, Any]:
        cadence = (self.policy.get("cadences") or {}).get("BROAD_DISCOVERY") or {}
        approved = self._approved_actor_count()
        state = "ACTIVE" if approved and cadence.get("enabled") else str(
            cadence.get("status") or "PAUSED"
        )
        return {
            "state": state,
            "approved_actors": approved,
            "interval_hours": cadence.get("interval_hours"),
            "detail": str(cadence.get("description") or "").strip(),
        }

    def _approved_actor_count(self) -> int:
        registry = load_yaml_config("approved_apify_actors.yaml")
        actors = registry.get("actors") or []
        return sum(1 for a in actors if isinstance(a, dict) and a.get("status") == "APPROVED")

    def _corpus(self, sweep_started: datetime | None) -> dict[str, Any]:
        """Corpus size and age, in one pass over ``job``.

        Five separate aggregates read the same 8,000 rows five times — two of
        them full scans, because ``first_seen`` is not indexed — and this is the
        page the user opens to ask a one-line question. One query answers all
        of it.

        "Confirmed in the last sweep" is the number that says *how much of this
        list is still real*: everything below it is a posting nobody has
        re-checked since.
        """
        from sqlalchemy import case, func

        confirmed_since = sweep_started or utcnow()
        row = self.session.execute(
            select(
                func.count(),
                func.sum(
                    case((Job.lifecycle_status == JobLifecycleStatus.OPEN.value, 1), else_=0)
                ),
                func.min(Job.first_seen),
                func.max(Job.first_seen),
                func.sum(
                    case(
                        (
                            Job.last_confirmed_open_at.is_not(None)
                            & (Job.last_confirmed_open_at >= confirmed_since),
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).where(Job.is_demo.is_(False))
        ).one()

        total, open_jobs, oldest, newest, confirmed = row
        return {
            "jobs": int(total or 0),
            "open": int(open_jobs or 0),
            "oldest_first_seen": _iso(as_utc(oldest) if oldest else None),
            "newest_first_seen": _iso(as_utc(newest) if newest else None),
            "confirmed_in_last_sweep": int(confirmed or 0) if sweep_started else 0,
        }


def _count():  # type: ignore[no-untyped-def]
    from sqlalchemy import func

    return func.count()


def _stamps(values: Iterable[datetime | None]) -> list[datetime]:
    """Timezone-aware timestamps only. A row with no timestamp says nothing
    about when anything last happened, so it is dropped rather than defaulted."""
    return [stamp for stamp in (as_utc(v) for v in values) if stamp is not None]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
