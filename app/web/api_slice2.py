"""Slice 2 JSON API: CRM, people, follow-ups, referrals, resume, gaps.

API-first by design. Every capability here exists as an endpoint before it
exists as a page, so the Next.js frontend consumes exactly what the Jinja
templates consume and no business rule has to be reimplemented in either.

Route handlers stay thin: they validate input, call one service, and shape the
response. Nothing in this module decides what is safe to claim, which stage an
application may enter, or whether a referral ask is a duplicate — those live in
the services, which are the single source of truth for both frontends.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.evidence.service import get_evidence_bank
from app.models.enums import (
    ApplicationStatus,
    ContactStatus,
    ContactType,
    ContactWarmth,
    FollowUpKind,
    FollowUpPriority,
    ReferralState,
)
from app.models.ingestion import Job
from app.models.misc import Contact, FollowUp, OutreachInteraction, Referral
from app.services.crm import ApplicationService
from app.services.dashboard import ExecutionDashboardService
from app.services.gaps import SkillGapService
from app.services.people import PeopleService
from app.services.resume import ResumeRecommendationService, seed_resume_bases
from app.web.schemas import (
    ApplicationResponse,
    ContactsResponse,
    ContactView,
    DraftResponse,
    EnumsResponse,
    ExecutionResponse,
    FollowUpsResponse,
    FollowUpView,
    JobContactsResponse,
    MarkSentResponse,
    NextActionResponse,
    RecommendationsResponse,
    ReferralsResponse,
    ReferralUpsertResponse,
    ResumeBasesResponse,
    SkillGapsResponse,
    TransitionResponse,
)

api2 = APIRouter(prefix="/api")


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _contact_or_404(db: Session, contact_id: str) -> Contact:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


# ---------------------------------------------------------------------------
# Application CRM
# ---------------------------------------------------------------------------


class StatusPayload(BaseModel):
    status: str
    note: str | None = None


class NextActionPayload(BaseModel):
    action: str | None = None
    due: date | None = None


@api2.post("/jobs/{job_id}/status", response_model=TransitionResponse)
def set_status(
    job_id: str, payload: StatusPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    try:
        target = ApplicationStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown status: {payload.status}") from exc

    result = ApplicationService(db).transition(job, target, note=payload.note)
    db.commit()
    return {
        "job_id": job_id,
        "changed": result.changed,
        "from_status": result.from_status,
        "to_status": result.to_status,
        # Warnings are advisory: the transition already happened.
        "warnings": result.warnings,
    }


@api2.post("/jobs/{job_id}/next-action", response_model=NextActionResponse)
def set_next_action(
    job_id: str, payload: NextActionPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    application = ApplicationService(db).set_next_action(job, payload.action, payload.due)
    db.commit()
    return {
        "job_id": job_id,
        "next_action": application.next_action,
        "next_action_due": (
            application.next_action_due.isoformat() if application.next_action_due else None
        ),
    }


@api2.get("/jobs/{job_id}/application", response_model=ApplicationResponse)
def get_application(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    service = ApplicationService(db)
    application = service.get_or_create(job)
    people = PeopleService(db)
    db.commit()
    return {
        "job_id": job_id,
        "application_id": application.application_id,
        "current_status": application.current_status,
        "applied_at": application.applied_at.isoformat() if application.applied_at else None,
        "next_action": application.next_action,
        "next_action_due": (
            application.next_action_due.isoformat() if application.next_action_due else None
        ),
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "resume_version_id": application.resume_version_id,
        "referral_state": people.referral_state_for_job(job).value,
        "referral_used": application.referral_used,
        "is_stale": service.is_stale(application),
        "days_since_change": service.days_since_last_change(application),
        "stages_reached": sorted(service.stages_reached(application)),
        "timeline": [
            {
                "at": entry.at.isoformat(),
                "from": entry.from_status,
                "to": entry.to_status,
                "by": entry.changed_by,
                "note": entry.note,
                "warnings": entry.warnings,
            }
            for entry in service.timeline(application)
        ],
        "notes": [
            {"body": n.body, "at": n.created_at.isoformat(), "author": n.author}
            for n in application.notes
        ],
    }


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class ContactPayload(BaseModel):
    name: str = Field(min_length=1)
    company_name: str | None = None
    role: str | None = None
    profile_url: str | None = None
    email: str | None = None
    contact_type: str = ContactType.OTHER.value
    warmth: str = ContactWarmth.UNKNOWN.value
    shared_context: str | None = None
    notes: str | None = None
    next_follow_up: date | None = None
    #: Verification and demo data. Excluded from every count and panel.
    is_demo: bool = False


class OutreachStatePayload(BaseModel):
    status: str


class DraftPayload(BaseModel):
    job_id: str | None = None
    body: str = Field(min_length=1)
    channel: str = "MANUAL"
    evidence_ids: list[str] | None = None


def _contact_row(contact: Contact) -> dict[str, Any]:
    return {
        "contact_id": contact.contact_id,
        "name": contact.name,
        "company_name": contact.company_name,
        "role": contact.role,
        "profile_url": contact.profile_url,
        "contact_type": contact.relationship_type,
        "warmth": contact.warmth,
        "status": contact.status,
        "shared_context": contact.shared_context,
        "last_contact_at": (
            contact.last_contact_at.isoformat() if contact.last_contact_at else None
        ),
        "next_follow_up": contact.next_follow_up.isoformat() if contact.next_follow_up else None,
        "notes": contact.notes,
    }


@api2.get("/contacts", response_model=ContactsResponse)
def list_contacts(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(Contact).where(Contact.is_demo.is_(False))).scalars().all()
    return {"count": len(rows), "contacts": [_contact_row(c) for c in rows]}


@api2.post("/contacts", response_model=ContactView)
def create_contact(payload: ContactPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        contact = PeopleService(db).create_contact(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _contact_row(contact)


@api2.post("/contacts/{contact_id}/outreach-state", response_model=ContactView)
def set_outreach_state(
    contact_id: str, payload: OutreachStatePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    contact = _contact_or_404(db, contact_id)
    try:
        status = ContactStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown state: {payload.status}") from exc
    PeopleService(db).set_outreach_state(contact, status)
    db.commit()
    return _contact_row(contact)


@api2.post("/contacts/{contact_id}/draft", response_model=DraftResponse)
def create_draft(
    contact_id: str, payload: DraftPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Store an outreach draft. This never sends anything."""
    contact = _contact_or_404(db, contact_id)
    job = _job_or_404(db, payload.job_id) if payload.job_id else None
    interaction = PeopleService(db).record_outreach_draft(
        contact,
        job=job,
        draft_body=payload.body,
        channel=payload.channel,
        evidence_ids=payload.evidence_ids,
    )
    db.commit()
    return {
        "id": interaction.id,
        "contact_id": contact_id,
        "job_id": interaction.job_id,
        "sent_by_user": interaction.sent_by_user,
        "note": "Draft stored. Sending is manual — this system never sends messages.",
    }


