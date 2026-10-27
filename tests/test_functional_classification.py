"""Function-first matching, and the Fall lane's retirement.

Two changes with one test file, because they are one decision: the search now
has a single active lane, and relevance inside it is decided by what a posting
describes rather than by what it is called.

The scale of the problem these replace: **9,931 of 10,424 collected jobs
classified as `role_family = UNRELATED`** — 95% — because `ROLE_PATTERNS` is a
list of title regexes whose description fallback searches for the same *title*
patterns. A posting reading "SQL, Python, KPI analysis, experiment design" has
none of those strings in it.

Every fixture below is a shape the retrospective audit actually surfaced.
"""

from __future__ import annotations

import pytest

from app.services.functional_classifier import (
    TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS,
    TITLE_CLEAR_IRRELEVANT,
    TITLE_CLEAR_RELEVANT,
    classify_function,
)
from app.services.search_strategy import (
    ENTRY_COMPATIBLE,
    OVERLEVELLED,
    REACH,
    START_MISMATCH,
    START_UNSTATED,
    active_lanes,
    archived_lanes,
    classify_experience_band,
    classify_start_fit,
    lane_is_active,
    location_tier,
)

# ---------------------------------------------------------------------------
# The strategy reset
# ---------------------------------------------------------------------------


def test_one_active_lane():
    assert active_lanes() == ["FULL_TIME_2027"]


def test_the_fall_lane_is_archived_not_deleted():
    assert "FALL_2026_INTERNSHIP_COOP" in archived_lanes()
    assert not lane_is_active("FALL_2026_INTERNSHIP_COOP")


def test_ambiguous_lane_records_stay_active():
    """9,647 rows are BOTH_OR_AMBIGUOUS.

    That value means the classifier could not tell, not that the job is a Fall
    internship. Treating it as archived would hide most of the search behind a
    classification failure.
    """
    assert lane_is_active("BOTH_OR_AMBIGUOUS")
    assert lane_is_active(None)


def test_lane_activity_is_config_not_code():
    """Retiring or reopening a lane must not need a code change."""
    from app.services.search_strategy import load_strategy

    lanes = load_strategy()["lanes"]
    assert lanes["FULL_TIME_2027"]["status"] == "ACTIVE"
    assert lanes["FALL_2026_INTERNSHIP_COOP"]["status"] == "ARCHIVED"
    assert lanes["FALL_2026_INTERNSHIP_COOP"]["archived_on"] == "2026-08-24"


def test_internships_are_excluded_from_the_active_search():
    from app.services.search_strategy import excluded_employment_types

    excluded = excluded_employment_types()
    assert "INTERNSHIP" in excluded
    assert "CO_OP" in excluded
    assert "FULL_TIME" not in excluded


def test_the_example_title_list_is_marked_non_exhaustive():
    """Load-bearing: any code reading `example_titles` must check this."""
    from app.services.search_strategy import load_strategy

    strategy = load_strategy()
    assert strategy["title_list_is_exhaustive"] is False
    assert strategy["example_titles"]["non_obvious"]


# ---------------------------------------------------------------------------
# Title triage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["Data Scientist", "Machine Learning Engineer", "Analytics Engineer", "Data Analyst"]
)
def test_obvious_titles_are_clear_relevant(title):
    assert classify_function(title, "SQL and Python.").title_triage == TITLE_CLEAR_RELEVANT


@pytest.mark.parametrize(
    "title",
    [
        "Marketplace Strategy Associate",
        "Strategy & Operations Associate",
        "Risk Strategy Analyst",
        "Insights Associate",
        "Revenue Operations Analyst",
    ],
)
def test_unusual_titles_are_ambiguous_not_irrelevant(title):
    """The whole point. These must reach the JD, not be rejected by the title."""
    assert (
        classify_function(title, "").title_triage == TITLE_AMBIGUOUS_REQUIRES_JD_ANALYSIS
    )


@pytest.mark.parametrize(
    "title",
    ["Senior Tax Attorney", "Registered Nurse", "Account Executive", "Help Desk Technician"],
)
def test_genuinely_other_occupations_are_clear_irrelevant(title):
    assert classify_function(title, "").title_triage == TITLE_CLEAR_IRRELEVANT


