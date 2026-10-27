"""Workday connector: URL parsing, listing, normalization, and the collection path.

Every request goes to a fake Workday server built on ``httpx.MockTransport``.
The fake reproduces the behaviour verified against live tenants on 2026-10-16
rather than an idealised API: pages of 20, ``total`` only on the first page, a
country facet nested inside a location group, 404 for a wrong site and 422 for
a wrong tenant or pod.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.connectors.base import SourceConfig
from app.connectors.http import HttpClient
from app.connectors.workday import (
    WorkdayBoard,
    WorkdayConnector,
    parse_board_identifier,
    parse_workday_url,
    posting_id,
)
from app.models.enums import JobLifecycleStatus, SourceCapability
from app.models.ingestion import Job, JobSourceAssociation
from app.services.collection import CollectionService
from app.services.dedupe import CanonicalJobResolver
from app.services.pipeline import JobPipeline, RawJobInput

HOST = "acme.wd5.myworkdayjobs.com"
BOARD = f"{HOST}/AcmeCareers"
US_ID = "bc33aa3152ec42d4995f4791a106ed09"

DATA_SCIENTIST_JD = """About the role
Acme is hiring a Data Scientist to join the Product Analytics team in Boston, MA.
This is a full-time role for recent graduates starting in January 2027.

What you'll do
- Build models in Python and SQL to forecast customer demand
- Design and analyze A/B experiments with product managers
- Present findings to stakeholders

