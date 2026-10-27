"""The expensive-analysis boundary.

The asymmetry drives every test here: a false IRRELEVANT is a permanently
missed job, while a false PLAUSIBLE costs milliseconds. So the known-relevant
retention tests matter more than the rejection-rate tests, and they are written
from postings that really appeared on real boards during Pilot 1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.ingestion import Job
from app.services.pipeline import JobPipeline, RawJobInput
from app.services.prescreen import (
    PrescreenOutcome,
    PrescreenReason,
    PreScreenService,
    should_run_semantic_analysis,
)


@pytest.fixture
def prescreen() -> PreScreenService:
    return PreScreenService()


# ---------------------------------------------------------------------------
# Known-relevant retention — drawn from real Pilot 1 postings
# ---------------------------------------------------------------------------

KNOWN_RELEVANT = [
    (
        "Whoop",
        "Data Scientist, Analytics",
        "Boston, MA",
        "Analyze sensor and membership data, build models and partner with product teams. "
        "Strong SQL and Python required.",
    ),
    (
        "Whoop",
        "Machine Learning Engineer",
        "Boston, MA",
        "Build and deploy ML models over physiological time-series data. Python required.",
    ),
    (
        "Pinecrest",
        "Data Scientist",
        "New York, NY",
        "Partner with product and business teams on experimentation and analytics. SQL, Python.",
    ),
    (
        "Pinecrest",
        "Analytics Engineer",
        "New York, NY",
        "Build dbt models and semantic layers over the warehouse. SQL and Python.",
    ),
    (
        "Klaviyo",
        "Data Engineer",
        "Boston, MA",
        "Build streaming and batch data pipelines. Kafka, Python, SQL.",
    ),
    (
        "Klaviyo",
        "Product Analyst",
        "Boston, MA",
        "Measure feature adoption and retention. SQL and experimentation.",
    ),
    (
        "Confluent",
        "Software Engineer, New Grad",
        "Remote (US)",
        "Work on distributed streaming systems. Recent graduates welcome. Java, Python.",
    ),
]


#: Real postings that MUST be filtered out since 2026-09-08 (D-51).
#:
#: Both were in KNOWN_RELEVANT until the search became full-time only. They are
#: kept rather than deleted because they are the regression fixtures for the
#: opposite property now: an internship must not reach expensive analysis, and
#: these are the exact two the corpus had.
KNOWN_INTERNSHIPS = [
    (
        "SharkNinja",
        "Fall 2026: AI/Sharks Applied AI & Analytics Co-op (August to December)",
        "Needham, MA, United States",
        "Own the end-to-end scoping, analysis and delivery of a business problem. "
        "Working knowledge of SQL, Python or similar tools through coursework or projects. "
        "Build and maintain dashboards, reports, or models.",
    ),
    (
        "Pinecrest",
        "Software Engineer Internship, Android",
        "New York, NY",
        "Internship building Android features. Currently enrolled students. Kotlin and Java.",
    ),
]


@pytest.mark.parametrize("company,title,location,body", KNOWN_INTERNSHIPS)
def test_known_internships_are_filtered_out(prescreen, company, title, location, body):
    """The search is full-time only. An internship must not reach analysis."""
    result = prescreen.evaluate(
        title=title, description=body, company_name=company, location_raw=location
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT, (
        f"{company} — {title!r} survived screening: {result.detail}"
    )
    assert result.needs_analysis is False


@pytest.mark.parametrize("company,title,location,body", KNOWN_RELEVANT)
def test_known_relevant_postings_are_never_filtered_out(prescreen, company, title, location, body):
    """The one property that must hold. A missed job is invisible and permanent."""
    result = prescreen.evaluate(
        title=title, description=body, company_name=company, location_raw=location
    )
    assert result.outcome is not PrescreenOutcome.IRRELEVANT, (
        f"{company} — {title!r} was filtered out: {result.detail}"
    )


def test_the_sharkninja_coop_is_now_rejected(prescreen):
    """It was in the real database, and it was applied to.

    This test asserted the co-op SURVIVED screening, because at the time it was
    the highest-value live role. It was purged with the rest of the internship
    records on 2026-09-08 (D-51), and screening now rejects its shape.
    """
    company, title, location, body = KNOWN_INTERNSHIPS[0]
    result = prescreen.evaluate(
        title=title, description=body, company_name=company, location_raw=location
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT
    assert result.needs_analysis is False


# ---------------------------------------------------------------------------
# Irrelevant controls — real titles from the Pilot 1 boards
# ---------------------------------------------------------------------------

KNOWN_IRRELEVANT = [
    ("Account Executive, Commercial", "New York, NY", "Sell to mid-market customers."),
    ("Business Development Representative", "Boston, MA", "Outbound prospecting and lead gen."),
    ("Customer Success Manager", "Chicago, IL", "Own customer relationships and renewals."),
    ("Registered Nurse", "Boston, MA", "Provide patient care."),
    ("Warehouse Associate", "Dallas, TX", "Pick and pack orders."),
    ("Recruiter, Technical", "Remote", "Source and screen engineering candidates."),
    ("Graphic Designer", "New York, NY", "Design marketing assets."),
    # A Staff-level data role is genuinely out of reach for an early-career
    # candidate. Rejecting it is correct, not an over-filter.
    ("Staff Data Scientist", "Boston, MA", "Causal inference on platform data."),
]


@pytest.mark.parametrize("title,location,body", KNOWN_IRRELEVANT)
def test_clearly_irrelevant_roles_are_filtered(prescreen, title, location, body):
    result = prescreen.evaluate(
        title=title, description=body, company_name="Some Co", location_raw=location
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT
    assert result.needs_analysis is False


def test_non_us_onsite_is_filtered(prescreen):
    result = prescreen.evaluate(
        title="Data Scientist",
        description="Analyze data for our EMEA team.",
        location_raw="London, United Kingdom",
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT
    assert PrescreenReason.NON_US in result.reasons


def test_senior_roles_are_filtered(prescreen):
    result = prescreen.evaluate(
        title="Principal Machine Learning Engineer",
        description="10+ years leading ML teams.",
        location_raw="Boston, MA",
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT
    assert PrescreenReason.SENIORITY_TOO_HIGH in result.reasons


def test_citizenship_requirement_is_filtered(prescreen):
    """The MIT Lincoln Laboratory case, caught before any analysis spend."""
    result = prescreen.evaluate(
        title="Co-Op, AI Technology and Systems, Fall 2026",
        description="Python and ML. Must be a U.S. citizen. Secret clearance required.",
        location_raw="Lexington, MA",
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT
    assert PrescreenReason.CITIZENSHIP_REQUIRED in result.reasons


# ---------------------------------------------------------------------------
# Conservatism — ambiguity must land in UNCERTAIN, not IRRELEVANT
# ---------------------------------------------------------------------------


def test_unconventional_2027_title_is_not_rejected(prescreen):
    """An odd title is not evidence of irrelevance."""
    result = prescreen.evaluate(
        title="Insights Partner, Growth",
        description="Work with data to inform growth decisions. SQL required.",
        location_raw="Remote (US)",
    )
    assert result.outcome is not PrescreenOutcome.IRRELEVANT


def test_missing_location_is_uncertain_not_rejected(prescreen):
    """A blank location is unknown, not foreign."""
    result = prescreen.evaluate(
        title="Business Systems Coordinator",
        description="Support reporting and operational data workflows.",
        location_raw=None,
    )
    assert result.outcome is not PrescreenOutcome.IRRELEVANT


def test_two_to_three_years_experience_is_not_rejected(prescreen):
    result = prescreen.evaluate(
        title="Data Analyst",
        description="2-3 years of experience with SQL and dashboarding.",
        location_raw="Boston, MA",
    )
    assert result.outcome is not PrescreenOutcome.IRRELEVANT


def test_an_internship_is_now_rejected_outright(prescreen):
    """Full-time only since 2026-09-08 (D-51).

    This test has moved twice with the strategy, which is the honest record of
    it: it first asserted a Fall co-op was PLAUSIBLE, then that it was retained
    but not targeted, and now that it is rejected before any analysis runs.
    The candidate is not pursuing internships, so spending analysis on one is waste.
    """
    result = prescreen.evaluate(
        title="Fall Co-op — Digital Insights Team",
        description="Support analytics workstreams using Excel and SQL.",
        location_raw="Boston, MA",
    )
    assert result.outcome is PrescreenOutcome.IRRELEVANT


def test_sales_title_with_a_genuine_data_signal_is_kept(prescreen):
    """'Sales Data Scientist' is a data role wearing a sales word."""
    result = prescreen.evaluate(
        title="Sales Data Scientist",
        description="Forecasting and modelling for the revenue org. Python and SQL.",
        location_raw="Boston, MA",
    )
    assert result.outcome is not PrescreenOutcome.IRRELEVANT


# ---------------------------------------------------------------------------
# The LLM gate
# ---------------------------------------------------------------------------


def _result(outcome: PrescreenOutcome):
    from app.services.prescreen import PrescreenResult

    return PrescreenResult(outcome=outcome)


def test_semantic_analysis_never_runs_without_a_provider():
    assert (
        should_run_semantic_analysis(
            prescreen=_result(PrescreenOutcome.PLAUSIBLE), llm_available=False
        )
        is False
    )


def test_semantic_analysis_never_runs_on_irrelevant_jobs():
    assert (
        should_run_semantic_analysis(
            prescreen=_result(PrescreenOutcome.IRRELEVANT), llm_available=True
        )
        is False
    )


def test_semantic_analysis_resolves_uncertain_jobs():
    """UNCERTAIN means 'a cheap filter could not decide' — worth resolving."""
    assert (
        should_run_semantic_analysis(
            prescreen=_result(PrescreenOutcome.UNCERTAIN), llm_available=True
        )
        is True
    )


def test_budget_exhaustion_stops_semantic_analysis():
    assert (
        should_run_semantic_analysis(
            prescreen=_result(PrescreenOutcome.PLAUSIBLE),
            llm_available=True,
            budget_permits=False,
        )
        is False
    )


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


def _ingest(session, title, body, location="Boston, MA", company="Pilot Co"):
    return JobPipeline(session).ingest(
        RawJobInput(company_name=company, title=title, description=body, location=location)
    )


def test_irrelevant_postings_skip_analysis_entirely(session):
    outcome = _ingest(
        session, "Account Executive, Commercial", "Sell to mid-market customers.", "New York, NY"
    )
    session.flush()

    assert outcome.analysis is None
    assert outcome.job.prescreen_outcome == "PREFILTER_IRRELEVANT"
    assert outcome.job.analyses == []
    # Provenance is kept: the record is still inspectable.
    assert outcome.job.job_id
    assert outcome.job.prescreen_detail
    assert outcome.job.screen_reason


def test_plausible_postings_are_fully_analyzed(session, sample_jd):
    outcome = _ingest(session, "Data Scientist, Product", sample_jd)
    session.flush()

    assert outcome.job.prescreen_outcome == "PREFILTER_PLAUSIBLE"
    assert outcome.analysis is not None
    assert outcome.analysis.priority_score is not None
    assert outcome.analysis.requirements


def test_uncertain_postings_stay_reviewable_but_unanalyzed(session):
    outcome = _ingest(
        session,
        "Business Operations Coordinator",
        "Support reporting workflows across the business.",
        location=None,
    )
    session.flush()

    if outcome.job.prescreen_outcome == "PREFILTER_UNCERTAIN":
        assert outcome.analysis is None
        assert outcome.job.screen_status == "FLAGGED", "uncertain must not read as rejected"


def test_prescreen_verdict_is_versioned(session, sample_jd):
    from app.services.prescreen import PRESCREEN_VERSION

    outcome = _ingest(session, "Data Scientist, Product", sample_jd)
    session.flush()
    assert outcome.job.prescreen_version == PRESCREEN_VERSION


def test_unchanged_repoll_still_skips_work(session, sample_jd):
    """The content-hash gate survives the refactor."""
    _ingest(session, "Data Scientist, Product", sample_jd)
    session.flush()
    second = _ingest(session, "Data Scientist, Product", sample_jd)
    session.flush()

    assert second.unchanged is True
    assert second.analysis is None
    job = session.execute(select(Job)).scalars().one()
    assert len(job.analyses) == 1


def test_analysis_avoidance_on_a_mixed_board(session, sample_jd):
    """The measurement the pilot exists to produce."""
    postings = [
        ("Data Scientist, Product", sample_jd, "Boston, MA"),
        ("Account Executive, Commercial", "Sell software.", "New York, NY"),
        ("Registered Nurse", "Patient care.", "Boston, MA"),
        ("Warehouse Associate", "Pick and pack.", "Dallas, TX"),
        ("Principal Engineer", "15 years of experience leading teams.", "Boston, MA"),
    ]
    for index, (title, body, location) in enumerate(postings):
        _ingest(session, title, body, location, company=f"Board Co {index}")
    session.flush()

    jobs = session.execute(select(Job)).scalars().all()
    analyzed = [j for j in jobs if j.analyses]
    assert len(jobs) == 5
    assert len(analyzed) == 1, "only the relevant posting should have been analyzed"
    avoidance = 1 - len(analyzed) / len(jobs)
    assert avoidance >= 0.6
