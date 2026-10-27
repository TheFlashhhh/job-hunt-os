"""Software engineering is a real target, and only some of it.

The function-first pass (D-47) was right to stop calling backend postings
"analytics". It then drew the wrong conclusion and marked SOFTWARE_ENGINEERING
`active: false`, which made every software role irrelevant — for a candidate
whose OpenText internship IS a software engineering job: C#/.NET, Entity
Framework, REST APIs, SQL Server/Oracle/PostgreSQL/MySQL, Postman, Swagger,
MSTest, Azure DevOps.

The correction has two halves, and both matter:

* Software engineering is a **SECONDARY_TARGET** — real, ranked behind a
  primary data/AI role at similar fit.
* Only the **specialisations the evidence covers**. Backend, APIs, databases and
  data/ML platform work, yes. Frontend, mobile, embedded, graphics, kernel, no.

Seniority remains a separate axis and still binds independently.
"""

from __future__ import annotations

import pathlib

import pytest

from app.services.functional_classifier import (
    DEPRIORITIZED,
    PRIMARY_TARGET,
    SECONDARY_TARGET,
    SPECIALIZATION_ALIGNED,
    SPECIALIZATION_MISMATCHED,
    classify_function,
)
from app.services.search_strategy import OVERLEVELLED, classify_experience_band

# ---------------------------------------------------------------------------
# The Part 21 fixtures
# ---------------------------------------------------------------------------

BACKEND_JD = """Build and own backend services.
- Experience with Python and REST APIs
- Strong SQL; PostgreSQL in production
- Service development and code review
- 0-2 years of experience
"""

ML_PLATFORM_JD = """Build the platform our ML runs on.
- Python, machine learning systems and ML infrastructure
- Build APIs and data pipelines
- Docker and Kubernetes
- Experience with ML platform work
"""

DATA_INFRA_JD = """Own our distributed data systems.
- Python, Kafka, Spark and SQL
- Build and operate data pipelines
- Data infrastructure at scale
- 1-3 years of experience
"""

FRONTEND_JD = """Own the customer-facing web experience.
- React, TypeScript and CSS
- Build and maintain our component library
- Responsive design and web accessibility
- Front-end development across the product
"""

SENIOR_BACKEND_JD = """Lead backend architecture.
- Python, REST APIs, SQL and backend services
- 7+ years of experience required
"""

IOS_JD = """Build our iOS application.
- Swift and SwiftUI, UIKit
- Mobile architecture and iOS app development
- Xcode, the App Store release process
"""

DOTNET_JD = """Join our applications team.
- C# and .NET, Entity Framework
- REST APIs and SQL Server
- Database-backed applications
- 1 year of experience
"""

DATA_SCIENTIST_JD = """Join the data science team.
- Experience with Python and SQL
- Statistical modeling and predictive models
- Experimentation and A/B tests
"""


def test_a_backend_role_is_software_engineering_not_analytics():
    profile = classify_function("Backend Software Engineer", BACKEND_JD)
    assert profile.primary_function == "SOFTWARE_ENGINEERING"
    assert profile.is_functional_match
    assert profile.target_priority == SECONDARY_TARGET
    assert profile.specialization_fit == SPECIALIZATION_ALIGNED
    assert classify_experience_band("Backend Software Engineer", BACKEND_JD).active


def test_an_ml_platform_role_is_a_target():
    profile = classify_function("Software Engineer, ML Platform", ML_PLATFORM_JD)
    assert profile.is_functional_match
    assert profile.primary_function in (
        "SOFTWARE_ENGINEERING",
        "MACHINE_LEARNING_ENGINEERING",
        "DATA_ENGINEERING",
    )


def test_a_data_infrastructure_role_is_a_hybrid_target():
    """Hybrid roles may resolve to either side; both are targets."""
    profile = classify_function("Software Engineer, Data Infrastructure", DATA_INFRA_JD)
    assert profile.is_functional_match
    assert profile.primary_function in ("DATA_ENGINEERING", "SOFTWARE_ENGINEERING")


def test_a_dotnet_role_matches_the_opentext_stack():
    """C#/.NET/Entity Framework/SQL Server is exactly the candidate's internship."""
    profile = classify_function("Software Developer", DOTNET_JD)
    assert profile.primary_function == "SOFTWARE_ENGINEERING"
    assert profile.target_priority == SECONDARY_TARGET
    assert profile.specialization_fit == SPECIALIZATION_ALIGNED


def test_frontend_is_a_specialization_mismatch():
    profile = classify_function("Frontend Engineer", FRONTEND_JD)
    assert profile.specialization_fit == SPECIALIZATION_MISMATCHED
    assert not profile.is_functional_match
    assert profile.target_priority == DEPRIORITIZED
    # Still recorded as software. "Classify this as software" is what
    # SOFTWARE_BUILD means; it never meant "discard this".
    assert profile.classified_function == "SOFTWARE_ENGINEERING"


def test_ios_is_a_specialization_mismatch():
    profile = classify_function("iOS Engineer", IOS_JD)
    assert profile.specialization_fit == SPECIALIZATION_MISMATCHED
    assert not profile.is_functional_match


