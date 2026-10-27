"""Collection orchestration: staging, hashing, dedupe, lifecycle, isolation.

The end-to-end fixture flow the Slice 3 quality gate requires lives at the
bottom of this file: connector → raw source → normalize → dedupe → analyze →
job detail → dashboard, once for a Fall job and once for a 2027 job.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.connectors.ashby import AshbyConnector
from app.connectors.base import SourceConfig
from app.connectors.greenhouse import GreenhouseConnector
from app.connectors.http import HttpClient
from app.connectors.lever import LeverConnector
from app.models.enums import CollectionRunStatus, JobLifecycleStatus, SearchLane
from app.models.ingestion import CollectionRun, Job, JobSourceAssociation, RawJobSourceRecord
from app.services.collection import CollectionService
from app.services.watchlist import WatchlistLoadResult
from tests.fixtures_ats import (
    ASHBY_FALL_JOB,
    GREENHOUSE_FALL_JOB,
    GREENHOUSE_FULLTIME_JOB,
    LEVER_FALL_JOB,
    ashby_payload,
    greenhouse_payload,
)


def _client(payload, *, status: int = 200) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)

    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _: None, max_retries=0)


def _config(ats_type="GREENHOUSE", **overrides) -> SourceConfig:
    base = {
        "company_name": "Fixture Board Co",
        "ats_type": ats_type,
        "ats_identifier": "fixtureco",
        "is_fixture": True,
        "active": True,
        "automated_collection_enabled": True,
    }
    base.update(overrides)
    return SourceConfig(**base)  # type: ignore[arg-type]


def _service(session, payload, *, ats_type="GREENHOUSE", status: int = 200) -> CollectionService:
    """A service whose connector is wired to a mocked transport."""
    connectors = {
        "GREENHOUSE": GreenhouseConnector,
        "LEVER": LeverConnector,
        "ASHBY": AshbyConnector,
    }

    def factory(requested: str, **_kwargs):
        return connectors[requested.upper()](client=_client(payload, status=status))

    return CollectionService(session, connector_factory=factory)


# ---------------------------------------------------------------------------
# Raw staging and provenance
# ---------------------------------------------------------------------------


def test_collection_stages_raw_records_before_creating_jobs(session):
    service = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB))
    service.collect_company(_config())
    session.flush()

    raw = session.execute(select(RawJobSourceRecord)).scalars().all()
    assert len(raw) == 1
    assert raw[0].connector == "greenhouse"
    assert raw[0].external_id == "4455667"
    assert raw[0].source_url == "https://boards.greenhouse.io/fixtureco/jobs/4455667"
    assert raw[0].source_content_hash
    assert raw[0].raw_payload, "the source payload must be replayable without a refetch"
    assert raw[0].job_id is not None


def test_source_association_records_provenance(session):
    service = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB))
    service.collect_company(_config())
    session.flush()

    association = session.execute(select(JobSourceAssociation)).scalars().one()
    assert association.connector == "greenhouse"
    assert association.discovery_channel == "A_ATS_WATCHLIST"
    assert association.source_external_id == "4455667"


def test_canonical_source_is_the_ats_posting(session):
    """Channel A collects from the employer's own board, so it is canonical."""
    service = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB))
    service.collect_company(_config())
    session.flush()

    job = session.execute(select(Job)).scalars().one()
    assert job.canonical_source == "GREENHOUSE"
    assert job.canonical_verification == "CANONICAL_VERIFIED"
    # discovery_source is preserved separately so Slice 4 can add LinkedIn as a
    # second discovery path without overwriting how the job was found.
    assert job.discovery_source == "GREENHOUSE"


def test_collection_run_records_counters_and_zero_cost(session):
    service = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB, GREENHOUSE_FULLTIME_JOB))
    service.collect_company(_config())
    session.flush()

    run = session.execute(select(CollectionRun)).scalars().one()
    assert run.status == CollectionRunStatus.COMPLETED.value
    assert run.records_received == 2
    assert run.records_valid == 2
    assert run.jobs_new == 2
    assert run.estimated_cost_usd == 0.0, "structured public sources cost nothing"
    assert run.completed_at is not None
    assert run.connector_version
    assert run.http_stats