def test_a_target_word_rescues_an_otherwise_excluded_title():
    """"Sales Analytics Data Scientist" is a data role wearing a sales word."""
    assert (
        classify_function("Sales Analytics Data Scientist", "").title_triage
        == TITLE_CLEAR_RELEVANT
    )


# ---------------------------------------------------------------------------
# The Part 33 fixtures
# ---------------------------------------------------------------------------

STRATEGY_OPS_JD = """You will partner with product and operations teams.
- Experience with SQL to pull and analyse data from our warehouse; strong SQL skills
- Proficiency in Python for analysis and automation
- Define and track KPIs and metrics for the business
- Design and measure experiments; run A/B tests to evaluate changes
- Present insights to stakeholders and inform the roadmap
"""

RISK_JD = """Join the credit risk team.
- Experience with SQL and Python analysing borrower and transaction data
- Build predictive models for default risk; fraud detection models
- Develop risk metrics and monitor scorecard performance
- Strong SQL skills required
"""

OPERATIONS_JD = """Coordinate daily operations for our facilities.
- Schedule staff shifts and manage vendor relationships
- Maintain Excel spreadsheets for weekly reporting
- Track shipments and resolve delivery escalations
- Coordinate meetings and prepare status reports
"""

FPA_JD = """Support the FP&A function.
- Own the monthly budgeting and forecasting process
- Prepare financial statements and variance analysis
- Month-end close and journal entries
- CPA or progress toward CPA preferred
"""

SENIOR_DS_JD = """Principal-level role on our research team.
- 8+ years of experience required building production ML systems
- PhD with a publication record in causal inference
- Lead a team of scientists and set research direction
- Deep expertise in Python, machine learning and experimentation
"""

CUSTOMER_SUCCESS_JD = """Own a book of accounts.
- Respond to support tickets and resolve customer issues
- Manage customer onboarding and account health
- Run light reporting in a dashboard for your accounts
"""

MARKETPLACE_JD = """Help our two-sided marketplace run efficiently.
- Experience with SQL for supply and demand analysis; SQL is used daily
- Run experiments on matching and pricing; experimentation is core
- Build dashboards to track marketplace metrics
"""


def test_a_strategy_title_over_analytics_work_is_a_hidden_fit():
    profile = classify_function("Strategy & Operations Associate", STRATEGY_OPS_JD)
    assert profile.is_functional_match
    assert profile.primary_function in ("DATA_ANALYTICS", "PRODUCT_ANALYTICS", "DECISION_ANALYTICS")
    assert profile.hidden_fit


def test_a_risk_title_over_modelling_work_is_a_hidden_fit():
    profile = classify_function("Risk Strategy Analyst", RISK_JD)
    assert profile.is_functional_match
    assert profile.primary_function in ("DECISION_ANALYTICS", "DATA_SCIENCE")
    assert profile.hidden_fit
    assert "RISK" in profile.domains


def test_operations_coordination_is_not_a_data_role():
    """The word "operations" is not a disqualifier; nor is it a qualification."""
    profile = classify_function("Operations Analyst", OPERATIONS_JD)
    assert not profile.is_functional_match
    assert not profile.hidden_fit


def test_fpa_accounting_work_is_not_data_analytics():
    profile = classify_function("Financial Analyst", FPA_JD)
    assert not profile.is_functional_match


def test_a_senior_data_scientist_is_relevant_but_overlevelled():
    """Title relevant, function relevant, experience out of band.

    Three separate facts, and the system must not collapse them: this is not a
    classification failure, it is a seniority mismatch.
    """
    profile = classify_function("Data Scientist", SENIOR_DS_JD)
    assert profile.title_triage == TITLE_CLEAR_RELEVANT
    assert profile.is_functional_match
    assert classify_experience_band("Data Scientist", SENIOR_DS_JD).band == OVERLEVELLED


def test_customer_success_with_light_reporting_is_excluded():
    profile = classify_function("Customer Success Analyst", CUSTOMER_SUCCESS_JD)
    assert not profile.is_functional_match