Requirements
- Master's degree in Data Science, Statistics or a related field
- Strong proficiency in Python, SQL and pandas
- Experience with statistical modeling and experimentation
"""


# ---------------------------------------------------------------------------
# A fake Workday tenant
# ---------------------------------------------------------------------------


class FakeWorkday:
    """Serves one Workday site the way the real endpoint does."""

    def __init__(self, postings: list[dict[str, Any]], *, us_postings: list[str] | None = None):
        self.postings = postings
        #: externalPaths visible under the US facet; None = no country facet.
        self.us_postings = us_postings
        self.list_calls: list[dict[str, Any]] = []
        self.detail_calls: list[str] = []
        self.fail_list_with: int | None = None
        self.fail_detail_for: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/wday/cxs/acme/AcmeCareers"):
            if path.startswith("/wday/cxs/acme/"):
                return httpx.Response(404, json={"errorCode": "S21"})
            return httpx.Response(422, json={"errorCode": "HTTP_422"})
        rest = path.removeprefix("/wday/cxs/acme/AcmeCareers")
        if rest == "/jobs" and request.method == "POST":
            if self.fail_list_with:
                return httpx.Response(self.fail_list_with)
            body = json.loads(request.content)
            self.list_calls.append(body)
            visible = self.postings
            if body["appliedFacets"] and self.us_postings is not None:
                visible = [p for p in visible if p["externalPath"] in self.us_postings]
            offset, limit = body["offset"], min(body["limit"], 20)
            page = visible[offset : offset + limit]
            return httpx.Response(
                200,
                json={
                    # The real endpoint: total on page one only.
                    "total": len(visible) if offset == 0 else 0,
                    "jobPostings": [
                        {
                            "title": p["title"],
                            "externalPath": p["externalPath"],
                            "locationsText": p.get("location", ""),
                            "postedOn": "Posted Today",
                            "bulletFields": [p["externalPath"].rsplit("_", 1)[-1]],
                        }
                        for p in page
                    ],
                    "facets": self._facets() if offset == 0 else [],
                },
            )
        if rest.startswith("/job/") and request.method == "GET":
            self.detail_calls.append(rest)
            if rest in self.fail_detail_for:
                return httpx.Response(500)
            match = next((p for p in self.postings if p["externalPath"] == rest), None)
            if match is None:
                return httpx.Response(404)
            pid = rest.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "jobPostingInfo": {
                        "id": "x" + pid,
                        "title": match["title"],
                        "jobDescription": match.get("html", f"<p>{DATA_SCIENTIST_JD}</p>"),
                        "location": match.get("location", "Boston, MA"),
                        "startDate": match.get("posted", "2026-10-14"),
                        "timeType": "Full time",
                        "jobReqId": pid.rsplit("_", 1)[-1],
                        "jobPostingId": pid,
                        "externalUrl": f"https://{BOARD}{rest}",
                        **({"remoteType": match["remote"]} if match.get("remote") else {}),
                    },
                    "hiringOrganization": {"name": "", "url": ""},
                },
            )
        return httpx.Response(404)

    def _facets(self) -> list[dict[str, Any]]:
        if self.us_postings is None:
            return [{"facetParameter": "timeType", "values": [{"descriptor": "Full time", "id": "ft"}]}]
        return [
            {
                "facetParameter": "locationMainGroup",
                "values": [
                    {
                        "facetParameter": "locationCountry",
                        "values": [
                            {"descriptor": "Canada", "id": "ca", "count": 1},
                            {"descriptor": "United States of America", "id": US_ID, "count": 2},
                        ],
                    },
                    {
                        "facetParameter": "locations",
                        "values": [
                            {"descriptor": "Austin, Texas, United States of America", "id": "aus"}
                        ],
                    },
                ],
            }
        ]

    def client(self) -> HttpClient:
        return HttpClient(
            transport=httpx.MockTransport(self.handler), sleep=lambda _: None, max_retries=1
        )


def _posting(n: int, title: str = "Data Scientist", **extra: Any) -> dict[str, Any]:
    return {"title": f"{title} {n}", "externalPath": f"/job/Boston-MA/Data-Scientist-{n}_R{n:05d}", **extra}


def _config(**overrides: Any) -> SourceConfig:
    base: dict[str, Any] = {
        "company_name": "Acme Analytics",
        "ats_type": "WORKDAY",
        "ats_identifier": BOARD,
        "active": True,
        "automated_collection_enabled": True,
    }
    base.update(overrides)
    return SourceConfig(**base)


def _connector(fake: FakeWorkday) -> WorkdayConnector:
    return WorkdayConnector(client=fake.client())


# ---------------------------------------------------------------------------
# URL parsing — no network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "identifier", "path"),
    [
        (
            "https://capitalone.wd12.myworkdayjobs.com/Capital_One",
            "capitalone.wd12.myworkdayjobs.com/Capital_One",
            None,
        ),
        (
            "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/job/McLean-VA/Data-Scientist_R123?src=li",
            "capitalone.wd12.myworkdayjobs.com/Capital_One",
            "/job/McLean-VA/Data-Scientist_R123",
        ),
        (
            "https://wd1.myworkdaysite.com/en-US/recruiting/acme/External/job/Boston/Analyst_JR9",
            "wd1.myworkdaysite.com/recruiting/acme/External",
            "/job/Boston/Analyst_JR9",
        ),
        (
            "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/job/San-Jose/X_R1",
            "adobe.wd5.myworkdayjobs.com/external_experienced",
            "/job/San-Jose/X_R1",
        ),
    ],
)
def test_workday_urls_resolve_to_an_exact_board(url, identifier, path):
    parsed = parse_workday_url(url)
    assert parsed is not None
    board, posting_path = parsed
    assert board.identifier == identifier
    assert posting_path == path


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.acme.com/jobs/123",  # a branded domain: tenant unknowable
        "https://www.myworkdayjobs.com/",
        "https://acme.wd5.myworkdayjobs.com/",  # no site
        "https://wd1.myworkdaysite.com/en-US/recruiting/acme",  # no site
        "https://boards.greenhouse.io/acme/jobs/1",
        "",
    ],
)
def test_non_workday_or_incomplete_urls_are_not_guessed(url):
    assert parse_workday_url(url) is None


def test_board_identity_carries_tenant_site_and_pod():
    board = parse_board_identifier("capitalone.wd12.myworkdayjobs.com/Capital_One")
    assert board == WorkdayBoard("capitalone.wd12.myworkdayjobs.com", "capitalone", "Capital_One")
    assert board.api_base == "https://capitalone.wd12.myworkdayjobs.com/wday/cxs/capitalone/Capital_One"


def test_posting_id_is_the_last_path_segment():
    assert posting_id("/job/McLean-VA/Data-Scientist_R123") == "Data-Scientist_R123"
    assert posting_id(None) is None


def test_a_pasted_workday_posting_and_the_connector_share_an_identity():
    hint = CanonicalJobResolver().resolve(
        f"https://{HOST}/en-US/AcmeCareers/job/Boston-MA/Data-Scientist-1_R00001"
    )
    assert hint.ats_platform == "WORKDAY"
    assert hint.ats_org_slug == "acme"
    assert hint.ats_job_id == posting_id("/job/Boston-MA/Data-Scientist-1_R00001")


# ---------------------------------------------------------------------------
# Connector behaviour
# ---------------------------------------------------------------------------


def test_workday_is_registered_honestly():
    connector = WorkdayConnector()
    assert connector.capability is SourceCapability.UNDOCUMENTED_PUBLIC_ENDPOINT
    assert connector.reuses_known_postings is True


def test_the_registry_refuses_a_bare_workday_tenant(session):
    """A tenant name alone is not a board: the site and pod are its identity."""
    from app.services import monitored_companies

    outcome = monitored_companies.monitor(
        session, company_name="Acme", ats_type="WORKDAY", ats_identifier="acme"
    )
    assert not outcome.ok
    assert outcome.reason == "INVALID_BOARD"


def test_validate_config_rejects_a_bare_tenant():
    result = WorkdayConnector().validate_config(_config(ats_identifier="acme"))
    assert not result.ok


def test_listing_walks_every_page_despite_page_two_total_of_zero():
    fake = FakeWorkday([_posting(n) for n in range(45)])
    result = _connector(fake).collect(_config(connector_config={"max_detail_fetches": 100}))
    assert result.ok
    assert result.listing_complete is True
    assert [c["offset"] for c in fake.list_calls] == [0, 20, 40]
    assert result.records_valid == 45
    assert result.records_received == 45


def test_the_us_country_facet_is_found_by_value_and_applied():
    postings = [_posting(1), _posting(2), _posting(3, location="Toronto, ON")]
    fake = FakeWorkday(postings, us_postings=[postings[0]["externalPath"], postings[1]["externalPath"]])
    result = _connector(fake).collect(_config())
    assert fake.list_calls[-1]["appliedFacets"] == {"locationCountry": [US_ID]}
    assert result.records_valid == 2


def test_a_site_without_a_country_facet_is_listed_in_full():
    fake = FakeWorkday([_posting(1), _posting(2, location="Toronto, ON")])
    result = _connector(fake).collect(_config())
    assert all(call["appliedFacets"] == {} for call in fake.list_calls)
    assert result.records_valid == 2


def test_normalization_maps_the_posting_onto_the_common_contract():
    fake = FakeWorkday(
        [
            _posting(
                7,
                html="<p>About</p><ul><li>Python &amp; SQL</li><li>A/B tests</li></ul>",
                remote="Hybrid",
                posted="2026-10-02",
            )
        ]
    )
    record = _connector(fake).collect(_config()).records[0]
    assert isinstance(record, RawJobInput)
    assert record.company_name == "Acme Analytics"
    assert record.title == "Data Scientist 7"
    assert record.external_id == "Data-Scientist-7_R00007"
    assert record.url == f"https://{BOARD}/job/Boston-MA/Data-Scientist-7_R00007"
    assert record.location == "Boston, MA (Hybrid)"
    assert str(record.posted_date) == "2026-10-02"
    assert "- Python & SQL" in record.description
    assert "<" not in record.description
    assert record.connector == "workday"
    assert record.source_type == "WORKDAY"


def test_known_postings_are_observed_not_refetched():
    fake = FakeWorkday([_posting(n) for n in range(5)])
    known = {"Data-Scientist-0_R00000", "Data-Scientist-1_R00001", "Data-Scientist-2_R00002"}
    result = _connector(fake).collect(_config(), known_external_ids=known)
    assert len(fake.detail_calls) == 2
    assert result.observed_external_ids == known
    assert {r.external_id for r in result.records} == {
        "Data-Scientist-3_R00003",
        "Data-Scientist-4_R00004",
    }
    assert result.records_received == 5


def test_the_detail_budget_defers_new_postings_without_losing_them():
    fake = FakeWorkday([_posting(n) for n in range(6)])
    result = _connector(fake).collect(_config(connector_config={"max_detail_fetches": 2}))
    assert result.records_valid == 2
    assert len(result.observed_external_ids) == 4
    assert result.listing_complete is True


def test_a_page_bound_marks_the_listing_incomplete():
    fake = FakeWorkday([_posting(n) for n in range(45)])
    result = _connector(fake).collect(_config(connector_config={"max_pages": 2}))
    assert result.listing_complete is False


def test_one_unreadable_posting_does_not_fail_the_board():
    postings = [_posting(1), _posting(2)]
    fake = FakeWorkday(postings)
    fake.fail_detail_for = {postings[0]["externalPath"]}
    result = _connector(fake).collect(_config())
    assert result.ok
    assert [r.external_id for r in result.records] == ["Data-Scientist-2_R00002"]
    assert "Data-Scientist-1_R00001" in result.observed_external_ids


def test_an_empty_board_is_a_valid_answer():
    result = _connector(FakeWorkday([])).collect(_config())
    assert result.ok
    assert result.records == []
    assert result.listing_complete is True


def test_a_wrong_site_is_a_capability_failure_not_an_outage():
    result = _connector(FakeWorkday([])).collect(_config(ats_identifier=f"{HOST}/NoSuchSite"))
    assert result.capability_failure is True


def test_a_wrong_tenant_or_pod_422_is_a_capability_failure():
    result = _connector(FakeWorkday([])).collect(
        _config(ats_identifier="other.wd1.myworkdayjobs.com/Site")
    )
    assert result.capability_failure is True
    assert "422" in result.errors[0]


def test_a_server_error_is_a_failure_not_a_capability_answer():
    fake = FakeWorkday([_posting(1)])
    fake.fail_list_with = 503
    result = _connector(fake).collect(_config())
    assert result.errors
    assert result.capability_failure is False


def test_health_check_reads_one_page_and_no_bodies():
    fake = FakeWorkday([_posting(n) for n in range(3)], us_postings=[])
    health = _connector(fake).health_check(_config())
    assert health.reachable is True
    assert health.postings_visible == 3
    assert fake.detail_calls == []


# ---------------------------------------------------------------------------
# Through the collection service and the pipeline
# ---------------------------------------------------------------------------


def _service(session, fake: FakeWorkday) -> CollectionService:
    return CollectionService(session, connector_factory=lambda _t, **_k: _connector(fake))


def _jobs(session) -> list[Job]:
    return session.execute(select(Job).where(Job.discovery_source == "WORKDAY")).scalars().all()


def test_collected_workday_jobs_reach_the_job_table(session):
    fake = FakeWorkday([_posting(1), _posting(2)])
    outcome = _service(session, fake).collect_company(_config())
    session.flush()
    assert outcome.ok, outcome.errors
    assert outcome.jobs_new == 2
    jobs = _jobs(session)
    assert {j.ats_platform for j in jobs} == {"WORKDAY"}
    assert {j.ats_org_slug for j in jobs} == {"acme"}
    assert {j.ats_job_id for j in jobs} == {"Data-Scientist-1_R00001", "Data-Scientist-2_R00002"}
    # Classification consumed the canonical job; it never needed to know the source.
    assert all(j.functional_classifier_version for j in jobs)


def test_a_second_poll_fetches_only_new_postings(session):
    fake = FakeWorkday([_posting(1), _posting(2)])
    service = _service(session, fake)
    service.collect_company(_config())
    session.flush()
    fake.detail_calls.clear()

    fake.postings.append(_posting(3))
    outcome = service.collect_company(_config())
    session.flush()
    assert fake.detail_calls == ["/job/Boston-MA/Data-Scientist-3_R00003"]
    assert outcome.jobs_new == 1
    assert outcome.jobs_unchanged == 2
    assert len(_jobs(session)) == 3


def test_observed_postings_stay_open_and_missing_ones_age_out(session):
    fake = FakeWorkday([_posting(1), _posting(2)])
    service = _service(session, fake)
    service.collect_company(_config())
    session.flush()

    fake.postings.pop()  # posting 2 leaves the board
    for _ in range(service.misses_before_suspect):
        service.collect_company(_config())
    session.flush()
    by_id = {j.ats_job_id: j for j in _jobs(session)}
    assert by_id["Data-Scientist-1_R00001"].lifecycle_status == JobLifecycleStatus.OPEN.value
    assert by_id["Data-Scientist-1_R00001"].consecutive_misses == 0
    assert (
        by_id["Data-Scientist-2_R00002"].lifecycle_status
        == JobLifecycleStatus.POSSIBLY_CLOSED.value
    )


def test_a_failed_poll_never_counts_as_a_disappearance(session):
    fake = FakeWorkday([_posting(1)])
    service = _service(session, fake)
    service.collect_company(_config())
    session.flush()

    fake.fail_list_with = 503
    for _ in range(service.misses_before_closed + 1):
        service.collect_company(_config())
    session.flush()
    (job,) = _jobs(session)
    assert job.consecutive_misses == 0
    assert job.lifecycle_status == JobLifecycleStatus.OPEN.value


def test_an_incomplete_listing_never_counts_as_a_disappearance(session):
    fake = FakeWorkday([_posting(n) for n in range(25)])
    service = _service(session, fake)
    service.collect_company(_config())
    session.flush()

    for _ in range(service.misses_before_closed + 1):
        service.collect_company(_config(connector_config={"max_pages": 1}))
    session.flush()
    assert all(j.consecutive_misses == 0 for j in _jobs(session))


def test_a_pasted_copy_and_a_collected_copy_are_one_job(session):
    """Manual import first, then collection: the dedupe must merge them even
    though the pasted URL carries a locale and a tracking parameter."""
    JobPipeline(session).ingest(
        RawJobInput(
            company_name="Acme Analytics",
            title="Data Scientist 1",
            description=DATA_SCIENTIST_JD,
            url=f"https://{HOST}/en-US/AcmeCareers/job/Boston-MA/Data-Scientist-1_R00001?source=LinkedIn",
            connector="user_url",
            source_type="USER_URL",
        )
    )
    session.flush()
    outcome = _service(session, FakeWorkday([_posting(1)])).collect_company(_config())
    session.flush()
    assert outcome.jobs_new == 0
    jobs = session.execute(select(Job)).scalars().all()
    assert len(jobs) == 1
    connectors = {
        a.connector for a in session.execute(select(JobSourceAssociation)).scalars().all()
    }
    assert connectors == {"user_url", "workday"}


def test_the_registry_drives_a_workday_collection(session):
    """registry row → collection target → connector → pipeline → Job."""
    from app.services import monitored_companies
    from app.services.watchlist import WatchlistLoadResult

    outcome = monitored_companies.monitor(
        session,
        company_name="Acme Analytics",
        ats_type="WORKDAY",
        ats_identifier=BOARD,
        careers_url=f"https://{BOARD}",
        registry_source="CURATED_EXPANSION",
    )
    assert outcome.ok
    fake = FakeWorkday([_posting(1)])
    summary = _service(session, fake).collect_watchlist(
        watchlist=WatchlistLoadResult(), commit_between_companies=False
    )
    assert summary.as_dict()["companies"] == 1
    assert len(_jobs(session)) == 1
    company = outcome.company
    assert company.last_successful_collection_at is not None
    assert company.last_jobs_seen == 1
    assert company.last_new_jobs == 1
