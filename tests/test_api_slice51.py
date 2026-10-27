"""Slice 5.1 over the real HTTP surface.

Drives the endpoints exactly as the Next.js frontend does, so the contract the
generated TypeScript client is built from is the thing under test.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import url_fetch, url_import
from tests.test_url_import import (
    GREENHOUSE_PAYLOAD,
    GREENHOUSE_URL,
    JSON_LD_PAGE,
)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/acmerobotics/jobs/4455667?content=true"

ANALYTICS_JD = """Analytics Engineer, Data Science — Boston, MA. Full-time.

About the Role
- Build and maintain canonical datasets by developing reliable ETL/ELT pipelines
- Collaborate with business stakeholders to translate scope into data requirements
- Design metrics and data visualizations with dashboarding tools like Tableau and Sigma

Requirements
- Strong proficiency in SQL for data transformation, comfort in Python or Scala
- Experience in writing data quality checks to validate data integrity
- Experience creating reporting solutions using dashboarding tools (e.g. Looker, Tableau)
- Strong communication skills with technical and non-technical teams
"""


@pytest.fixture
def client(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import app.db as db_module
    from app.main import create_app

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)
    # Fixture hostnames do not resolve; the address guard itself still runs.
    monkeypatch.setattr(url_fetch, "_resolve", lambda hostname: ["93.184.216.34"])
    return TestClient(create_app())


def _route(monkeypatch, routes: dict[str, tuple[int, str, str]]) -> None:
    """Point the importer's extractor at recorded responses."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in routes:
            raise AssertionError(f"unrouted request: {key}")
        status, content_type, body = routes[key]
        return httpx.Response(status, headers={"content-type": content_type}, text=body)

    original = url_import.PostingExtractor.__init__

    def patched(self, client=None):
        original(
            self,
            httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        )

    monkeypatch.setattr(url_import.PostingExtractor, "__init__", patched)


def _import_text_job(client, *, title: str, description: str, company: str, location: str) -> str:
    """Seed a normal (non-URL) job through the existing form importer."""
    response = client.post(
        "/import",
        data={
            "company_name": company,
            "title": title,
            "location": location,
            "url": "",
            "deadline": "",
            "description": description,
            "bulk_payload": "",
            "bulk_format": "json",
        },
    )
    assert response.status_code == 200
    jobs = client.get("/api/jobs?relevant_only=false&limit=50").json()["jobs"]
    return next(j["job_id"] for j in jobs if j["title"] == title)


# ---------------------------------------------------------------------------
# Role targeting endpoint
# ---------------------------------------------------------------------------


def test_resume_targeting_explains_the_positioning(client):
    job_id = _import_text_job(
        client,
        company="Example Delivery Co",
        title="Analytics Engineer, Data Science",
        description=ANALYTICS_JD,
        location="Boston, MA",
    )
    payload = client.get(f"/api/jobs/{job_id}/resume-targeting").json()

    assert payload["role_archetype"] == "ANALYTICS_ENGINEERING"
    assert payload["resume_base"] == "DATA_ENGINEERING"
    assert payload["intensity"] == "BALANCED"

    targeting = payload["targeting"]
    assert "SQL" in targeting["prioritized"]
    assert targeting["deemphasized"]
    assert targeting["lead_projects"]

    audit = payload["audit"]
    assert audit["unsupported_keyword_leakage"] == 0
    assert audit["ownership_risk_count"] == 0
    assert audit["clean"] is True
    # Internal QA only. No invented ATS score, and it says so.
    assert "ats" not in json.dumps(audit).lower().replace("prediction", "")


def test_targeting_intensity_is_validated(client):
    job_id = _import_text_job(
        client,
        company="Example Delivery Co",
        title="Analytics Engineer, Data Science",
        description=ANALYTICS_JD,
        location="Boston, MA",
    )
    assert (
        client.get(f"/api/jobs/{job_id}/resume-targeting?intensity=CONSERVATIVE").status_code == 200
    )
    bad = client.get(f"/api/jobs/{job_id}/resume-targeting?intensity=MAXIMUM")
    assert bad.status_code == 400


def test_prepare_application_picks_the_base_from_the_archetype(client):
    """The role family says DATA_SCIENTIST; the posting says analytics
    engineering. The posting wins."""
    job_id = _import_text_job(
        client,
        company="Example Delivery Co",
        title="Analytics Engineer, Data Science",
        description=ANALYTICS_JD,
        location="Boston, MA",
    )
    response = client.post(f"/api/jobs/{job_id}/prepare-application")
    assert response.status_code == 200
    assert response.json()["resume_base"] == "DATA_ENGINEERING"