# ---------------------------------------------------------------------------
# The content-hash gate
# ---------------------------------------------------------------------------


def test_unchanged_poll_does_not_reanalyze(session):
    """The cost control that makes nightly collection viable."""
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB)
    first = _service(session, payload).collect_company(_config())
    session.flush()
    assert first.jobs_new == 1

    second = _service(session, payload).collect_company(_config())
    session.flush()

    assert second.jobs_new == 0
    assert second.jobs_unchanged == 1
    assert second.jobs_changed == 0

    job = session.execute(select(Job)).scalars().one()
    assert len(job.analyses) == 1, "an unchanged posting must not be analyzed twice"


def test_repeated_polls_stay_free(session):
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()
    for _ in range(4):
        outcome = _service(session, payload).collect_company(_config())
        session.flush()
        assert outcome.jobs_unchanged == 1
    assert len(session.execute(select(Job)).scalars().one().analyses) == 1


def test_changed_posting_is_reanalyzed_and_versioned(session):
    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
    session.flush()

    changed = {
        **GREENHOUSE_FALL_JOB,
        "content": GREENHOUSE_FALL_JOB["content"]
        + "&lt;p&gt;Additional requirement: experience with Airflow.&lt;/p&gt;",
    }
    outcome = _service(session, greenhouse_payload(changed)).collect_company(_config())
    session.flush()

    assert outcome.jobs_changed == 1
    assert outcome.jobs_new == 0
    job = session.execute(select(Job)).scalars().one()
    assert len(job.analyses) == 2
    assert len(job.versions) == 1


def test_unchanged_poll_still_updates_last_seen(session):
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()
    before = session.execute(select(Job)).scalars().one().last_seen

    _service(session, payload).collect_company(_config())
    session.flush()
    assert session.execute(select(Job)).scalars().one().last_seen >= before


# ---------------------------------------------------------------------------
# Deduplication across sources
# ---------------------------------------------------------------------------


def test_same_job_seen_twice_produces_one_canonical_job(session):
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()
    _service(session, payload).collect_company(_config())
    session.flush()

    assert len(session.execute(select(Job)).scalars().all()) == 1
    # Both source records survive: provenance is never dropped on merge.
    assert len(session.execute(select(RawJobSourceRecord)).scalars().all()) == 2


def test_duplicate_within_one_payload_collapses(session):
    """The same posting returned twice by one board must not create two jobs."""
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB, dict(GREENHOUSE_FALL_JOB))
    outcome = _service(session, payload).collect_company(_config())
    session.flush()

    assert outcome.records_valid == 2
    assert len(session.execute(select(Job)).scalars().all()) == 1


def test_the_same_role_on_two_ats_platforms_is_one_job(session):
    """A company mid-migration lists the same role on both boards."""
    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(
        _config("GREENHOUSE", company_name="Dual Board Co")
    )
    session.flush()

    lever_twin = {
        **LEVER_FALL_JOB,
        "text": "Data Scientist, Product",
        "categories": {"location": "Boston, MA"},
    }
    _service(session, [lever_twin], ats_type="LEVER").collect_company(
        _config("LEVER", company_name="Dual Board Co")
    )
    session.flush()

    jobs = session.execute(select(Job)).scalars().all()
    assert len(jobs) == 1, "the same opening on two boards is still one opening"
    associations = session.execute(select(JobSourceAssociation)).scalars().all()
    assert {a.connector for a in associations} == {"greenhouse", "lever"}


def test_different_roles_stay_separate(session):
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB, GREENHOUSE_FULLTIME_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()
    assert len(session.execute(select(Job)).scalars().all()) == 2


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_one_company_failure_does_not_end_the_run(session):
    """A fifty-company watchlist always contains a broken board."""
    good = _config(company_name="Working Co")
    bad = _config(company_name="Broken Co")

    def factory(ats_type: str, **_kwargs):
        return GreenhouseConnector(client=_client(None, status=500))

    broken = CollectionService(session, connector_factory=factory)
    working = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB))

    summary_outcomes = [
        working.collect_company(good),
        broken.collect_company(bad),
    ]
    session.flush()

    assert summary_outcomes[0].ok is True
    assert summary_outcomes[1].ok is False
    # The healthy company's job still landed.
    assert session.execute(select(Job)).scalars().all()