@api2.post("/outreach/{interaction_id}/mark-sent", response_model=MarkSentResponse)
def mark_sent(interaction_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Record that *the user* sent a draft. The system cannot set this itself."""
    interaction = db.get(OutreachInteraction, interaction_id)
    if interaction is None:
        raise HTTPException(status_code=404, detail="Outreach record not found")
    PeopleService(db).mark_sent_by_user(interaction)
    db.commit()
    return {
        "id": interaction.id,
        "sent_by_user": True,
        "sent_at": interaction.sent_at.isoformat() if interaction.sent_at else None,
    }


@api2.get("/jobs/{job_id}/contacts", response_model=JobContactsResponse)
def contacts_for_job(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    contacts = PeopleService(db).contacts_for_job(job)
    return {
        "job_id": job_id,
        "count": len(contacts),
        "contacts": [_contact_row(c) for c in contacts],
    }


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


class FollowUpPayload(BaseModel):
    subject: str = Field(min_length=1)
    due_date: date
    kind: str = FollowUpKind.OTHER.value
    priority: str = FollowUpPriority.NORMAL.value
    job_id: str | None = None
    contact_id: str | None = None
    notes: str | None = None


def _follow_up_row(follow_up: FollowUp) -> dict[str, Any]:
    return {
        "id": follow_up.id,
        "subject": follow_up.subject,
        "kind": follow_up.kind,
        "priority": follow_up.priority,
        "due_date": follow_up.due_date.isoformat(),
        "overdue": follow_up.due_date < date.today() and not follow_up.completed,
        "completed": follow_up.completed,
        "job_id": follow_up.job_id,
        "contact_id": follow_up.contact_id,
        "notes": follow_up.notes,
    }


@api2.get("/follow-ups", response_model=FollowUpsResponse)
def list_follow_ups(within_days: int = 7, db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = PeopleService(db).due_follow_ups(within_days=within_days)
    return {"count": len(rows), "follow_ups": [_follow_up_row(f) for f in rows]}


@api2.post("/follow-ups", response_model=FollowUpView)
def create_follow_up(payload: FollowUpPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, payload.job_id) if payload.job_id else None
    contact = _contact_or_404(db, payload.contact_id) if payload.contact_id else None
    try:
        follow_up = PeopleService(db).create_follow_up(
            subject=payload.subject,
            due_date=payload.due_date,
            kind=payload.kind,
            priority=payload.priority,
            job=job,
            contact=contact,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _follow_up_row(follow_up)


@api2.post("/follow-ups/{follow_up_id}/complete", response_model=FollowUpView)
def complete_follow_up(follow_up_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    follow_up = db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    PeopleService(db).complete_follow_up(follow_up)
    db.commit()
    return _follow_up_row(follow_up)


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------


class ReferralPayload(BaseModel):
    contact_id: str
    state: str = ReferralState.REQUESTED.value
    notes: str | None = None


@api2.get("/jobs/{job_id}/referrals", response_model=ReferralsResponse)
def list_referrals(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    service = PeopleService(db)
    rows: list[dict[str, Any]] = []
    for referral in service.referrals_for_job(job):
        contact = db.get(Contact, referral.contact_id)
        rows.append(
            {
                "id": referral.id,
                "contact_id": referral.contact_id,
                "contact_name": contact.name if contact else None,
                "state": referral.status,
                "requested_at": (
                    referral.requested_at.isoformat() if referral.requested_at else None
                ),
                "resolved_at": referral.resolved_at.isoformat() if referral.resolved_at else None,
                "notes": referral.notes,
            }
        )
    return {
        "job_id": job_id,
        "job_state": service.referral_state_for_job(job).value,
        "referrals": rows,
    }


@api2.post("/jobs/{job_id}/referrals", response_model=ReferralUpsertResponse)
def upsert_referral(
    job_id: str, payload: ReferralPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Record a referral state. Duplicate asks warn; they are never blocked."""
    job = _job_or_404(db, job_id)
    contact = _contact_or_404(db, payload.contact_id)
    try:
        state = ReferralState(payload.state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown state: {payload.state}") from exc

    outcome = PeopleService(db).request_referral(
        job=job, contact=contact, state=state, notes=payload.notes
    )
    ApplicationService(db).set_referral(job, state=state, contact_id=contact.contact_id)
    db.commit()
    return {
        "job_id": job_id,
        "contact_id": contact.contact_id,
        "state": outcome.referral.status,
        "created": outcome.created,
        "blocked": outcome.blocked,
        "warnings": [{"code": w.code, "message": w.message} for w in outcome.warnings],
    }


# ---------------------------------------------------------------------------
# Resume recommendations
# ---------------------------------------------------------------------------


@api2.get("/jobs/{job_id}/resume-recommendations", response_model=RecommendationsResponse)
def resume_recommendations(
    job_id: str, base: str | None = None, refresh: bool = False, db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    seed_resume_bases(db)
    service = ResumeRecommendationService(db)

    stored = service.stored(job_id)
    if stored and not refresh and base is None:
        # resume_base is read back from the stored rows. Omitting it made the
        # UI render "Selected base resume: not chosen yet" directly above a
        # list of proposals that could only exist because a base WAS chosen.
        return {
            "job_id": job_id,
            "cached": True,
            "resume_base": service.stored_resume_base(job_id),
            "recommendations": [_recommendation_row(row) for row in stored],
        }

    result = service.build(job, resume_base=base)
    rows = service.save(result)
    db.commit()
    payload = result.as_dict()
    payload["cached"] = False
    payload["recommendations"] = [_recommendation_row(row) for row in rows]
    return payload


def _evidence_labels(evidence_ids: list[str] | None) -> list[str]:
    """Human-readable project/experience names for a set of Evidence IDs.

    Resolved here rather than in React: the Evidence Bank is backend
    source-of-truth, and a frontend copy of the ID-to-name mapping would be
    exactly the duplication the API contract exists to prevent.

    Names are de-duplicated and order-preserved — four IDs from one project
    should read as that one project, not as the same name four times.
    """
    if not evidence_ids:
        return []
    bank = get_evidence_bank()
    labels: list[str] = []
    for evidence_id in evidence_ids:
        record = bank.get_evidence(evidence_id)
        name = getattr(record, "organization_or_project", None) if record else None
        if name and name not in labels:
            labels.append(name)
    return labels


def _recommendation_row(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "body": row.body,
        "rationale": row.rationale,
        "evidence_ids": row.evidence_ids or [],
        "evidence_labels": _evidence_labels(row.evidence_ids),
        "safe_claim_ids": row.safe_claim_ids or [],
        "requirement_text": row.requirement_text,
        "verdict": row.verdict,
        "rank": row.rank,
        "accepted": row.accepted,
    }


@api2.get("/resume-bases", response_model=ResumeBasesResponse)
def resume_bases(db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.models.misc import ResumeFamily
    from app.services import resume_library

    seed_resume_bases(db)
    db.commit()
    rows = {f.name: f for f in db.execute(select(ResumeFamily)).scalars().all()}
    # resume_bases.yaml is the order and the set; the DB row supplies what job
    # tailoring records against. Library fields describe the base itself.
    bases: list[dict[str, Any]] = []
    for info in resume_library.list_bases():
        row = rows.get(info.base_name)
        bases.append(
            {
                "name": info.base_name,
                "description": info.description or (row.description if row else None),
                "target_role_families": (row.target_role_families or []) if row else [],
                "active": row.active if row else True,
                "display_name": info.display_name,
                "best_for": info.best_for,
                "last_generated_at": (
                    info.last_generated_at.isoformat() if info.last_generated_at else None
                ),
                "pdf_filename": info.pdf_filename,
                "docx_filename": info.docx_filename,
            }
        )
    return {"bases": bases}


# ---------------------------------------------------------------------------
# Skill gaps and the execution dashboard
# ---------------------------------------------------------------------------


@api2.get("/skill-gaps", response_model=SkillGapsResponse)
def skill_gaps(lane: str = "ALL", db: Session = Depends(get_db)) -> dict[str, Any]:
    return SkillGapService(db).baseline(lane=lane).as_dict()


@api2.get("/execution", response_model=ExecutionResponse)
def execution_panel(lane: str = "ALL", db: Session = Depends(get_db)) -> dict[str, Any]:
    return ExecutionDashboardService(db).panel(lane=lane).as_dict()


@api2.get("/enums", response_model=EnumsResponse)
def enums() -> dict[str, list[str]]:
    """Vocabulary for the frontend, so no list is hand-copied into React."""
    return {
        "application_status": [s.value for s in ApplicationStatus],
        "contact_type": [s.value for s in ContactType],
        "contact_status": [s.value for s in ContactStatus],
        "contact_warmth": [s.value for s in ContactWarmth],
        "follow_up_kind": [s.value for s in FollowUpKind],
        "follow_up_priority": [s.value for s in FollowUpPriority],
        "referral_state": [s.value for s in ReferralState],
    }


__all__ = ["api2", "Referral"]
