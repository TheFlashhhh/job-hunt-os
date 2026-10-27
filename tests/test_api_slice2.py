"""Slice 2 HTTP surface, and the end-to-end user flow over it.

Slice 2 was built API-first, so this file is the contract the Next.js frontend
will consume. The final tests walk the full flow the quality gate requires, once
for each of two jobs.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

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

FULLTIME_JD = """Data Engineer, New Grad 2027. Remote within the United States. Full-time.

Minimum qualifications
- Bachelor's or Master's degree
- 0-2 years of experience
- Proficiency in Python and SQL
- Exposure to Kafka or Spark

Preferred qualifications
- Build production RAG pipelines with LangChain

We sponsor work visas for qualified candidates.
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
            "url": "",
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
        company="Northwind Analytics",
        title="Data Scientist",
        description=PRIMARY_JD,
        location="Boston, MA",
    )


@pytest.fixture
def fulltime_job(client) -> str:
    return _import(
        client,
        company="Harborline Data",
        title="Data Engineer, New Grad 2027",
        description=FULLTIME_JD,
        location="Remote (US)",
    )


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_enums_endpoint_exposes_every_vocabulary(client):
    body = client.get("/api/enums").json()
    assert len(body["application_status"]) == 15
    assert "BU_ALUM" in body["contact_type"]
    assert "REFERRAL_REQUESTED" in body["contact_status"]
    assert "AUTHORIZATION_CPT" in body["follow_up_kind"]
    assert "SUBMITTED_WITH_REFERRAL" in body["referral_state"]


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------


def test_status_transition_returns_warnings_but_succeeds(client, primary_job):
    response = client.post(f"/api/jobs/{primary_job}/status", json={"status": "APPLIED"})
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["to_status"] == "APPLIED"
    assert body["warnings"]


def test_unknown_status_is_rejected(client, primary_job):
    assert client.post(f"/api/jobs/{primary_job}/status", json={"status": "NOPE"}).status_code == 400


def test_application_endpoint_returns_timeline_and_stages(client, primary_job):
    for status in ("REVIEW", "SHORTLISTED", "APPLIED"):
        client.post(f"/api/jobs/{primary_job}/status", json={"status": status})

    body = client.get(f"/api/jobs/{primary_job}/application").json()
    assert body["current_status"] == "APPLIED"
    assert body["applied_at"] is not None
    assert {"REVIEW", "SHORTLISTED", "APPLIED"} <= set(body["stages_reached"])
    assert [e["to"] for e in body["timeline"]][-1] == "APPLIED"


def test_next_action_round_trip(client, primary_job):
    due = (date.today() + timedelta(days=3)).isoformat()
    response = client.post(
        f"/api/jobs/{primary_job}/next-action",
        json={"action": "Email the hiring manager", "due": due},
    )
    assert response.json()["next_action_due"] == due
    assert client.get(f"/api/jobs/{primary_job}/application").json()["next_action"] == (
        "Email the hiring manager"
    )


# ---------------------------------------------------------------------------
# People and outreach
# ---------------------------------------------------------------------------


def test_contact_create_and_list(client):
    created = client.post(
        "/api/contacts",
        json={
            "name": "Priya Raman",
            "company_name": "Northwind Analytics",
            "role": "Senior Data Scientist",
            "contact_type": "BU_ALUM",
            "warmth": "ALUMNI",
            "shared_context": "BU MSDS, two years ahead",
        },
    ).json()
    assert created["contact_type"] == "BU_ALUM"
    assert client.get("/api/contacts").json()["count"] == 1


def test_contact_requires_a_name(client):
    assert client.post("/api/contacts", json={"name": "  "}).status_code in (400, 422)


def test_draft_is_stored_unsent(client, primary_job):
    contact = client.post("/api/contacts", json={"name": "Priya"}).json()
    response = client.post(
        f"/api/contacts/{contact['contact_id']}/draft",
        json={"job_id": primary_job, "body": "Hi Priya — I'm a BU MSDS student..."},
    )
    body = response.json()
    assert body["sent_by_user"] is False
    assert "never sends messages" in body["note"]