def test_a_marketplace_associate_over_sql_and_experiments_is_a_hidden_fit():
    profile = classify_function("Marketplace Associate", MARKETPLACE_JD)
    assert profile.is_functional_match
    assert profile.hidden_fit
    assert "MARKETPLACE" in profile.domains


# ---------------------------------------------------------------------------
# What the audit taught the classifier
# ---------------------------------------------------------------------------


def test_culture_boilerplate_does_not_create_a_capability():
    """Every SharkNinja posting says "expected to experiment boldly".

    One `search()` for "experiment" therefore gave EXPERIMENTATION to mechanical
    engineers, supply planners and product integrity engineers, and the audit
    recovered a Mechanical Engineer I as DATA_ANALYTICS.
    """
    jd = (
        "Our people are expected to experiment boldly, adopt new tools, and raise "
        "what is possible. RESPONSIBILITIES: Design CAD layouts and 3D models. "
        "Run engineering studies. Statistical process control."
    )
    profile = classify_function("Mechanical Engineer I", jd)
    assert "EXPERIMENTATION" not in profile.primary_capabilities
    assert not profile.is_functional_match


def test_the_letter_r_in_r_and_d_is_not_the_r_language():
    """`\\br\\b` matched the "R" in "R&D team" and handed out PROGRAMMING."""
    jd = (
        "Facilitate communication of technical requirements between suppliers and "
        "our R&D team. Own cost modeling and cost estimation. R&D partnership is key."
    )
    profile = classify_function("Supplier Industrialization Engineer", jd)
    assert "PROGRAMMING" not in profile.primary_capabilities


def test_backend_engineering_is_software_not_data_engineering():
    """A trailing `\\b` after `backend (?:service|system)` cannot match
    "backend services", so SOFTWARE_BUILD never fired and Robinhood's backend
    posting classified as DATA_ENGINEERING."""
    jd = (
        "Design, build, test and deploy scalable backend services and systems. "
        "Develop product-facing features. Experience with backend systems, REST "
        "APIs and distributed systems. Some SQL and Python. Code review and "
        "on-call rotation."
    )
    profile = classify_function("Software Engineer, Backend", jd)

    # The D-47 guarantee, unchanged: this is software, not data engineering.
    assert profile.classified_function == "SOFTWARE_ENGINEERING"
    assert profile.primary_function != "DATA_ENGINEERING"

    # Corrected 2026-09-07 (D-49). This line used to read "software engineering
    # is not an active target". Classifying the posting as software was right;
    # concluding it was therefore irrelevant was not — the candidate's OpenText
    # internship is a software engineering job. It is a SECONDARY_TARGET.
    assert profile.is_functional_match
    assert profile.target_priority == "SECONDARY_TARGET"


def test_a_fintech_data_scientist_is_not_vetoed_by_its_domain():
    """A data scientist at a payments company talks about finance and sales all
    day. That is the domain, not the occupation — and the veto dropped a fintech's
    own Data Scientist posting."""
    jd = (
        "Experience with SQL and Python required; strong SQL skills. Build "
        "predictive models for spend and revenue. Machine learning models for "
        "underwriting. Partner with finance and sales teams on GAAP reporting."
    )
    profile = classify_function("Data Scientist", jd)
    assert profile.is_functional_match


def test_one_core_capability_is_not_a_data_job():
    """At a threshold of one, a single "SQL" in a backend posting was enough."""
    profile = classify_function(
        "Program Manager", "Experience with SQL is a plus. Coordinate the roadmap."
    )
    assert not profile.is_functional_match


# ---------------------------------------------------------------------------
# Experience bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "jd", "expected"),
    [
        ("Data Analyst", "New grad role. 0-2 years.", ENTRY_COMPATIBLE),
        ("Data Analyst", "Entry level position, no prior experience required.", ENTRY_COMPATIBLE),
        ("Analytics Engineer", "2+ years of experience required.", REACH),
        ("Product Analyst", "2+ years of experience preferred.", "EARLY_CAREER"),
        ("Decision Scientist", "5+ years of experience required.", OVERLEVELLED),
        ("Data Scientist", "8+ years of experience required.", OVERLEVELLED),
        ("Data Analyst", "Join our team.", "EXPERIENCE_UNSTATED"),
    ],
)
def test_experience_bands(title, jd, expected):
    assert classify_experience_band(title, jd).band == expected


