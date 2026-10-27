"""Resume recommendations and the skill-gap baseline.

The recommendation layer is where a tailoring tool is most tempted to lie. The
tests below are mostly about what it must refuse to say.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.enums import ApplicationStatus, RecommendationKind, RoleFamily
from app.models.misc import ResumeFamily, ResumeRecommendation
from app.services.crm import ApplicationService
from app.services.gaps import SkillGapService
from app.services.pipeline import JobPipeline, RawJobInput
from app.services.resume import (
    RESUME_BASES,
    ResumeRecommendationService,
    base_for_role_family,
    seed_resume_bases,
)

RAG_HEAVY_JD = """Data Scientist, Boston MA. Full-time, new grad 2027.

Requirements
- Strong proficiency in Python and SQL
- Experience building data pipelines
- Build production RAG pipelines over customer knowledge bases
- Deep experience with LangChain and vector databases
- Build executive dashboards in Tableau
"""

CLEAN_FIT_JD = """Data Engineer, New Grad 2027. Remote within the United States.

Requirements
- Proficiency in Python and SQL
- Experience with Kafka or Spark
- Experience building and maintaining data pipelines
"""


def _job(session, jd, *, company="Vantage Labs", title="Data Scientist, New Grad 2027"):
    return (
        JobPipeline(session)
        .ingest(
            RawJobInput(company_name=company, title=title, description=jd, location="Boston, MA")
        )
        .job
    )


# ---------------------------------------------------------------------------
# Role-family bases
# ---------------------------------------------------------------------------


def test_seeding_creates_the_five_bases(session):
    created = seed_resume_bases(session)
    assert created == 5
    names = {f.name for f in session.execute(select(ResumeFamily)).scalars()}
    assert names == {
        "DS_ML_AI",
        "ANALYTICS_PRODUCT",
        "DATA_ENGINEERING",
        "APPLIED_AI_SOFTWARE",
        "OPTIONAL_SOFTWARE",
    }


def test_seeding_is_idempotent(session):
    seed_resume_bases(session)
    assert seed_resume_bases(session) == 0
    assert len(session.execute(select(ResumeFamily)).scalars().all()) == 5


def test_bases_carry_no_invented_resume_content(session):
    """A base is a routing rule, not prose about the candidate."""
    seed_resume_bases(session)
    for family in session.execute(select(ResumeFamily)).scalars():
        assert family.target_role_families
        # Descriptions describe the base's purpose; they never assert experience.
        assert " I " not in f" {family.description} "
        assert "years of experience" not in (family.description or "")


@pytest.mark.parametrize(
    "role_family,expected",
    [
        (RoleFamily.DATA_SCIENTIST.value, "DS_ML_AI"),
        (RoleFamily.ML_ENGINEER.value, "DS_ML_AI"),
        (RoleFamily.PRODUCT_ANALYTICS.value, "ANALYTICS_PRODUCT"),
        (RoleFamily.DATA_ENGINEERING.value, "DATA_ENGINEERING"),
        (RoleFamily.SOFTWARE_ENGINEERING.value, "OPTIONAL_SOFTWARE"),
    ],
)
def test_role_family_routes_to_a_base(role_family, expected):
    assert base_for_role_family(role_family) == expected


def test_every_base_declares_target_role_families():
    for base in RESUME_BASES:
        assert base["target_role_families"]


# ---------------------------------------------------------------------------
# Recommendations — the safety surface
# ---------------------------------------------------------------------------


def test_unsupported_jd_keywords_become_do_not_add(session):
    """The core rule: a JD asking for RAG must not produce RAG resume text."""
    job = _job(session, RAG_HEAVY_JD)
    result = ResumeRecommendationService(session).build(job)

    do_not_add = result.of_kind(RecommendationKind.DO_NOT_ADD)
    assert do_not_add, "RAG-heavy JD produced no DO_NOT_ADD guidance"

    bodies = " ".join(r.body for r in do_not_add)
    assert "RAG" in bodies or "Tableau" in bodies
    assert all(r.body.lower().startswith("do not") for r in do_not_add)


def test_no_recommendation_ever_suggests_writing_an_unsupported_keyword(session):
    job = _job(session, RAG_HEAVY_JD)
    result = ResumeRecommendationService(session).build(job)

    writable = result.of_kind(RecommendationKind.EMPHASIZE) + result.of_kind(
        RecommendationKind.SAFE_REWORD
    )
    for rec in writable:
        lowered = rec.body.lower()
        assert "rag" not in lowered.split()
        assert "langchain" not in lowered
        # Tableau is Bank-cleared as of 2026-09-11 (EV-CH-11) and so may be
        # recommended; Looker and Sigma are not and may not.
        assert "looker" not in lowered
        assert "sigma" not in lowered


def test_every_writable_recommendation_carries_evidence_or_is_a_skills_line(session):
    job = _job(session, CLEAN_FIT_JD, title="Data Engineer, New Grad 2027")
    result = ResumeRecommendationService(session).build(job)

    for rec in result.of_kind(RecommendationKind.EMPHASIZE):
        traceable = bool(rec.evidence_ids) or bool(rec.safe_claim_ids)
        assert traceable or rec.body.startswith("Lead the skills line"), rec.body


def test_every_writable_recommendation_passes_the_validator(session):
    from app.evidence.claim_validator import get_claim_validator

    validator = get_claim_validator()
    job = _job(session, CLEAN_FIT_JD)
    result = ResumeRecommendationService(session).build(job)

    for rec in result.of_kind(RecommendationKind.EMPHASIZE) + result.of_kind(
        RecommendationKind.SAFE_REWORD
    ):
        if rec.body.startswith("Lead the skills line"):
            continue
        assert not validator.validate(rec.body).blocked, rec.body


def test_gaps_are_stated_plainly_not_papered_over(session):
    job = _job(session, RAG_HEAVY_JD)
    result = ResumeRecommendationService(session).build(job)
    problems = result.of_kind(RecommendationKind.GAP) + result.of_kind(
        RecommendationKind.DO_NOT_ADD
    )
    assert problems
    for rec in problems:
        assert rec.rationale


def test_ownership_gated_evidence_is_its_own_category(session):
    job = _job(session, CLEAN_FIT_JD)
    result = ResumeRecommendationService(session).build(job)
    for rec in result.of_kind(RecommendationKind.OWNERSHIP_GATED):
        assert "ownership" in rec.rationale.lower()
        assert "project-level" in rec.rationale.lower()


def test_de_emphasize_names_irrelevant_evidence(session):
    job = _job(session, CLEAN_FIT_JD, title="Data Engineer, New Grad 2027")
    result = ResumeRecommendationService(session).build(job)
    for rec in result.of_kind(RecommendationKind.DE_EMPHASIZE):
        assert rec.evidence_ids
        assert "drop" in rec.body.lower() or "shorten" in rec.body.lower()


def test_recommendations_are_ordered_actionable_first(session):
    job = _job(session, RAG_HEAVY_JD)
    result = ResumeRecommendationService(session).build(job)
    kinds = [r.kind for r in result.recommendations]
    if RecommendationKind.EMPHASIZE in kinds and RecommendationKind.DE_EMPHASIZE in kinds:
        assert kinds.index(RecommendationKind.EMPHASIZE) < kinds.index(
            RecommendationKind.DE_EMPHASIZE
        )


def test_a_job_with_no_analysis_yields_nothing_rather_than_guessing(session, sample_jd):
    job = (
        JobPipeline(session)
        .ingest(
            RawJobInput(
                company_name="No Analysis Co",
                title="Data Scientist, Product",
                description=sample_jd,
                location="Boston, MA",
            ),
            analyze=False,
        )
        .job
    )
    result = ResumeRecommendationService(session).build(job)
    # Only the whole-resume advice that needs no requirements can appear.
    assert not result.of_kind(RecommendationKind.GAP)
    assert not result.of_kind(RecommendationKind.DO_NOT_ADD)


def test_recommendations_persist_and_replace(session):
    job = _job(session, CLEAN_FIT_JD)
    service = ResumeRecommendationService(session)
    seed_resume_bases(session)

    first = service.build(job)
    service.save(first)
    session.flush()
    stored = service.stored(job.job_id)
    assert len(stored) == len(first.recommendations)

    service.save(service.build(job))
    session.flush()
    assert len(service.stored(job.job_id)) == len(stored)
    all_rows = session.execute(select(ResumeRecommendation)).scalars().all()
    assert len(all_rows) == len(stored), "old recommendations were not replaced"


def test_resume_version_records_evidence_used(session):
    job = _job(session, CLEAN_FIT_JD)
    service = ResumeRecommendationService(session)
    seed_resume_bases(session)
    result = service.build(job)

    version = service.record_version(job, base=result.resume_base, label="v1", result=result)
    assert version is not None
    assert version.job_id == job.job_id
    assert version.evidence_bank_version == "0.1"


def test_recommendation_set_serializes(session):
    job = _job(session, CLEAN_FIT_JD)
    payload = ResumeRecommendationService(session).build(job).as_dict()
    assert payload["job_id"] == job.job_id
    assert "counts" in payload
    assert isinstance(payload["recommendations"], list)


# ---------------------------------------------------------------------------
# Skill-gap baseline
# ---------------------------------------------------------------------------


def test_discovered_jobs_do_not_contaminate_the_baseline(session):
    """Only jobs the candidate actually chose to pursue count."""
    _job(session, RAG_HEAVY_JD, company="Merely Discovered Inc")
    session.flush()

    baseline = SkillGapService(session).baseline()
    assert baseline.jobs_considered == 0
    assert baseline.gaps == []


def test_shortlisting_admits_a_job_to_the_baseline(session):
    job = _job(session, RAG_HEAVY_JD)
    ApplicationService(session).transition(job, ApplicationStatus.SHORTLISTED)
    session.flush()

    baseline = SkillGapService(session).baseline()
    assert baseline.jobs_considered == 1
    assert baseline.gaps
    assert {g.skill for g in baseline.gaps} & {"RAG", "Tableau", "LangChain"}


def test_applying_admits_a_job_to_the_baseline(session):
    job = _job(session, RAG_HEAVY_JD)
    ApplicationService(session).transition(job, ApplicationStatus.APPLIED)
    session.flush()
    assert SkillGapService(session).baseline().jobs_considered == 1


def test_a_rejected_job_that_was_once_shortlisted_still_counts(session):
    """Intent was expressed; the outcome does not retract it."""
    job = _job(session, RAG_HEAVY_JD)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.SHORTLISTED)
    service.transition(job, ApplicationStatus.REJECTED)
    session.flush()
    assert SkillGapService(session).baseline().jobs_considered == 1


def test_gaps_report_evidence_strength_and_representative_jobs(session):
    job = _job(session, RAG_HEAVY_JD)
    ApplicationService(session).transition(job, ApplicationStatus.SHORTLISTED)
    session.flush()

    gap = SkillGapService(session).baseline().gaps[0]
    assert gap.job_count >= 1
    assert gap.weighted_job_count > 0
    assert gap.representative_jobs
    assert gap.representative_jobs[0]["job_id"] == job.job_id
    assert gap.evidence_level in {
        "LEARNING_NOT_RESUME_READY",
        "PENDING_USER_CONFIRMATION",
        "UNSUPPORTED_DO_NOT_USE",
        "SELF_REPORTED_LIMITED",
        "NONE",
    }


def test_small_sample_is_flagged(session):
    job = _job(session, RAG_HEAVY_JD)
    ApplicationService(session).transition(job, ApplicationStatus.SHORTLISTED)
    session.flush()
    assert SkillGapService(session).baseline().small_sample is True


def test_baseline_never_creates_learning_tasks(session):
    """Observation, not obligation."""
    from app.models.misc import SystemTask

    job = _job(session, RAG_HEAVY_JD)
    ApplicationService(session).transition(job, ApplicationStatus.SHORTLISTED)
    session.flush()

    payload = SkillGapService(session).baseline().as_dict()
    assert "No learning tasks are created" in str(payload["note"])
    assert session.execute(select(SystemTask)).scalars().all() == []


def test_lane_filter_narrows_the_population(session, sample_jd):
    fall = _job(session, sample_jd, company="Fall Corp", title="Data Scientist, Product")
    full = _job(session, RAG_HEAVY_JD, company="FullTime Corp")
    service = ApplicationService(session)
    service.transition(fall, ApplicationStatus.SHORTLISTED)
    service.transition(full, ApplicationStatus.SHORTLISTED)
    session.flush()

    gaps = SkillGapService(session)
    assert gaps.baseline().jobs_considered == 2
    # Both fixtures are full-time postings since D-51, so the lane filter no
    # longer splits them. What still matters is that it is applied and that the
    # population is reported honestly.
    scoped = gaps.baseline(lane="FULL_TIME_2027")
    assert scoped.jobs_considered <= gaps.baseline().jobs_considered
    assert "FULL_TIME_2027" in scoped.population
    assert "FULL_TIME_2027" in gaps.baseline(lane="FULL_TIME_2027").population


def test_demo_jobs_are_excluded_from_gaps(session):
    from app.services.demo import seed_demo_data

    seed_demo_data(session)
    session.flush()
    for job in session.execute(
        select(__import__("app.models.ingestion", fromlist=["Job"]).Job)
    ).scalars():
        if job.is_demo and job.application:
            job.application.current_status = ApplicationStatus.APPLIED.value
    session.flush()

    assert SkillGapService(session).baseline().jobs_considered == 0