def test_transient_failure_is_recorded_on_the_run(session):
    service = _service(session, None, status=503)
    outcome = service.collect_company(_config())
    session.flush()

    assert outcome.status == CollectionRunStatus.FAILED.value
    run = session.execute(select(CollectionRun)).scalars().one()
    assert run.status == CollectionRunStatus.FAILED.value
    assert run.error_summary


def test_capability_failure_is_skipped_not_failed(session):
    service = _service(session, None, status=403)
    outcome = service.collect_company(_config())
    session.flush()
    assert outcome.capability_failure is True
    assert outcome.status == CollectionRunStatus.SKIPPED.value


def test_unsupported_ats_type_fails_that_company_only(session):
    service = CollectionService(session)
    outcome = service.collect_company(_config("WORKABLE"))
    assert outcome.ok is False
    assert "not implemented in this slice" in outcome.errors[0]


# ---------------------------------------------------------------------------
# Lifecycle — conservative closure
# ---------------------------------------------------------------------------


def test_a_job_missing_once_is_not_closed(session):
    """One absent poll must never close a live opportunity."""
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB, GREENHOUSE_FULLTIME_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()

    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
    session.flush()

    missing = (
        session.execute(select(Job).where(Job.title == "Data Engineer, New Grad 2027"))
        .scalars()
        .one()
    )
    assert missing.consecutive_misses == 1
    assert missing.lifecycle_status == JobLifecycleStatus.OPEN.value


def test_repeated_misses_escalate_to_possibly_closed_then_closed(session):
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB, GREENHOUSE_FULLTIME_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()

    reduced = greenhouse_payload(GREENHOUSE_FALL_JOB)
    statuses = []
    for _ in range(5):
        _service(session, reduced).collect_company(_config())
        session.flush()
        job = (
            session.execute(select(Job).where(Job.title == "Data Engineer, New Grad 2027"))
            .scalars()
            .one()
        )
        statuses.append(job.lifecycle_status)

    assert statuses[0] == JobLifecycleStatus.OPEN.value
    assert JobLifecycleStatus.POSSIBLY_CLOSED.value in statuses
    assert statuses[-1] == JobLifecycleStatus.CLOSED.value


def test_a_source_error_never_ages_a_job_toward_closure(session):
    """Silence from a broken source says nothing about which jobs exist."""
    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
    session.flush()

    for _ in range(6):
        _service(session, None, status=500).collect_company(_config())
        session.flush()

    job = session.execute(select(Job)).scalars().one()
    assert job.consecutive_misses == 0
    assert job.lifecycle_status == JobLifecycleStatus.OPEN.value


def test_a_reappearing_job_is_reopened(session):
    payload = greenhouse_payload(GREENHOUSE_FALL_JOB, GREENHOUSE_FULLTIME_JOB)
    _service(session, payload).collect_company(_config())
    session.flush()
    for _ in range(3):
        _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
        session.flush()

    job = (
        session.execute(select(Job).where(Job.title == "Data Engineer, New Grad 2027"))
        .scalars()
        .one()
    )
    assert job.lifecycle_status == JobLifecycleStatus.POSSIBLY_CLOSED.value

    _service(session, payload).collect_company(_config())
    session.flush()
    session.refresh(job)
    assert job.lifecycle_status == JobLifecycleStatus.OPEN.value
    assert job.consecutive_misses == 0


# ---------------------------------------------------------------------------
# Fixture isolation — the Slice 2 guarantee must not regress
# ---------------------------------------------------------------------------