def test_a_senior_title_beats_a_new_grad_phrase_in_the_body():
    assert classify_experience_band("Senior Data Scientist", "New grad friendly.").band == OVERLEVELLED


def test_sr_with_a_period_is_recognised_as_senior():
    """A trailing `\\b` after `sr\\.` can never match — a period followed by a
    space is two non-word characters. Every "Sr." title in the corpus banded as
    ENTRY_COMPATIBLE until the audit caught it."""
    assert classify_experience_band("Sr. Business Intelligence Engineer", "SQL.").band == OVERLEVELLED


def test_a_manager_title_is_overlevelled_unless_it_says_new_grad():
    assert classify_experience_band("Engineering Manager", "SQL and Python.").band == OVERLEVELLED
    assert (
        classify_experience_band(
            "Associate Product Manager, New Grad", "A new grad rotational program."
        ).band
        == ENTRY_COMPATIBLE
    )


def test_reach_is_an_active_band():
    """Hard-rejecting every "2+ years" posting would delete most of the market."""
    assert classify_experience_band("Analytics Engineer", "2+ years required.").active


# ---------------------------------------------------------------------------
# Start dates
# ---------------------------------------------------------------------------


def test_an_unstated_start_date_is_not_a_mismatch():
    """Most postings give none. Treating silence as a conflict rejects the market."""
    fit = classify_start_fit("Join our growing analytics team.")
    assert fit.status == START_UNSTATED
    assert fit.compatible


def test_a_january_2027_start_is_compatible():
    assert classify_start_fit("Start January 2027 in our NYC office.").compatible


def test_a_2027_graduate_programme_is_compatible():
    assert classify_start_fit("2027 University Graduates programme.").compatible


@pytest.mark.parametrize(
    "jd", ["Fall 2026 start required.", "Immediate start required for this role."]
)
def test_a_start_before_completion_is_flagged(jd):
    fit = classify_start_fit(jd)
    assert fit.status == START_MISMATCH
    assert not fit.compatible


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "tier"),
    [
        ("Boston, MA", 1),
        ("Cambridge, MA", 1),
        ("New York, NY", 2),
        ("San Francisco, CA", 3),
        ("Remote - US", 4),
        ("Austin, TX", 5),
    ],
)
def test_location_priority_orders_without_excluding(location, tier):
    """A strong early-career role in Austin stays visible, ranked lower."""
    assert location_tier(location).tier == tier


# ---------------------------------------------------------------------------
# Hidden fit is not reach
# ---------------------------------------------------------------------------


def test_hidden_fit_and_reach_are_independent():
    """Different axes: one is about the title, the other about seniority."""
    hidden_and_reachable = classify_function(
        "Strategy & Operations Associate", STRATEGY_OPS_JD
    )
    assert hidden_and_reachable.hidden_fit
    assert classify_experience_band("Strategy & Operations Associate", STRATEGY_OPS_JD).active

    obvious_title = classify_function("Data Scientist", SENIOR_DS_JD)
    assert not obvious_title.hidden_fit, "an obvious title is never a hidden fit"
    assert classify_experience_band("Data Scientist", SENIOR_DS_JD).band == OVERLEVELLED


def test_an_obvious_title_is_never_a_hidden_fit():
    profile = classify_function("Data Scientist", STRATEGY_OPS_JD)
    assert profile.is_functional_match
    assert not profile.hidden_fit


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def test_the_classifier_stamps_its_version():
    from app.services.functional_classifier import FUNCTIONAL_CLASSIFIER_VERSION

    profile = classify_function("Data Analyst", STRATEGY_OPS_JD)
    assert profile.classifier_version == FUNCTIONAL_CLASSIFIER_VERSION
    assert profile.as_dict()["classifier_version"] == FUNCTIONAL_CLASSIFIER_VERSION


def test_the_prescreen_version_moved_with_the_logic():
    from app.services.prescreen import PRESCREEN_VERSION

    assert PRESCREEN_VERSION == "0.3.0"
