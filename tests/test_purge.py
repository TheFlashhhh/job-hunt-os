"""Targeted job deletion.

Deleting from the real database is the most dangerous thing this codebase does,
so the tests care less about "does it delete" than about "does it delete *only*
that" — the blast radius is the thing under test.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models.crm import Application, ApplicationStatusHistory
from app.models.ingestion import Company, Job, JobSourceAssociation, RawJobSourceRecord
from app.services.pipeline import JobPipeline, RawJobInput
from app.services.purge import plan_purge, purge_job


def _ingest(session, company: str, jd: str, title="Data Scientist, Product"):
    return JobPipeline(session).ingest(
        RawJobInput(
            company_name=company,
            title=title,
            description=jd,
            location="Boston, MA",
        )
    )


def test_plan_reports_the_blast_radius_without_touching_anything(session, sample_jd):
    job = _ingest(session, "Solo Corp", sample_jd).job
    session.flush()

    plan = plan_purge(session, job.job_id)

    assert plan.ok
    assert plan.counts["job"] == 1
    assert plan.counts["job_analysis"] == 1
    assert plan.counts["application"] == 1
    assert plan.counts["raw_job_source_record"] == 1
    assert plan.total_rows > 5
    assert job.job_id in plan.describe()

    # Nothing was removed by planning.
    assert session.get(Job, job.job_id) is not None


def test_purge_removes_the_job_and_its_dependents(session, sample_jd):
    job = _ingest(session, "Solo Corp", sample_jd).job
    session.flush()
    job_id = job.job_id

    purge_job(session, job_id)
    session.flush()

    assert session.get(Job, job_id) is None
    for model in (RawJobSourceRecord, JobSourceAssociation, Application):
        remaining = session.execute(select(model).where(model.job_id == job_id)).scalars().all()
        assert not remaining, f"{model.__name__} left behind"
    assert not session.execute(select(ApplicationStatusHistory)).scalars().all()


def test_purge_leaves_every_other_job_untouched(session, sample_jd, fulltime_jd):
    victim = _ingest(session, "Solo Corp", sample_jd).job
    keeper_a = _ingest(session, "Northwind Analytics", sample_jd).job
    keeper_b = _ingest(session, "Harborline", fulltime_jd, title="Data Engineer, New Grad").job
    session.flush()
    keepers = [keeper_a.job_id, keeper_b.job_id]

    purge_job(session, victim.job_id)
    session.flush()

    surviving = {j.job_id for j in session.execute(select(Job)).scalars()}
    assert surviving == set(keepers)
    for job_id in keepers:
        assert session.get(Job, job_id).application is not None
        assert session.get(Job, job_id).analyses


def test_orphaned_company_is_removed(session, sample_jd):
    job = _ingest(session, "Solo Corp", sample_jd).job
    session.flush()
    company_id = job.company_id

    plan = plan_purge(session, job.job_id)
    assert plan.company_will_be_removed is True

    purge_job(session, job.job_id)
    session.flush()
    assert session.get(Company, company_id) is None


def test_company_with_other_jobs_survives(session, sample_jd, fulltime_jd):
    """A shared employer must not vanish because one of its jobs was removed."""
    first = _ingest(session, "Northwind Analytics", sample_jd).job
    second = _ingest(
        session, "Northwind Analytics", fulltime_jd, title="Data Engineer, New Grad 2027"
    ).job
    session.flush()
    company_id = first.company_id
    assert second.company_id == company_id

    plan = plan_purge(session, first.job_id)
    assert plan.company_will_be_removed is False

    purge_job(session, first.job_id)
    session.flush()

    assert session.get(Company, company_id) is not None
    assert session.get(Job, second.job_id) is not None


def test_unknown_job_id_raises_rather_than_silently_succeeding(session):
    plan = plan_purge(session, "JOB-nope")
    assert plan.ok is False
    assert "No job" in plan.blocked_reason

    with pytest.raises(ValueError):
        purge_job(session, "JOB-nope")


def test_purge_does_not_touch_unrelated_tables(session, sample_jd):
    """Contacts, follow-ups and anything hand-authored are out of scope."""
    from app.models.misc import Contact, FollowUp

    job = _ingest(session, "Solo Corp", sample_jd).job
    session.flush()

    session.add(Contact(name="A Person", company_name="Elsewhere Inc"))
    session.add(FollowUp(subject="Unrelated follow-up", due_date=date.today()))
    session.flush()

    purge_job(session, job.job_id)
    session.flush()

    assert session.execute(select(Contact)).scalars().all()
    assert session.execute(select(FollowUp)).scalars().all()