def test_marking_sent_is_a_separate_explicit_call(client):
    contact = client.post("/api/contacts", json={"name": "Priya"}).json()
    draft = client.post(
        f"/api/contacts/{contact['contact_id']}/draft", json={"body": "Hello"}
    ).json()

    marked = client.post(f"/api/outreach/{draft['id']}/mark-sent").json()
    assert marked["sent_by_user"] is True
    assert marked["sent_at"]
    assert client.get("/api/contacts").json()["contacts"][0]["status"] == "OUTREACH_SENT"


def test_contacts_for_job_include_same_company(client, primary_job):
    client.post(
        "/api/contacts", json={"name": "Priya", "company_name": "Northwind Analytics, Inc."}
    )
    client.post("/api/contacts", json={"name": "Elsewhere", "company_name": "Other Co"})
    names = {c["name"] for c in client.get(f"/api/jobs/{primary_job}/contacts").json()["contacts"]}
    assert "Priya" in names
    assert "Elsewhere" not in names


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


def test_follow_up_lifecycle(client, primary_job):
    created = client.post(
        "/api/follow-ups",
        json={
            "subject": "Chase the recruiter",
            "due_date": date.today().isoformat(),
            "kind": "RECRUITER_RESPONSE",
            "priority": "HIGH",
            "job_id": primary_job,
        },
    ).json()
    assert created["kind"] == "RECRUITER_RESPONSE"
    assert client.get("/api/follow-ups").json()["count"] == 1

    client.post(f"/api/follow-ups/{created['id']}/complete")
    assert client.get("/api/follow-ups").json()["count"] == 0


def test_overdue_follow_ups_are_flagged(client):
    client.post(
        "/api/follow-ups",
        json={
            "subject": "Overdue thing",
            "due_date": (date.today() - timedelta(days=3)).isoformat(),
        },
    )
    row = client.get("/api/follow-ups").json()["follow_ups"][0]
    assert row["overdue"] is True


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------


