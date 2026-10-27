"""Regressions from the first real browser session.

Every test here corresponds to something a user actually saw go wrong on
localhost:3000, not to a hypothetical.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.ingestion import Job
from app.models.misc import ResumeRecommendation
from app.services.pipeline import JobPipeline, RawJobInput


@pytest.fixture
def client(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.db as db_module
    from app.main import create_app

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    return TestClient(create_app()), factory


def _seed_job(factory, fulltime_jd) -> str:
    session = factory()
    outcome = JobPipeline(session).ingest(
        RawJobInput(
            company_name="Browser Test Co",
            title="Analytics Engineer, Data Science",
            description=fulltime_jd,
            location="Remote (US)",
            url="https://boards.greenhouse.io/browsertest/jobs/9001",
            external_id="9001",
            connector="greenhouse",
        )
    )
    session.commit()
    job_id = outcome.job.job_id
    session.close()
    return job_id


# ---------------------------------------------------------------------------
# CORS / preflight — the "Failed to fetch" cause
# ---------------------------------------------------------------------------


def test_options_preflight_is_answered_for_the_dev_frontend(client):
    """The browser sends OPTIONS before a JSON POST.

    It previously returned 405 with no Access-Control-* headers, so Chrome
    aborted before the POST and surfaced only "Failed to fetch".
    """
    api, _ = client
    response = api.options(
        "/api/jobs/JOB-anything/prepare-application",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code < 400, "preflight must not fail"
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "POST" in response.headers.get("access-control-allow-methods", "")


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:3000"])
def test_both_dev_origins_are_allowed(client, origin):
    api, _ = client
    response = api.get("/api/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin


def test_cors_is_not_a_wildcard(client):
    """This API serves one person's real job-search data from their machine.

    A wildcard origin combined with credentials would let any visited page
    read localhost.
    """
    api, _ = client
    response = api.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert response.headers.get("access-control-allow-origin") != "*"
    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"


# ---------------------------------------------------------------------------
# Recommendations must arrive undecided
# ---------------------------------------------------------------------------


def test_preparation_does_not_auto_accept_anything(client, fulltime_jd):
    """The user makes accept/reject decisions, not the system."""
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)

    response = api.post(f"/api/jobs/{job_id}/prepare-application")
    assert response.status_code == 200
    body = response.json()
    assert body["recommendations"], "preparation produced nothing to review"
    assert all(r["accepted"] is None for r in body["recommendations"]), (
        "recommendations must arrive undecided"
    )

    session = factory()
    rows = (
        session.execute(select(ResumeRecommendation).where(ResumeRecommendation.job_id == job_id))
        .scalars()
        .all()
    )
    assert rows
    assert all(row.accepted is None for row in rows)
    session.close()


def test_preparation_persists_and_reports_the_resume_base(client, fulltime_jd):
    """ "not chosen yet" must never appear beside a list of proposals."""
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)

    prepared = api.post(f"/api/jobs/{job_id}/prepare-application").json()
    assert prepared["resume_base"], "preparation must choose a base"

    # The cached read is what the page actually renders.
    cached = api.get(f"/api/jobs/{job_id}/resume-recommendations").json()
    assert cached["cached"] is True
    assert cached["resume_base"] == prepared["resume_base"]
    assert cached["resume_base"] is not None


def test_recommendations_and_base_are_consistent(client, fulltime_jd):
    """The exact contradiction seen in the browser: proposals shown while the
    header claimed no base had been chosen."""
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)
    api.post(f"/api/jobs/{job_id}/prepare-application")

    body = api.get(f"/api/jobs/{job_id}/resume-recommendations").json()
    if body["recommendations"]:
        assert body["resume_base"] is not None


# ---------------------------------------------------------------------------
# Refresh must not destroy user decisions
# ---------------------------------------------------------------------------


def test_refresh_preserves_user_accept_decisions(client, fulltime_jd):
    """This is what silently erased seven accepted lines.

    A refresh rebuilds proposals from the current Evidence Bank. It must not
    take the user's decisions with it.
    """
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)
    prepared = api.post(f"/api/jobs/{job_id}/prepare-application").json()

    actionable = [
        r for r in prepared["recommendations"] if r["kind"] in ("EMPHASIZE", "SAFE_REWORD")
    ]
    assert actionable, "no actionable recommendations to decide on"
    target = actionable[0]

    accept = api.post(
        f"/api/jobs/{job_id}/recommendations/{target['id']}/decision",
        json={"accepted": True},
    )
    assert accept.status_code == 200

    # Re-preparing must not discard that decision.
    api.post(f"/api/jobs/{job_id}/prepare-application")
    after = api.get(f"/api/jobs/{job_id}/resume-recommendations").json()

    match = [r for r in after["recommendations"] if r["body"] == target["body"]]
    assert match, "the decided line vanished from the proposal set"
    assert match[0]["accepted"] is True, "a refresh erased the user's decision"


def test_refresh_leaves_undecided_lines_undecided(client, fulltime_jd):
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)
    api.post(f"/api/jobs/{job_id}/prepare-application")
    api.post(f"/api/jobs/{job_id}/prepare-application")

    body = api.get(f"/api/jobs/{job_id}/resume-recommendations").json()
    assert all(r["accepted"] is None for r in body["recommendations"])


# ---------------------------------------------------------------------------
# Preparation must not fabricate CRM progress
# ---------------------------------------------------------------------------


def test_preparation_does_not_advance_the_application(client, fulltime_jd):
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)
    api.post(f"/api/jobs/{job_id}/prepare-application")

    session = factory()
    job = session.get(Job, job_id)
    assert job.application.current_status == "DISCOVERED"
    assert job.application.applied_at is None
    session.close()


def test_sqlite_pool_is_sized_for_page_fanout():
    """Rendering one job page fires a dozen parallel API calls.

    With SQLAlchemy's default 5+10 pool, a POST alongside that render returned
    "QueuePool limit of size 5 overflow 10 reached" — a 500 in the browser on a
    database this machine owns exclusively.
    """
    from app.db import _sqlite_engine_kwargs

    kwargs = _sqlite_engine_kwargs("sqlite:///jobhunt.db")
    assert kwargs["pool_size"] >= 20
    assert kwargs["max_overflow"] >= 20
    assert kwargs["connect_args"]["check_same_thread"] is False


def test_postgres_url_gets_no_sqlite_specific_kwargs():
    """The SQLite tuning must not leak into a future Postgres migration."""
    from app.db import _sqlite_engine_kwargs

    assert _sqlite_engine_kwargs("postgresql://localhost/jobhunt") == {}


def test_concurrent_reads_and_a_write_do_not_exhaust_the_pool(client, fulltime_jd):
    """Reproduces the browser failure: parallel reads plus a POST."""
    api, factory = client
    job_id = _seed_job(factory, fulltime_jd)

    for _ in range(12):
        assert api.get(f"/api/jobs/{job_id}").status_code == 200
    assert api.post(f"/api/jobs/{job_id}/prepare-application").status_code == 200
    assert api.get("/api/dashboard").status_code == 200