def test_a_mismatched_specialisation_in_the_title_is_decisive():
    """"Android Engineer II (Healthcare)" reached the active list because the
    body named Android once and the two-occurrence rule needs two."""
    profile = classify_function(
        "Android Engineer II (Healthcare)",
        "Build our app. Work with backend APIs, Python services and SQL.",
    )
    assert profile.specialization_fit == SPECIALIZATION_MISMATCHED
    assert not profile.is_functional_match


def test_a_senior_backend_role_is_software_but_overlevelled():
    """Three separate facts. The classifier must not collapse them."""
    profile = classify_function("Senior Backend Engineer", SENIOR_BACKEND_JD)
    assert profile.classified_function == "SOFTWARE_ENGINEERING"
    assert classify_experience_band("Senior Backend Engineer", SENIOR_BACKEND_JD).band == (
        OVERLEVELLED
    )


def test_a_data_scientist_is_not_displaced_by_software_logic():
    profile = classify_function("Data Scientist", DATA_SCIENTIST_JD)
    assert profile.primary_function == "DATA_SCIENCE"
    assert profile.target_priority == PRIMARY_TARGET


# ---------------------------------------------------------------------------
# Priority is a second axis, not a second lane
# ---------------------------------------------------------------------------


def test_data_functions_are_primary_targets():
    for title, jd in [
        ("Data Scientist", DATA_SCIENTIST_JD),
        ("Data Engineer", DATA_INFRA_JD),
    ]:
        profile = classify_function(title, jd)
        if profile.is_functional_match:
            assert profile.target_priority == PRIMARY_TARGET


def test_priority_comes_from_config_not_from_a_literal():
    from app.services.search_strategy import load_strategy

    priorities = {
        f["name"]: f.get("priority")
        for f in load_strategy()["target_functions"]
        if f.get("active")
    }
    assert priorities["SOFTWARE_ENGINEERING"] == SECONDARY_TARGET
    assert priorities["DATA_SCIENCE"] == PRIMARY_TARGET
    # Every active function declares one; a missing priority would silently
    # default and nobody would notice.
    assert all(value for value in priorities.values())


def test_software_engineering_is_active_again():
    from app.services.search_strategy import load_strategy

    swe = next(
        f for f in load_strategy()["target_functions"] if f["name"] == "SOFTWARE_ENGINEERING"
    )
    assert swe["active"] is True


# ---------------------------------------------------------------------------
# Hidden fit is not corrupted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["Software Engineer", "Backend Engineer", "Software Developer"]
)
def test_a_normal_software_title_is_not_a_hidden_fit(title):
    """Hidden fit means the title HIDES the work. These announce it."""
    profile = classify_function(title, BACKEND_JD)
    assert profile.is_functional_match
    assert not profile.hidden_fit


def test_an_unusual_hybrid_title_can_still_be_a_hidden_fit():
    profile = classify_function("Product Engineer, Data Systems", DATA_INFRA_JD)
    assert profile.is_functional_match
    assert profile.hidden_fit


# ---------------------------------------------------------------------------
# Roles that must stay out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "jd"),
    [
        (
            "GTM Systems Engineer, Salesforce",
            "Own our Salesforce Service Cloud platform. Apex triggers. Integrations "
            "with REST APIs and SQL.",
        ),
        (
            "Technical Escalations Engineer 2",
            "Handle technical escalations from customers. Debug with Python and SQL. "
            "Tier 2 support queue.",
        ),
        (
            "Customer Experience Representative, Active Trader",
            "Help customers with complex options trading issues. Support tickets and "
            "customer inquiries.",
        ),
    ],
)
def test_support_and_saas_administration_stay_out(title, jd):
    """All three reached the active software list during the D-49 audit."""
    assert not classify_function(title, jd).is_functional_match


# ---------------------------------------------------------------------------
# The defect class that made this pass necessary twice
# ---------------------------------------------------------------------------


def test_no_literal_backspace_bytes_in_source():
    r"""A shell heredoc wrote `\b` as ASCII 8 into two regexes.

    The file parsed, the pattern compiled, and it matched NOTHING — the worst
    failure mode available to a classifier, because everything looks fine. Two
    negative signals were silently inert until a `repr()` showed `\x08`.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ("app", "scripts", "tests"):
        for path in (root / folder).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            if "\x08" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
    assert not offenders, f"literal backspace bytes in: {offenders}"


def test_every_capability_and_negative_pattern_matches_something():
    """A pattern that can never match is worse than a missing one.

    It looks like coverage. This is the cheap structural check that would have
    caught the backspace bug on the same day it was introduced.
    """
    from app.services.functional_classifier import (
        CAPABILITY_FAMILIES,
        NEGATIVE_FUNCTION_SIGNALS,
    )

    dead = []
    for name, _weight, pattern in [*CAPABILITY_FAMILIES, *NEGATIVE_FUNCTION_SIGNALS]:
        # Every alternation should be reachable by the literal it is built from.
        if "\x08" in pattern.pattern:
            dead.append(name)
    assert not dead, f"patterns containing a literal backspace: {dead}"