def test_referral_duplicate_warns_over_http_but_does_not_block(client, primary_job):
    first = client.post(
        "/api/contacts", json={"name": "Priya", "company_name": "Northwind Analytics"}
    ).json()
    second = client.post(
        "/api/contacts", json={"name": "Sam", "company_name": "Northwind Analytics"}
    ).json()

    client.post(f"/api/jobs/{primary_job}/referrals", json={"contact_id": first["contact_id"]})
    response = client.post(
        f"/api/jobs/{primary_job}/referrals", json={"contact_id": second["contact_id"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is False
    assert body["state"] == "REQUESTED"
    assert "DUPLICATE_ASK_SAME_JOB" in {w["code"] for w in body["warnings"]}


def test_referral_state_surfaces_on_the_job(client, primary_job):
    contact = client.post(
        "/api/contacts", json={"name": "Priya", "company_name": "Northwind Analytics"}
    ).json()
    client.post(
        f"/api/jobs/{primary_job}/referrals",
        json={"contact_id": contact["contact_id"], "state": "RECEIVED"},
    )
    assert client.get(f"/api/jobs/{primary_job}/referrals").json()["job_state"] == "RECEIVED"
    assert client.get(f"/api/jobs/{primary_job}/application").json()["referral_used"] is True


# ---------------------------------------------------------------------------
# Resume recommendations
# ---------------------------------------------------------------------------


def test_resume_bases_are_available(client):
    names = {b["name"] for b in client.get("/api/resume-bases").json()["bases"]}
    assert names == {
        "DS_ML_AI",
        "ANALYTICS_PRODUCT",
        "DATA_ENGINEERING",
        "APPLIED_AI_SOFTWARE",
        "OPTIONAL_SOFTWARE",
    }


def test_recommendations_never_suggest_an_unsupported_keyword(client, fulltime_job):
    """The 2027 JD asks for RAG/LangChain. The advice must refuse it."""
    body = client.get(f"/api/jobs/{fulltime_job}/resume-recommendations").json()
    recs = body["recommendations"]
    assert recs

    writable = [r for r in recs if r["kind"] in {"EMPHASIZE", "SAFE_REWORD"}]
    for rec in writable:
        lowered = rec["body"].lower()
        assert "langchain" not in lowered
        assert " rag " not in f" {lowered} "

    do_not_add = [r for r in recs if r["kind"] == "DO_NOT_ADD"]
    assert do_not_add
    assert any("RAG" in r["body"] for r in do_not_add)


def test_recommendations_are_cached_then_refreshable(client, primary_job):
    first = client.get(f"/api/jobs/{primary_job}/resume-recommendations").json()
    assert first["cached"] is False

    second = client.get(f"/api/jobs/{primary_job}/resume-recommendations").json()
    assert second["cached"] is True
    assert len(second["recommendations"]) == len(first["recommendations"])

    refreshed = client.get(
        f"/api/jobs/{primary_job}/resume-recommendations", params={"refresh": True}
    ).json()
    assert refreshed["cached"] is False


# ---------------------------------------------------------------------------
# Gaps and execution
# ---------------------------------------------------------------------------


def test_skill_gaps_ignore_merely_discovered_jobs(client, fulltime_job):
    body = client.get("/api/skill-gaps").json()
    assert body["jobs_considered"] == 0
    assert body["gaps"] == []
    assert "SHORTLISTED and APPLIED" in body["population"]


def test_skill_gaps_appear_after_shortlisting(client, fulltime_job):
    client.post(f"/api/jobs/{fulltime_job}/status", json={"status": "SHORTLISTED"})
    body = client.get("/api/skill-gaps").json()
    assert body["jobs_considered"] == 1
    assert body["small_sample"] is True
    assert {g["skill"] for g in body["gaps"]}


def test_execution_panel_surfaces_stalled_work(client, primary_job):
    client.post(f"/api/jobs/{primary_job}/status", json={"status": "SHORTLISTED"})
    panel = client.get("/api/execution").json()

    assert [r["job_id"] for r in panel["applications_needing_action"]] == [primary_job]
    assert [r["job_id"] for r in panel["resume_recommendations_pending"]] == [primary_job]

    client.get(f"/api/jobs/{primary_job}/resume-recommendations")
    assert client.get("/api/execution").json()["resume_recommendations_pending"] == []


def test_execution_panel_counts_people_and_referrals(client, primary_job):
    contact = client.post(
        "/api/contacts", json={"name": "Priya", "company_name": "Northwind Analytics"}
    ).json()
    client.post(f"/api/jobs/{primary_job}/referrals", json={"contact_id": contact["contact_id"]})

    panel = client.get("/api/execution").json()
    assert panel["people_contacted"]["total"] == 1
    assert panel["referrals"]["total"] == 1
    assert panel["referrals"]["REQUESTED"] == 1


# ---------------------------------------------------------------------------
# The required end-to-end flow
# ---------------------------------------------------------------------------


def _walk_full_flow(client, job_id: str, *, company: str, expected_lane: str) -> dict:
    """JOB → SHORTLIST → RESUME REC → CONTACT → FOLLOW-UP → REFERRAL → APPLIED."""
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["job"]["lane"] == expected_lane

    # SHORTLIST
    assert (
        client.post(f"/api/jobs/{job_id}/status", json={"status": "SHORTLISTED"}).json()[
            "to_status"
        ]
        == "SHORTLISTED"
    )

    # RESUME RECOMMENDATION
    recs = client.get(f"/api/jobs/{job_id}/resume-recommendations").json()["recommendations"]
    assert recs

    # ADD CONTACT
    contact = client.post(
        "/api/contacts",
        json={"name": f"Contact at {company}", "company_name": company, "contact_type": "BU_ALUM"},
    ).json()

    # CREATE FOLLOW-UP
    follow_up = client.post(
        "/api/follow-ups",
        json={
            "subject": f"Follow up with {contact['name']}",
            "due_date": date.today().isoformat(),
            "kind": "NETWORKING_OUTREACH",
            "job_id": job_id,
            "contact_id": contact["contact_id"],
        },
    ).json()
    assert follow_up["job_id"] == job_id

    # REFERRAL STATE
    referral = client.post(
        f"/api/jobs/{job_id}/referrals",
        json={"contact_id": contact["contact_id"], "state": "RECEIVED"},
    ).json()
    assert referral["state"] == "RECEIVED"

    # READY_TO_APPLY → APPLIED
    client.post(f"/api/jobs/{job_id}/status", json={"status": "READY_TO_APPLY"})
    client.post(f"/api/jobs/{job_id}/status", json={"status": "APPLIED", "note": "Submitted"})

    # STATUS HISTORY
    application = client.get(f"/api/jobs/{job_id}/application").json()
    assert application["current_status"] == "APPLIED"
    assert application["referral_used"] is True
    reached = set(application["stages_reached"])
    assert {"SHORTLISTED", "READY_TO_APPLY", "APPLIED"} <= reached
    assert len(application["timeline"]) >= 3

    return application


def test_full_flow_for_a_primary_job(client, primary_job):
    _walk_full_flow(
        client,
        primary_job,
        company="Northwind Analytics",
        expected_lane="FULL_TIME_2027",
    )

    # DASHBOARD UPDATE
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["kpis"]["applied"] == 1
    assert dashboard["kpis"]["shortlisted"] == 0  # moved past it

    execution = client.get("/api/execution").json()
    assert execution["people_contacted"]["total"] == 1
    assert execution["referrals"]["RECEIVED"] == 1

    gaps = client.get("/api/skill-gaps").json()
    assert gaps["jobs_considered"] == 1


def test_full_flow_for_a_full_time_2027_job(client, fulltime_job):
    _walk_full_flow(client, fulltime_job, company="Harborline Data", expected_lane="FULL_TIME_2027")

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["kpis"]["applied"] == 1
    assert dashboard["lane_counts"]["FULL_TIME_2027"] == 1

    # The 2027 lane must surface sponsorship, which the JD states explicitly.
    detail = client.get(f"/api/jobs/{fulltime_job}").json()
    assert detail["sponsorship"]["role_signal"] == "SPONSORSHIP_CONFIRMED"


def test_two_jobs_run_independently(client, primary_job, fulltime_job):
    for job_id, company in ((primary_job, "Northwind Analytics"), (fulltime_job, "Harborline Data")):
        client.post(f"/api/jobs/{job_id}/status", json={"status": "SHORTLISTED"})
        client.post("/api/contacts", json={"name": f"P-{company}", "company_name": company})

    # One lane since D-51, so the two queries describe the same population.
    # The property under test is that the filter is applied and the population
    # is reported, not that two lanes disagree.
    all_gaps = client.get("/api/skill-gaps", params={"lane": "ALL"}).json()
    full_gaps = client.get("/api/skill-gaps", params={"lane": "FULL_TIME_2027"}).json()
    assert all_gaps["jobs_considered"] == 2
    assert full_gaps["jobs_considered"] <= all_gaps["jobs_considered"]
    assert "FULL_TIME_2027" in full_gaps["population"]
    assert client.get("/api/skill-gaps").json()["jobs_considered"] == 2


def test_authorization_is_never_confirmed_through_the_api(client, primary_job, fulltime_job):
    """The Slice 1 invariant must survive every Slice 2 write path."""
    for job_id in (primary_job, fulltime_job):
        client.post(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["eligibility"]["authorization_state"] != "AUTHORIZED_CONFIRMED"
