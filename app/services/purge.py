"""Targeted, auditable deletion of a single job and its dependents.

Deleting from the real database is a stop-and-ask operation, so this service is
built to be *inspected before it runs*: :func:`plan_purge` reports exactly what
would be removed and touches nothing. :func:`purge_job` then removes precisely
that set.

Scope is deliberately narrow. This removes one job, the records that exist only
because that job exists, and — only when it would otherwise be orphaned — its
company. It never matches on a name pattern and never cascades to a second job.

**Contacts survive; their link to the job does not.** A person is not a
disposable job record: `job_contact` rows go, `contact` rows stay, and the
networking history the user built is not collateral damage from deleting a posting.

Job-scoped documents and CRM rows DO go — resume versions, cover letters,
referrals, follow-ups, outreach and interview records generated for this job.
Several of those tables carry a `job_id` with no foreign key, so they are
deleted explicitly; a cascade alone would leave them dangling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.analysis import JobRequirement
from app.models.ingestion import Company, DedupeDecision, Job, RawJobSourceRecord


@dataclass
class PurgePlan:
    """What a purge would delete. Produced without modifying anything."""

    job_id: str
    company_name: str
    title: str
    is_demo: bool
    counts: dict[str, int] = field(default_factory=dict)
    company_id: str | None = None
    company_will_be_removed: bool = False
    blocked_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked_reason is None

    @property
    def total_rows(self) -> int:
        return sum(self.counts.values()) + (1 if self.company_will_be_removed else 0)

    def describe(self) -> str:
        lines = [f"{self.job_id} — {self.company_name}: {self.title}"]
        if self.blocked_reason:
            lines.append(f"  BLOCKED: {self.blocked_reason}")
            return "\n".join(lines)
        for name, count in sorted(self.counts.items()):
            if count:
                lines.append(f"  {name:<26} {count}")
        if self.company_will_be_removed:
            lines.append(f"  {'company (now orphaned)':<26} 1")
        lines.append(f"  {'TOTAL ROWS':<26} {self.total_rows}")
        return "\n".join(lines)


def plan_purge(session: Session, job_id: str) -> PurgePlan:
    """Report what :func:`purge_job` would delete. Read-only."""
    job = session.get(Job, job_id)
    if job is None:
        return PurgePlan(
            job_id=job_id,
            company_name="",
            title="",
            is_demo=False,
            blocked_reason="No job with that id.",
        )

    plan = PurgePlan(
        job_id=job.job_id,
        company_name=job.company_name,
        title=job.title,
        is_demo=job.is_demo,
        company_id=job.company_id,
    )

    analyses = list(job.analyses)
    analysis_ids = [a.analysis_id for a in analyses]
    requirements = (
        session.execute(select(JobRequirement).where(JobRequirement.analysis_id.in_(analysis_ids)))
        .scalars()
        .all()
        if analysis_ids
        else []
    )

    application = job.application
    plan.counts = {
        "job": 1,
        "job_source_association": len(job.sources),
        "job_version": len(job.versions),
        "job_analysis": len(analyses),
        "job_requirement": len(requirements),
        "evidence_match": sum(len(r.matches) for r in requirements),
        "priority_score": sum(1 for a in analyses if a.priority_score is not None),
        "eligibility_assessment": sum(1 for a in analyses if a.eligibility is not None),
        "legitimacy_assessment": sum(1 for a in analyses if a.legitimacy is not None),
        "sponsorship_signal": sum(1 for a in analyses if a.sponsorship is not None),
        "application": 1 if application else 0,
        "application_status_history": len(application.status_history) if application else 0,
        "application_note": len(application.notes) if application else 0,
        "raw_job_source_record": len(_raw_records(session, job_id)),
        "dedupe_decision": len(_dedupe_decisions(session, job_id)),
    }

    # These carry a job_id but have NO foreign key to `job`, so nothing deletes
    # them on their own — they would dangle against a missing job. They exist
    # because a job existed, and they go with it.
    #
    # `job_contact` is the exception that proves the rule: the LINK goes, the
    # contact does not. A person is not a disposable job record, and the user may
    # have talked to them about something else.
    for label, rows in _unlinked_dependents(session, job_id).items():
        plan.counts[label] = len(rows)

    if job.company_id:
        siblings = (
            session.execute(
                select(Job).where(Job.company_id == job.company_id, Job.job_id != job.job_id)
            )
            .scalars()
            .all()
        )
        plan.company_will_be_removed = not siblings

    return plan


def _raw_records(session: Session, job_id: str) -> list[RawJobSourceRecord]:
    return list(
        session.execute(select(RawJobSourceRecord).where(RawJobSourceRecord.job_id == job_id))
        .scalars()
        .all()
    )


def _dedupe_decisions(session: Session, job_id: str) -> list[DedupeDecision]:
    return list(
        session.execute(
            select(DedupeDecision).where(
                or_(DedupeDecision.job_id_a == job_id, DedupeDecision.job_id_b == job_id)
            )
        )
        .scalars()
        .all()
    )


def _unlinked_dependents(session: Session, job_id: str) -> dict[str, list[Any]]:
    """Job-scoped rows that no cascade reaches.

    Every one of these tables has a `job_id` column and no foreign key to
    `job`, so a plain `session.delete(job)` leaves them pointing at nothing.
    """
    from app.models.misc import (
        AIUsage,
        CoverLetterVersion,
        FollowUp,
        Interview,
        InterviewPrepItem,
        JobContact,
        OutreachInteraction,
        Referral,
        ResumeRecommendation,
        ResumeVersion,
    )

    found: dict[str, list[Any]] = {}
    for label, model in (
        ("resume_version", ResumeVersion),
        ("resume_recommendation", ResumeRecommendation),
        ("cover_letter_version", CoverLetterVersion),
        ("job_contact", JobContact),
        ("referral", Referral),
        ("follow_up", FollowUp),
        ("outreach_interaction", OutreachInteraction),
        ("interview", Interview),
        ("interview_prep_item", InterviewPrepItem),
        ("ai_usage", AIUsage),
    ):
        rows = list(
            session.execute(select(model).where(model.job_id == job_id)).scalars().all()
        )
        if rows:
            found[label] = rows
    return found


def purge_job(session: Session, job_id: str) -> PurgePlan:
    """Delete one job and everything that exists only because of it.

    Returns the plan that was executed. Raises ``ValueError`` if the job does
    not exist, so a typo can never be mistaken for a successful no-op.
    """
    plan = plan_purge(session, job_id)
    if not plan.ok:
        raise ValueError(plan.blocked_reason)

    job = session.get(Job, job_id)
    assert job is not None  # plan_purge already proved this

    # Raw staging and dedupe decisions have no ORM cascade: raw staging is
    # append-only by design and a dedupe decision belongs to a *pair* of jobs.
    # Both must go explicitly or they would dangle against a missing job.
    for raw in _raw_records(session, job_id):
        session.delete(raw)
    for decision in _dedupe_decisions(session, job_id):
        session.delete(decision)

    # Job-scoped rows with no FK cascade. Deleted before the job so nothing is
    # left pointing at a row that no longer exists.
    for rows in _unlinked_dependents(session, job_id).values():
        for row in rows:
            session.delete(row)
    session.flush()

    company_id = job.company_id if plan.company_will_be_removed else None

    # Everything else falls out of the delete-orphan cascades on Job.
    session.delete(job)
    session.flush()

    if company_id:
        company = session.get(Company, company_id)
        if company is not None:
            session.delete(company)
            session.flush()

    return plan
