"""Slice 5: experience fit, quality guards, Prepare Application → Resume
Review → Generate → Download → Mark Applied over the real HTTP surface.

Same fixture pattern as test_api_slice2.py: import a real job through the
form endpoint so it gets a genuine JobAnalysis/requirements/evidence-match
pipeline, then drive the Slice 5 endpoints exactly as the Next.js frontend
would.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.experience_fit import ExperienceFit, classify
from app.services.quality_guards import check as run_guards

# Renamed from PRIMARY_JD on 2026-09-08 (D-51). These tests exercise CRM
# transitions, resume generation and the application workspace — none of which
# is lane-specific — but they used an internship posting, which the prescreen
# now rejects because the search is full-time only.
PRIMARY_JD = """Data Scientist — Boston, MA. Full-time, starting early 2027.

Requirements
- Master's degree in Data Science or a related field
- Strong proficiency in Python and SQL
- Experience building data pipelines
- Experience with Kafka or Spark

Preferred qualifications
- Familiarity with dbt
"""

REACH_JD = """Data Scientist, Finance — San Francisco, CA. Full-time.

Requirements
- 3+ years of experience in Data Science, Analytics or a related field
- Advanced SQL and Python
- AWS experience preferred
"""


@pytest.fixture
def client(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.db as db_module
    from app.main import create_app

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    return TestClient(create_app())


def _import(client, *, company: str, title: str, description: str, location: str) -> str:
    response = client.post(
        "/import",
        data={
            "company_name": company,
            "title": title,
            "location": location,
            "description": description,
            "url": "https://example.com/jobs/1",
            "deadline": "",
            "bulk_payload": "",
            "bulk_format": "json",
        },
    )
    assert response.status_code == 200
    jobs = client.get("/api/jobs").json()["jobs"]
    match = [j for j in jobs if j["company"] == company]
    assert match, f"{company} was not imported"
    return match[0]["job_id"]


@pytest.fixture
def primary_job(client) -> str:
    return _import(
        client,
        company="Slice5 Primary Co",
        title="Data Scientist",
        description=PRIMARY_JD,
        location="Boston, MA",
    )


@pytest.fixture
def reach_job(client) -> str:
    return _import(
        client,
        company="Slice5 Reach Co",
        title="Data Scientist, Finance",
        description=REACH_JD,
        location="San Francisco, CA",
    )


# ---------------------------------------------------------------------------
# Experience fit — unit level
# ---------------------------------------------------------------------------


def test_years_in_jd_text_overrides_a_permissive_tag():
    """Regression for D-26: several EARLY_CAREER-tagged jobs actually
    required 2-8+ years. The classifier must catch this from the JD text
    itself, not just trust the stored tag."""

    class FakeAnalysis:
        years_experience_min = None

    class FakeJob:
        description = "Requirements\n- 3+ years of experience in Data Science or Analytics\n"
        title = "Data Scientist, Finance"
        experience_level = "EARLY_CAREER"

        @property
        def latest_analysis(self):
            return FakeAnalysis()

    fit, reason = classify(FakeJob())
    assert fit == ExperienceFit.STRETCH_3_PLUS
    assert "3" in reason


def test_internship_tag_with_no_years_text_is_entry_compatible():
    class FakeJob:
        description = "Full-time data science role."
        title = "Data Scientist"
        experience_level = "INTERNSHIP"

        @property
        def latest_analysis(self):
            return None

    fit, _ = classify(FakeJob())
    assert fit == ExperienceFit.ENTRY_COMPATIBLE


def test_senior_title_marker_wins_even_with_no_years_text():
    class FakeJob:
        description = "Join our team."
        title = "Senior Data Scientist"
        experience_level = "EARLY_CAREER"

        @property
        def latest_analysis(self):
            return None

    fit, reason = classify(FakeJob())
    assert fit == ExperienceFit.SENIOR
    assert "title" in reason.lower()


# ---------------------------------------------------------------------------
# Quality guards — unit level
# ---------------------------------------------------------------------------


def test_demo_job_is_blocked():
    class FakeJob:
        is_demo = True
        lifecycle_status = "OPEN"
        eligibility_status = "ELIGIBLE"

    result = run_guards(FakeJob())
    assert result.blocked is True
    assert "demo" in result.block_reason.lower()


def test_closed_job_is_blocked():
    class FakeJob:
        is_demo = False
        lifecycle_status = "CLOSED"
        eligibility_status = "ELIGIBLE"

    result = run_guards(FakeJob())
    assert result.blocked is True


def test_ineligible_job_is_blocked():
    class FakeJob:
        is_demo = False
        lifecycle_status = "OPEN"
        eligibility_status = "INELIGIBLE"

    result = run_guards(FakeJob())
    assert result.blocked is True


# ---------------------------------------------------------------------------
# Prepare Application → Resume Review → Generate → Download — HTTP level
# ---------------------------------------------------------------------------


def test_prepare_application_blocks_demo_jobs(client):
    # Seed via the app's own demo path so this is the real quarantine
    # mechanism, not a hand-rolled stand-in.
    import app.db as db_module
    from app.services.demo import seed_demo_data

    with db_module.get_session_factory()() as session:
        seed_demo_data(session)
        session.commit()

    jobs = client.get("/api/jobs?relevant_only=false&limit=200").json()["jobs"]
    # Demo jobs must never appear even with relevant_only off.
    assert all("DEMO" not in j["company"] for j in jobs)


def test_prepare_application_returns_recommendations_and_guard(client, primary_job):
    response = client.post(f"/api/jobs/{primary_job}/prepare-application")
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == primary_job
    assert body["guard"]["blocked"] is False
    assert body["recommendations"]
    assert all(r["id"] for r in body["recommendations"])
    assert all(r["accepted"] is None for r in body["recommendations"])


def test_experience_stretch_warning_surfaces_for_reach_job(client, reach_job):
    response = client.post(f"/api/jobs/{reach_job}/prepare-application")
    body = response.json()
    codes = {w["code"] for w in body["guard"]["warnings"]}
    assert "EXPERIENCE_STRETCH" in codes
    assert body["guard"]["experience_fit"] == "STRETCH_3_PLUS"


def test_generate_resume_requires_at_least_one_accepted_recommendation(client, primary_job):
    client.post(f"/api/jobs/{primary_job}/prepare-application")
    response = client.post(f"/api/jobs/{primary_job}/generate-resume")
    assert response.status_code == 422


def test_full_prepare_review_generate_download_flow(client, primary_job):
    prep = client.post(f"/api/jobs/{primary_job}/prepare-application").json()
    recs = prep["recommendations"]
    emphasize = [r for r in recs if r["kind"] in ("EMPHASIZE", "SAFE_REWORD")]
    assert emphasize, "fixture JD should produce at least one usable recommendation"

    for rec in emphasize:
        decision = client.post(
            f"/api/jobs/{primary_job}/recommendations/{rec['id']}/decision",
            json={"accepted": True},
        )
        assert decision.status_code == 200
        assert decision.json()["accepted"] is True

    generated = client.post(f"/api/jobs/{primary_job}/generate-resume")
    assert generated.status_code == 200
    body = generated.json()
    assert body["accepted_count"] == len(emphasize)
    assert body["pdf_available"] is True
    assert body["docx_available"] is True
    version_id = body["resume_version_id"]

    pdf = client.get(f"/api/resumes/{version_id}/download.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert len(pdf.content) > 0

    docx = client.get(f"/api/resumes/{version_id}/download.docx")
    assert docx.status_code == 200
    assert len(docx.content) > 0

    versions = client.get(f"/api/jobs/{primary_job}/resume-versions").json()
    assert versions["versions"][0]["resume_version_id"] == version_id
    assert versions["versions"][0]["pdf_available"] is True

    # -- application tab + mark applied --------------------------------
    tab = client.get(f"/api/jobs/{primary_job}/application-tab").json()
    assert tab["checklist"]["resume_prepared"] is True

    from datetime import date

    marked = client.post(
        f"/api/jobs/{primary_job}/mark-applied",
        json={
            "applied_at": date.today().isoformat(),
            "resume_version_id": version_id,
            "notes": "Applied via the employer portal.",
        },
    )
    assert marked.status_code == 200
    assert marked.json()["to_status"] == "APPLIED"

    application = client.get(f"/api/jobs/{primary_job}/application").json()
    assert application["current_status"] == "APPLIED"
    assert application["resume_version_id"] == version_id

    # -- interview tab ----------------------------------------------------
    interview = client.get(f"/api/jobs/{primary_job}/interview-prep").json()
    assert interview["job_id"] == primary_job
    assert interview["stage"] == "APPLIED"


def test_download_before_generation_is_404(client, primary_job):
    response = client.get("/api/resumes/RV-does-not-exist/download.pdf")
    assert response.status_code == 404


def test_decision_on_unknown_recommendation_is_404(client, primary_job):
    response = client.post(
        f"/api/jobs/{primary_job}/recommendations/RREC-nope/decision",
        json={"accepted": True},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Dashboard — applications this week
# ---------------------------------------------------------------------------


def test_applications_this_week_counts_only_recent_applied_at(client, primary_job):
    assert client.get("/api/execution").json()["applications_this_week"] == 0

    from datetime import date

    client.post(
        f"/api/jobs/{primary_job}/mark-applied",
        json={"applied_at": date.today().isoformat()},
    )
    assert client.get("/api/execution").json()["applications_this_week"] == 1