def test_fixture_jobs_never_reach_real_analytics(session):
    from app.services.dashboard import DashboardService, ExecutionDashboardService
    from app.services.gaps import SkillGapService

    _service(
        session, greenhouse_payload(GREENHOUSE_FALL_JOB, GREENHOUSE_FULLTIME_JOB)
    ).collect_company(_config(is_fixture=True))
    session.flush()

    assert len(session.execute(select(Job)).scalars().all()) == 2
    assert all(j.is_demo for j in session.execute(select(Job)).scalars())

    dashboard = DashboardService(session)
    assert dashboard.kpis().jobs_found == 0
    assert dashboard.source_performance() == []
    assert dashboard.lane_counts() == {}
    assert SkillGapService(session).baseline().jobs_considered == 0
    assert ExecutionDashboardService(session).panel().applications_needing_action == []


def test_a_real_company_produces_real_jobs(session):
    from app.services.dashboard import DashboardService

    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(
        _config(is_fixture=False, company_name="Real Co")
    )
    session.flush()
    assert DashboardService(session).kpis().jobs_found == 1


def test_manually_imported_demo_job_never_reaches_real_analytics(session):
    """Regression for the Northwind Analytics incident (D-6, D-25).

    A development-verification job was inserted via ``ManualImportService``
    without ``is_demo=True`` and, lacking the "(DEMO)" naming convention
    ``app/services/demo.py`` uses for its own fixtures, silently passed as a
    real record until caught and quarantined by hand. This asserts the
    general invariant the fix relies on: ANY job flagged ``is_demo=True`` —
    regardless of how it entered the system (manual import here, not a
    fixture connector run as in the tests above) or whether its
    company/title look like a real employer — is excluded from every real
    analytics surface, not just the connector-fixture path already covered
    by ``test_fixture_jobs_never_reach_real_analytics``.
    """
    from app.services.dashboard import DashboardService, ExecutionDashboardService
    from app.services.gaps import SkillGapService
    from app.services.import_service import ManualImportService

    outcome = ManualImportService(session).import_text(
        company_name="Northwind Analytics",  # deliberately real-looking, no "(DEMO)" suffix
        title="Data Scientist, Product",
        description="Requirements\n- Master's degree\n- Python and SQL\n",
        location="Boston, MA",
        is_demo=True,
    )
    assert outcome.job is not None
    outcome.job.is_demo = True
    if outcome.job.application:
        outcome.job.application.is_demo = True
    if outcome.job.company:
        outcome.job.company.is_demo = True
    session.flush()

    assert session.execute(select(Job)).scalars().one().is_demo is True

    dashboard = DashboardService(session)
    assert dashboard.kpis().jobs_found == 0
    assert dashboard.source_performance() == []
    assert dashboard.lane_counts() == {}
    assert SkillGapService(session).baseline().jobs_considered == 0
    assert ExecutionDashboardService(session).panel().applications_needing_action == []


def test_fixture_runs_are_flagged(session):
    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
    session.flush()
    assert session.execute(select(CollectionRun)).scalars().one().is_fixture is True


# ---------------------------------------------------------------------------
# Watchlist gating
# ---------------------------------------------------------------------------


def test_inactive_companies_are_not_collected(session):
    """A proposed company must not be polled until a human approves it."""
    watchlist = WatchlistLoadResult(companies=[_config(active=False, is_fixture=False)])
    summary = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_watchlist(
        watchlist=watchlist
    )
    assert summary.outcomes == []
    assert summary.skipped
    assert session.execute(select(Job)).scalars().all() == []


def test_fixtures_are_only_collected_when_asked_for(session):
    watchlist = WatchlistLoadResult(fixtures=[_config()])
    service = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB))

    assert service.collect_watchlist(watchlist=watchlist).outcomes == []
    assert service.collect_watchlist(watchlist=watchlist, only_fixtures=True).outcomes


def test_lane_scope_still_filters_targets(session):
    watchlist = WatchlistLoadResult(
        companies=[
            _config(company_name="Fall Only", watchlist_scope="FALL", is_fixture=False),
            _config(company_name="FT Only", watchlist_scope="FULL_TIME", is_fixture=False),
            _config(company_name="Both", watchlist_scope="BOTH", is_fixture=False),
        ]
    )
    service = _service(session, greenhouse_payload())
    fall = service.collect_watchlist(
        lane=SearchLane.FULL_TIME_2027.value, watchlist=watchlist
    )
    # FULL_TIME scope selects the full-time company and the BOTH one. The
    # equivalent Fall assertion moved with the lane (D-51).
    assert {o.config.company_name for o in fall.outcomes} == {"FT Only", "Both"}


