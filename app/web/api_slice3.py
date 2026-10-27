"""Slice 3 JSON API: collection runs, source health, watchlist, profiles.

Read-only by design. **No endpoint here triggers a collection.** Running a
connector reaches out to someone else's servers and writes to the real
database, so it stays behind the CLI where it is deliberate, scriptable and
reviewable — not one stray HTTP request away.

Every response carries an explicit Pydantic model, matching the Slice 2
standard. Nothing regresses to ``dict[str, Any]``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import yaml
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.connectors import CONNECTORS, DEFERRED_SOURCES, get_connector
from app.db import get_db
from app.models.base import utcnow
from app.models.ingestion import CollectionRun, Job
from app.services.collection import CollectionService
from app.services.watchlist import load_watchlist
from app.web.schemas import (
    CollectionRunsResponse,
    CollectionRunView,
    CollectionSummaryResponse,
    ConnectorInfoView,
    DiscoveredJobsResponse,
    DiscoveredJobView,
    SearchProfilesResponse,
    SearchProfileView,
    SourceHealthResponse,
    WatchlistCompanyView,
    WatchlistResponse,
)

api3 = APIRouter(prefix="/api")

PROFILES_PATH = REPO_ROOT / "data" / "config" / "search_profiles.yaml"
WATCHLIST_PATH = REPO_ROOT / "data" / "config" / "companies.yaml"


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Collection runs
# ---------------------------------------------------------------------------


def _run_view(run: CollectionRun) -> CollectionRunView:
    return CollectionRunView(
        collection_run_id=run.collection_run_id,
        connector=run.connector,
        source=run.source,
        company_name=run.company_name,
        search_profile=run.search_profile,
        lane=run.lane,
        status=run.status,
        source_capability=run.source_capability,
        is_fixture=run.is_fixture,
        started_at=_iso(run.started_at),
        completed_at=_iso(run.completed_at),
        records_received=run.records_received,
        records_valid=run.records_valid,
        jobs_seen=run.jobs_seen,
        jobs_new=run.jobs_new,
        jobs_changed=run.jobs_changed,
        jobs_unchanged=run.jobs_unchanged,
        jobs_failed=run.jobs_failed,
        estimated_cost_usd=run.estimated_cost_usd,
        connector_version=run.connector_version,
        error_summary=run.error_summary,
    )


@api3.get("/collection-runs", response_model=CollectionRunsResponse)
def collection_runs(
    limit: int = 50, include_fixtures: bool = False, db: Session = Depends(get_db)
) -> CollectionRunsResponse:
    stmt = select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
    if not include_fixtures:
        stmt = stmt.where(CollectionRun.is_fixture.is_(False))
    rows = db.execute(stmt).scalars().all()
    return CollectionRunsResponse(
        count=len(rows),
        includes_fixtures=include_fixtures,
        runs=[_run_view(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# Watchlist and source health
# ---------------------------------------------------------------------------


def _watchlist_response(*, with_health: list[dict[str, Any]] | None = None) -> WatchlistResponse:
    result = load_watchlist()
    summary = result.summary()

    health_by_company = {row["company"]: row for row in (with_health or [])}
    entries: list[WatchlistCompanyView] = []
    for config in result.all_entries:
        row = health_by_company.get(config.company_name, {})
        try:
            capability = get_connector(config.ats_type).capability.value
        except ValueError:
            capability = "UNSUPPORTED"
        entries.append(
            WatchlistCompanyView(
                company_name=config.company_name,
                ats_type=config.ats_type,
                ats_identifier=config.ats_identifier,
                capability=row.get("capability", capability),
                watchlist_scope=config.watchlist_scope,
                tier=config.tier,
                fall_signal=config.fall_signal,
                industry_category=config.industry_category,
                active=config.active,
                automated_collection_enabled=config.automated_collection_enabled,
                is_fixture=config.is_fixture,
                config_valid=row.get("config_valid"),
                config_problems=row.get("config_problems", []),
                health=row.get("health"),
                last_successful_collection_at=row.get("last_successful_collection_at"),
                last_failure_at=row.get("last_failure_at"),
            )
        )

    raw = yaml.safe_load(WATCHLIST_PATH.read_text(encoding="utf-8")) or {}
    return WatchlistResponse(
        companies=summary["companies"],
        watchlist_active=summary["watchlist_active"],
        automation_enabled=summary["automation_enabled"],
        manual_only=summary["manual_only"],
        inactive=summary["inactive"],
        fixtures=summary["fixtures"],
        by_ats=summary["by_ats"],
        by_tier=summary["by_tier"],
        problems=summary["problems"],
        # Stated explicitly so the UI can say why the list is empty rather than
        # looking broken.
        awaiting_approval=summary["watchlist_active"] == 0,
        watchlist_version=str(raw.get("watchlist_version") or "") or None,
        activated_date=str(raw.get("activated_date") or "") or None,
        entries=entries,
    )


@api3.get("/watchlist", response_model=WatchlistResponse)
def watchlist() -> WatchlistResponse:
    """The configured watchlist. Reads the config file; touches no network."""
    return _watchlist_response()


@api3.get("/source-health", response_model=SourceHealthResponse)
def source_health(
    include_fixtures: bool = True, db: Session = Depends(get_db)
) -> SourceHealthResponse:
    """One cheap read per configured source. Ingests nothing.

    This *does* reach the network, which is why it is opt-in per request rather
    than part of the watchlist view.
    """
    rows = CollectionService(db).check_sources(include_fixtures=include_fixtures)
    response = _watchlist_response(with_health=rows)
    return SourceHealthResponse(count=len(response.entries), sources=response.entries)


# ---------------------------------------------------------------------------
# Search profiles
# ---------------------------------------------------------------------------


@api3.get("/search-profiles", response_model=SearchProfilesResponse)
def search_profiles() -> SearchProfilesResponse:
    if not PROFILES_PATH.exists():
        return SearchProfilesResponse(count=0, profiles=[])

    raw = yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8")) or {}
    profiles: list[SearchProfileView] = []
    for entry in raw.get("search_profiles") or []:
        if not isinstance(entry, dict):
            continue
        profiles.append(
            SearchProfileView(
                profile_id=str(entry.get("profile_id", "")),
                lane=str(entry.get("lane", "")),
                enabled=bool(entry.get("enabled", False)),
                locations=list(entry.get("locations") or []),
                remote=entry.get("remote"),
                employment_type=list(entry.get("employment_type") or []),
                title_keywords=list(entry.get("title_keywords") or []),
                term_keywords=list(entry.get("term_keywords") or []),
                exclusion_keywords=list(entry.get("exclusion_keywords") or []),
                source_scope=list(entry.get("source_scope") or []),
                frequency_hours=entry.get("frequency_hours"),
                max_results=entry.get("max_results"),
            )
        )
    return SearchProfilesResponse(count=len(profiles), profiles=profiles)


# ---------------------------------------------------------------------------
# Discovered jobs and summary
# ---------------------------------------------------------------------------


@api3.get("/discovered-jobs", response_model=DiscoveredJobsResponse)
def discovered_jobs(
    lane: str = "ALL", days: int = 7, limit: int = 50, db: Session = Depends(get_db)
) -> DiscoveredJobsResponse:
    """Recently discovered jobs from automated collection.

    Manual imports are excluded: this answers "what did automation find",
    which is the question the dashboard section asks.
    """
    cutoff = utcnow() - timedelta(days=days)
    stmt = (
        select(Job)
        .where(
            Job.is_demo.is_(False),
            Job.first_seen >= cutoff,
            Job.discovery_source.notin_(["MANUAL", "manual"]),
        )
        .order_by(Job.first_seen.desc())
        .limit(limit)
    )
    if lane != "ALL":
        stmt = stmt.where(Job.search_lane == lane)

    rows: list[DiscoveredJobView] = []
    for job in db.execute(stmt).scalars():
        analysis = job.latest_analysis
        score = analysis.priority_score if analysis else None
        rows.append(
            DiscoveredJobView(
                job_id=job.job_id,
                company=job.company_name,
                title=job.title,
                lane=job.search_lane,
                location=job.location_raw,
                priority=score.total if score else 0.0,
                band=score.band if score else "LOW",
                eligibility=job.eligibility_status,
                discovery_source=job.discovery_source,
                canonical_source=job.canonical_source,
                lifecycle_status=job.lifecycle_status,
                first_seen=_iso(job.first_seen),
                application_status=(
                    job.application.current_status if job.application else "DISCOVERED"
                ),
            )
        )
    return DiscoveredJobsResponse(count=len(rows), jobs=rows)


@api3.get("/collection-summary", response_model=CollectionSummaryResponse)
def collection_summary(db: Session = Depends(get_db)) -> CollectionSummaryResponse:
    connectors = []
    for ats_type in sorted(CONNECTORS):
        connector = get_connector(ats_type)
        connectors.append(ConnectorInfoView(**connector.source_metadata()))

    # Aggregates, not a load: five thousand run rows with their JSON stats
    # took most of eight seconds to materialise just to be counted.
    real_runs = CollectionRun.is_fixture.is_(False)
    runs_total, last_run_at = db.execute(
        select(func.count(), func.max(CollectionRun.started_at)).where(real_runs)
    ).one()
    # SQLite stores these timestamps naive (UTC); compare like with like. An
    # aware cutoff against naive values raised TypeError in the old Python-side
    # filter, which took this endpoint — and the Sources page — down.
    cutoff = (utcnow() - timedelta(days=7)).replace(tzinfo=None)
    recent = db.execute(
        select(func.count()).where(real_runs, CollectionRun.started_at > cutoff)
    ).scalar_one()

    # A count, not a load: the job rows carry full descriptions.
    automated = db.execute(
        select(func.count())
        .select_from(Job)
        .where(Job.is_demo.is_(False), Job.discovery_source.notin_(["MANUAL", "manual"]))
    ).scalar_one()

    return CollectionSummaryResponse(
        connectors=connectors,
        deferred_sources=DEFERRED_SOURCES,
        runs_total=int(runs_total or 0),
        runs_last_7_days=int(recent or 0),
        jobs_from_automation=int(automated),
        last_run_at=_iso(last_run_at),
        watchlist=_watchlist_response(),
    )