def test_an_explicit_base_still_overrides_the_archetype(client):
    job_id = _import_text_job(
        client,
        company="Example Delivery Co",
        title="Analytics Engineer, Data Science",
        description=ANALYTICS_JD,
        location="Boston, MA",
    )
    response = client.post(f"/api/jobs/{job_id}/prepare-application?base=DS_ML_AI")
    assert response.json()["resume_base"] == "DS_ML_AI"


# ---------------------------------------------------------------------------
# Manual URL import
# ---------------------------------------------------------------------------


def test_import_url_end_to_end(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    response = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL, "intent": "REVIEW"})
    assert response.status_code == 200
    payload = response.json()

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["company"] == "Acme Robotics"
    assert payload["provider"] == "GREENHOUSE"
    assert payload["external_id"] == "4455667"
    assert payload["description_chars"] > 100
    assert payload["application_status"] == "REVIEW"

    # And it behaves like any other job from here on.
    detail = client.get(f"/api/jobs/{payload['job_id']}").json()
    assert detail["job"]["discovery_source"] == "USER_URL"
    assert detail["requirements"]
    assert detail["priority"] is not None


def test_import_with_save_intent_leaves_the_job_at_discovered(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    payload = client.post(
        "/api/jobs/import-url", json={"url": GREENHOUSE_URL, "intent": "SAVE"}
    ).json()
    assert payload["application_status"] == "DISCOVERED"


def test_import_never_marks_a_job_applied(client, monkeypatch):
    """No import path may record a submission. That needs a date and a resume."""
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    for intent in ("SAVE", "REVIEW", "APPLIED"):
        payload = client.post(
            "/api/jobs/import-url", json={"url": GREENHOUSE_URL, "intent": intent}
        ).json()
        assert payload["application_status"] != "APPLIED"


def test_importing_a_known_url_reports_the_duplicate(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    first = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()
    second = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()

    assert second["duplicate"] is True
    assert second["existing_job_id"] == first["job_id"]
    assert client.get("/api/jobs?relevant_only=false").json()["count"] == 1


def test_a_blocked_url_returns_a_reason_not_a_500(client):
    response = client.post("/api/jobs/import-url", json={"url": "http://169.254.169.254/latest/"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error_code"] in {"PRIVATE_HOST", "PRIVATE_ADDRESS"}
    assert payload["can_paste_description"] is False


def test_an_unreadable_page_offers_the_paste_fallback(client, monkeypatch):
    url = "https://example.com/blog/hiring"
    _route(monkeypatch, {url: (200, "text/html", "<html><body>nothing</body></html>")})
    payload = client.post("/api/jobs/import-url", json={"url": url}).json()

    assert payload["ok"] is False
    assert payload["can_paste_description"] is True
    assert client.get("/api/jobs?relevant_only=false").json()["count"] == 0


def test_the_paste_fallback_creates_a_full_record(client):
    payload = client.post(
        "/api/jobs/import-url",
        json={
            "url": "https://beacon.example.com/careers/1",
            "company_name": "Beacon Analytics",
            "title": "Analytics Engineer",
            "description": ANALYTICS_JD,
            "intent": "REVIEW",
        },
    ).json()

    assert payload["ok"] is True
    detail = client.get(f"/api/jobs/{payload['job_id']}").json()
    assert detail["requirements"]


def test_save_url_only_creates_a_minimal_record(client):
    payload = client.post(
        "/api/jobs/import-url",
        json={
            "url": "https://weird-portal.example.com/req/9981",
            "save_url_only": True,
            "company_name": "Orion Data",
            "title": "Data Engineer",
        },
    ).json()
    assert payload["ok"] is True

    action = client.get(f"/api/jobs/{payload['job_id']}/next-action").json()
    assert action["action"] == "Verify this listing"


def test_json_ld_import_works_over_http(client, monkeypatch):
    url = "https://vantagelabs.example.com/careers/ml"
    _route(monkeypatch, {url: (200, "text/html", JSON_LD_PAGE)})
    payload = client.post("/api/jobs/import-url", json={"url": url}).json()
    assert payload["ok"] is True
    assert payload["company"] == "Vantage Labs"
    assert payload["provider"] == "SCHEMA_ORG"


# ---------------------------------------------------------------------------
# Already applied
# ---------------------------------------------------------------------------


def test_already_applied_uses_the_append_only_transition_system(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    job_id = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()["job_id"]

    response = client.post(
        f"/api/jobs/{job_id}/already-applied",
        json={
            "applied_at": date.today().isoformat(),
            "external_resume": True,
            "confirmation_id": "REQ-88213",
            "notes": "Applied via the company site before importing.",
        },
    )
    assert response.status_code == 200
    assert response.json()["to_status"] == "APPLIED"

    application = client.get(f"/api/jobs/{job_id}/application").json()
    assert application["current_status"] == "APPLIED"
    assert application["applied_at"]
    # History is appended, never rewritten.
    assert [e["to"] for e in application["timeline"]][-1] == "APPLIED"
    assert len(application["timeline"]) >= 2
    notes = " ".join(n["body"] for n in application["notes"])
    assert "external resume" in notes.lower()
    assert "REQ-88213" in notes


def test_already_applied_rejects_a_resume_version_from_another_job(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    job_id = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()["job_id"]
    response = client.post(
        f"/api/jobs/{job_id}/already-applied",
        json={"applied_at": date.today().isoformat(), "resume_version_id": "RSV-nonexistent"},
    )
    assert response.status_code == 400


def test_already_applied_validates_the_date(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    job_id = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()["job_id"]
    response = client.post(
        f"/api/jobs/{job_id}/already-applied", json={"applied_at": "last Tuesday"}
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Next actions
# ---------------------------------------------------------------------------


def test_next_action_is_specific_at_every_stage(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    job_id = client.post(
        "/api/jobs/import-url", json={"url": GREENHOUSE_URL, "intent": "SAVE"}
    ).json()["job_id"]

    seen: list[str] = []
    for status, expected in [
        ("DISCOVERED", "Review fit and eligibility"),
        ("REVIEW", "Prepare the application"),
        ("SHORTLISTED", "referral"),
        ("READY_TO_APPLY", "Submit the application"),
        ("APPLIED", "Track the response"),
        ("RECRUITER_SCREEN", "recruiter-screen"),
        ("INTERVIEW_1", "Prepare for this round"),
        ("OFFER", "Review the offer"),
    ]:
        if status != "DISCOVERED":
            client.post(f"/api/jobs/{job_id}/status", json={"status": status})
        action = client.get(f"/api/jobs/{job_id}/next-action").json()
        assert expected.lower() in action["action"].lower(), (status, action)
        # Never a content-free instruction.
        assert len(action["detail"]) > 20
        assert action["urgency"] in {"URGENT", "SOON", "NORMAL"}
        seen.append(action["action"])

    assert len(set(seen)) == len(seen), "each stage must give a different instruction"


def test_next_actions_can_be_filtered_to_user_added_jobs(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL})
    _import_text_job(
        client,
        company="Example Delivery Co",
        title="Analytics Engineer, Data Science",
        description=ANALYTICS_JD,
        location="Boston, MA",
    )

    everything = client.get("/api/next-actions").json()["actions"]
    assert len(everything) == 2
    # The filter is a view over the same list, not a separate feature.
    user_added = client.get("/api/next-actions?source=USER_ADDED").json()["actions"]
    assert len(user_added) == 2


# ---------------------------------------------------------------------------
# Dashboard / list integration
# ---------------------------------------------------------------------------


def test_an_imported_job_appears_in_the_normal_lists(client, monkeypatch):
    """Not isolated into a mini-app: it counts everywhere a job counts."""
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    job_id = client.post(
        "/api/jobs/import-url", json={"url": GREENHOUSE_URL, "intent": "REVIEW"}
    ).json()["job_id"]

    listed = client.get("/api/jobs").json()
    assert job_id in {j["job_id"] for j in listed["jobs"]}

    review = client.get("/api/jobs?crm_state=REVIEW").json()
    assert job_id in {j["job_id"] for j in review["jobs"]}

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["kpis"]["jobs_found"] >= 1


def test_the_source_filter_separates_user_added_from_automated(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    imported = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()["job_id"]

    user_added = client.get("/api/jobs?source=USER_ADDED&relevant_only=false").json()
    assert imported in {j["job_id"] for j in user_added["jobs"]}

    automated = client.get("/api/jobs?source=AUTOMATED&relevant_only=false").json()
    assert imported not in {j["job_id"] for j in automated["jobs"]}


def test_applied_imported_jobs_count_in_applications_this_week(client, monkeypatch):
    _route(monkeypatch, {GREENHOUSE_API: (200, "application/json", json.dumps(GREENHOUSE_PAYLOAD))})
    job_id = client.post("/api/jobs/import-url", json={"url": GREENHOUSE_URL}).json()["job_id"]
    client.post(
        f"/api/jobs/{job_id}/already-applied",
        json={"applied_at": date.today().isoformat(), "external_resume": True},
    )

    execution = client.get("/api/execution").json()
    assert execution["applications_this_week"] >= 1