def test_dry_run_writes_nothing(session):
    service = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB))
    outcome = service.collect_company(_config(), dry_run=True)
    session.flush()

    assert outcome.records_valid == 1
    assert session.execute(select(Job)).scalars().all() == []
    assert "Dry run" in outcome.errors[0]


# ---------------------------------------------------------------------------
# End-to-end fixture flow — the Slice 3 quality gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,ats_type,expected_lane,expected_title",
    [
        (
            greenhouse_payload(GREENHOUSE_FALL_JOB),
            "GREENHOUSE",
            SearchLane.FULL_TIME_2027.value,
            "Data Scientist, Product",
        ),
        (
            greenhouse_payload(GREENHOUSE_FULLTIME_JOB),
            "GREENHOUSE",
            SearchLane.FULL_TIME_2027.value,
            "Data Engineer, New Grad 2027",
        ),
    ],
)
def test_end_to_end_fixture_flow(session, payload, ats_type, expected_lane, expected_title):
    """CONNECTOR → RAW → NORMALIZE → DEDUPE → ANALYZE → DETAIL → DASHBOARD."""
    from app.services.dashboard import DashboardService

    outcome = _service(session, payload, ats_type=ats_type).collect_company(_config(ats_type))
    session.flush()

    assert outcome.ok
    assert outcome.jobs_new == 1

    job = session.execute(select(Job)).scalars().one()
    assert job.title == expected_title
    assert job.search_lane == expected_lane
    assert job.screen_status in ("PASSED", "FLAGGED")

    analysis = job.latest_analysis
    assert analysis is not None
    assert analysis.priority_score is not None
    assert analysis.eligibility is not None
    # The Slice 1 invariant, still holding through an automated path.
    assert job.authorization_state != "AUTHORIZED_CONFIRMED"
    assert analysis.requirements, "the connector body must parse into requirements"

    # Fixture jobs stay out of the real dashboard.
    assert DashboardService(session).kpis().jobs_found == 0


def test_a_target_job_is_analyzed_in_the_same_pass(session):
    """The Fall lane is time-critical; it is not deferred to a later batch."""
    outcome = _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
    session.flush()
    # The Fall fast path was removed with the internship lane (D-51). What
    # this test still proves is that a target job IS analyzed in the same pass.
    assert outcome.jobs_new == 1

    job = session.execute(select(Job)).scalars().one()
    # The fixtures are full-time postings now (D-51), so they no longer
    # land in the retired Fall lane. What these tests prove — that the
    # posting was ingested, classified and analyzed — is unchanged.
    assert job.search_lane != SearchLane.FALL_2026_INTERNSHIP_COOP.value
    assert job.latest_analysis is not None


def test_ashby_job_flows_end_to_end(session):
    outcome = _service(session, ashby_payload(ASHBY_FALL_JOB), ats_type="ASHBY").collect_company(
        _config("ASHBY")
    )
    session.flush()

    assert outcome.jobs_new == 1
    job = session.execute(select(Job)).scalars().one()
    # The fixtures are full-time postings now (D-51), so they no longer
    # land in the retired Fall lane. What these tests prove — that the
    # posting was ingested, classified and analyzed — is unchanged.
    assert job.search_lane != SearchLane.FALL_2026_INTERNSHIP_COOP.value
    assert job.canonical_source == "ASHBY"
    assert job.latest_analysis is not None


def test_no_application_is_created_from_automated_discovery_beyond_discovered(session):
    """Discovery stops at the tracker. It never applies."""
    from app.models.enums import ApplicationStatus

    _service(session, greenhouse_payload(GREENHOUSE_FALL_JOB)).collect_company(_config())
    session.flush()

    job = session.execute(select(Job)).scalars().one()
    assert job.application is not None
    assert job.application.current_status == ApplicationStatus.DISCOVERED.value
    assert job.application.applied_at is None
