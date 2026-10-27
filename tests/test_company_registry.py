"""The company registry: the curated seed and the database, merged into one set.

Everything here runs on the in-memory test database and a watchlist built in
the test — never ``data/config/companies.yaml``, never ``jobhunt.db``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.connectors.base import SourceConfig
from app.models.base import utcnow
from app.models.ingestion import Company
from app.services import monitored_companies as registry
from app.services.watchlist import WatchlistLoadResult


def _seed(name: str, ats: str = "GREENHOUSE", token: str | None = None, **kw) -> SourceConfig:
    return SourceConfig(
        company_name=name,
        ats_type=ats,
        ats_identifier=token if token is not None else name.lower().replace(" ", ""),
        active=True,
        automated_collection_enabled=kw.pop("automated", ats != "UNKNOWN"),
        tier=kw.pop("tier", "TIER_A"),
        **kw,
    )


@pytest.fixture
def watchlist() -> WatchlistLoadResult:
    return WatchlistLoadResult(
        companies=[
            _seed("Klaviyo"),
            _seed("Pinecrest", "ASHBY", "pinecrest"),
            _seed("Wayfair", "UNKNOWN", "", automated=False),
        ]
    )


# -- the seed ----------------------------------------------------------------


def test_the_core_seed_loads_without_any_database_rows(session, watchlist):
    entries = registry.registry_entries(session, watchlist=watchlist)
    by_name = {e.company_name: e for e in entries}
    assert set(by_name) == {"Klaviyo", "Pinecrest", "Wayfair"}
    assert all(e.registry_source == "CORE_SEED" for e in entries)
    assert by_name["Klaviyo"].monitoring_status == "ACTIVE"
    assert by_name["Wayfair"].monitoring_status == "MANUAL_ONLY"
    assert by_name["Klaviyo"].health() == registry.NOT_YET_COLLECTED


def test_seed_companies_are_polled_first_in_tier_order(session, watchlist):
    registry.monitor(session, company_name="Mercury", ats_type="ASHBY", ats_identifier="mercury")
    targets = registry.collection_targets(session, watchlist=watchlist)
    assert [t.company_name for t in targets] == ["Klaviyo", "Pinecrest", "Mercury"]
    assert targets[-1].registry_source == "DISCOVERED"


# -- adding and deduplicating -------------------------------------------------


def test_a_discovered_company_is_added_with_its_provenance(session, watchlist):
    outcome = registry.monitor(
        session,
        company_name="Mercury",
        ats_type="ASHBY",
        ats_identifier="mercury",
        discovered_from_job_id="JOB-1",
    )
    assert outcome.ok and outcome.created
    row = outcome.company
    assert (row.registry_source, row.monitoring_status, row.source_verification) == (
        "DISCOVERED",
        "ACTIVE",
        "CONFIRMED",
    )
    assert row.discovered_from_job_id == "JOB-1"


@pytest.mark.parametrize("spelling", ["Brightpath", "Bright Path", "brightpath", "Brightpath, Inc."])
def test_name_spellings_of_one_employer_find_one_row(session, spelling):
    registry.monitor(session, company_name="Brightpath", ats_type="ASHBY", ats_identifier="brightpath")
    session.flush()
    row = registry.find_row(session, company_name=spelling)
    assert row is not None and row.ats_identifier == "brightpath"


def test_the_same_board_under_another_name_is_not_added_twice(session):
    first = registry.monitor(session, company_name="Brightpath", ats_type="ASHBY", ats_identifier="brightpath")
    second = registry.monitor(
        session, company_name="Brightpath Global LLC", ats_type="ASHBY", ats_identifier="Brightpath"
    )
    assert second.company.company_id == first.company.company_id
    assert session.query(Company).count() == 1


def test_distinct_boards_with_similar_names_are_not_merged(session):
    """Two monitoring targets stay two, even under one display name."""
    a = registry.monitor(
        session, company_name="Acme", ats_type="GREENHOUSE", ats_identifier="acme"
    )
    b = registry.monitor(session, company_name="Acme", ats_type="LEVER", ats_identifier="acme")
    assert a.company.company_id != b.company.company_id


def test_a_registry_row_naming_a_seed_board_is_not_polled_twice(session, watchlist):
    registry.monitor(session, company_name="Klaviyo Inc", ats_type="GREENHOUSE", ats_identifier="KLAVIYO")
    targets = registry.collection_targets(session, watchlist=watchlist)
    assert [t.company_name for t in targets].count("Klaviyo") == 1
    assert len(targets) == 2


# -- pause and resume ----------------------------------------------------------


def test_a_seed_company_can_be_paused_without_editing_the_file(session, watchlist):
    outcome = registry.set_monitoring(
        session, status="PAUSED", company_name="Klaviyo", watchlist=watchlist
    )
    assert outcome.ok
    assert outcome.company.registry_source == "CORE_SEED"
    names = [t.company_name for t in registry.collection_targets(session, watchlist=watchlist)]
    assert "Klaviyo" not in names
    entry = next(e for e in registry.registry_entries(session, watchlist=watchlist) if e.company_name == "Klaviyo")
    assert entry.health() == registry.PAUSED

    registry.set_monitoring(session, status="ACTIVE", company_name="Klaviyo", watchlist=watchlist)
    names = [t.company_name for t in registry.collection_targets(session, watchlist=watchlist)]
    assert "Klaviyo" in names


def test_a_registry_company_pauses_and_resumes(session, watchlist):
    row = registry.monitor(session, company_name="Mercury", ats_type="ASHBY", ats_identifier="mercury").company
    registry.set_monitoring(session, status="PAUSED", company_id=row.company_id, watchlist=watchlist)
    assert "Mercury" not in [t.company_name for t in registry.collection_targets(session, watchlist=watchlist)]
    registry.set_monitoring(session, status="ACTIVE", company_id=row.company_id, watchlist=watchlist)
    assert "Mercury" in [t.company_name for t in registry.collection_targets(session, watchlist=watchlist)]


def test_an_unconfirmed_or_unsupported_company_cannot_be_resumed(session, watchlist):
    registry.record_unsupported(
        session, company_name="Branded Co", monitoring_status="VERIFY_LATER"
    )
    outcome = registry.set_monitoring(
        session, status="ACTIVE", company_name="Branded Co", watchlist=watchlist
    )
    assert not outcome.ok
    assert outcome.reason == "UNSUPPORTED_ATS"

    row = registry.monitor(session, company_name="Maybe Co", ats_type="LEVER", ats_identifier="maybe").company
    row.source_verification = "LIKELY_REVIEW"
    row.monitoring_status = "PAUSED"
    outcome = registry.set_monitoring(session, status="ACTIVE", company_id=row.company_id, watchlist=watchlist)
    assert not outcome.ok
    assert outcome.reason == "SOURCE_NOT_CONFIRMED"


def test_unmonitor_still_refuses_a_seed_company(session, watchlist):
    row = Company(company_name="Klaviyo", normalized_name="klaviyo", registry_source="CORE_SEED")
    session.add(row)
    session.flush()
    outcome = registry.unmonitor(session, row.company_id)
    assert not outcome.ok and outcome.reason == "CORE_WATCHLIST"


# -- the canary gate and the provider switch ------------------------------------


def test_an_experimental_company_is_polled_only_when_asked_for(session, watchlist):
    registry.monitor(
        session,
        company_name="Acme",
        ats_type="LEVER",
        ats_identifier="acme",
        monitoring_status="EXPERIMENTAL",
    )
    plain = registry.collection_targets(session, watchlist=watchlist)
    assert "Acme" not in [t.company_name for t in plain]
    canary = registry.collection_targets(session, watchlist=watchlist, include_experimental=True)
    assert "Acme" in [t.company_name for t in canary]


def test_a_disabled_provider_leaves_the_sweep(session, watchlist, monkeypatch):
    registry.monitor(session, company_name="Acme", ats_type="LEVER", ats_identifier="acme")
    monkeypatch.setattr(registry, "provider_enabled", lambda ats: ats != "LEVER")
    assert "Acme" not in [t.company_name for t in registry.collection_targets(session, watchlist=watchlist)]


# -- health and coverage ---------------------------------------------------------


def test_health_is_derived_from_recorded_polls_only(session, watchlist):
    config = watchlist.companies[0]
    registry.record_poll(session, config, ok=True, jobs_seen=40, jobs_new=3)
    entry = next(e for e in registry.registry_entries(session, watchlist=watchlist) if e.company_name == "Klaviyo")
    assert entry.health() == registry.HEALTHY
    assert (entry.last_jobs_seen, entry.last_new_jobs) == (40, 3)
    assert entry.health(utcnow() + timedelta(hours=30)) == registry.STALE

    registry.record_poll(session, config, ok=False, detail="SourceUnavailable: HTTP 503.")
    entry = next(e for e in registry.registry_entries(session, watchlist=watchlist) if e.company_name == "Klaviyo")
    assert entry.health() == registry.FAILING
    assert "503" in (entry.last_failure_reason or "")


def test_a_poll_creates_the_seed_row_it_needs(session, watchlist):
    company = registry.record_poll(session, watchlist.companies[1], ok=True, jobs_seen=5, jobs_new=0)
    assert company.registry_source == "CORE_SEED"


def test_coverage_counts_gaps_as_gaps(session, watchlist):
    registry.monitor(session, company_name="Mercury", ats_type="ASHBY", ats_identifier="mercury")
    registry.record_unsupported(session, company_name="Branded Co")
    registry.record_unsupported(session, company_name="Later Co", monitoring_status="VERIFY_LATER")
    registry.record_poll(session, watchlist.companies[0], ok=True, jobs_seen=1, jobs_new=0)

    cov = registry.coverage(session, watchlist=watchlist)
    assert cov["total"] == 6
    assert cov["monitored"] == 3  # Klaviyo, Pinecrest, Mercury
    assert cov["healthy"] == 1
    assert cov["not_yet_collected"] == 2
    assert cov["manual_only"] == 2  # Wayfair (seed) + Branded Co
    assert cov["verify_later"] == 1
    assert cov["unknown_ats"] == 3
    assert cov["by_provider"]["ASHBY"]["monitored"] == 2


def test_a_manual_only_seed_covered_by_a_registry_board_is_not_a_gap(session, watchlist):
    """Wayfair is in the seed as UNKNOWN ATS here. Once the registry holds a
    confirmed board for it, it is one monitored company, not a gap as well."""
    registry.monitor(session, company_name="Wayfair", ats_type="LEVER", ats_identifier="wayfair")
    entries = [e for e in registry.registry_entries(session, watchlist=watchlist) if e.company_name == "Wayfair"]
    assert len(entries) == 1
    assert (entries[0].registry_source, entries[0].monitoring_status) == ("DISCOVERED", "ACTIVE")
    cov = registry.coverage(session, watchlist=watchlist)
    assert cov["manual_only"] == 0


# -- why-missed knows the registry ------------------------------------------------


def _pasted_job(session, company: str):
    from app.models.enums import JobLifecycleStatus, ScreenStatus
    from app.models.ingestion import Job
    from app.services.text import normalize_company

    job = Job(
        company_name=company,
        normalized_company=normalize_company(company),
        title="Data Scientist",
        normalized_title="data scientist",
        description="Python and SQL.",
        discovery_source="USER_URL",
        lifecycle_status=JobLifecycleStatus.OPEN.value,
        screen_status=ScreenStatus.PASSED.value,
    )
    session.add(job)
    session.flush()
    return job


@pytest.mark.parametrize(
    ("setup", "reason", "phrase"),
    [
        ("paused", "COMPANY_NOT_MONITORED", "paused"),
        ("trial", "COMPANY_NOT_MONITORED", "on trial"),
        ("manual", "UNSUPPORTED_SOURCE", "manual-only"),
        ("verify", "COMPANY_NOT_MONITORED", "awaiting a verified careers source"),
    ],
)
def test_why_missed_names_the_registry_state(session, watchlist, setup, reason, phrase):
    from app.services.why_missed import WhyMissedService

    if setup in ("paused", "trial"):
        row = registry.monitor(
            session,
            company_name="Northwind",
            ats_type="LEVER",
            ats_identifier="northwind",
            monitoring_status="EXPERIMENTAL" if setup == "trial" else "ACTIVE",
        ).company
        if setup == "paused":
            registry.set_monitoring(session, status="PAUSED", company_id=row.company_id, watchlist=watchlist)
    else:
        registry.record_unsupported(
            session,
            company_name="Northwind",
            monitoring_status="MANUAL_ONLY" if setup == "manual" else "VERIFY_LATER",
        )
    result = WhyMissedService(session, watchlist=watchlist).explain(_pasted_job(session, "Northwind"))
    assert result.reason == reason
    assert phrase in result.headline
    assert result.evidence["registry_status"]
    assert result.monitor_offer is None
