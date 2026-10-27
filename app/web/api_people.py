"""The People CRM's browser surface.

Slice 2 built the primitives and left the UX unbuilt, so every table stayed
empty and no referral ever happened. These are the endpoints the People page and
a job's Networking tab need to make the loop actually run: find someone, record
them, draft a message, mark it sent yourself, be reminded, record the reply,
track the referral.

**The system drafts, tracks and reminds. It never sends.** There is no transport
here, `sent_by_user` is only ever set from a request the user made after sending
the message themselves, and the discovery endpoint returns SEARCH LINKS rather
than scraped people — no account is automated and no credential is supplied.

Referral rules warn and never block. Asking two people at one company for the
same job is usually a mistake and occasionally exactly right, so the second ask
carries a visible warning and proceeds.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.base import as_utc, utcnow
from app.models.enums import ContactStatus, FollowUpKind, ReferralState
from app.models.ingestion import Job
from app.models.misc import Contact, FollowUp, JobContact, OutreachInteraction
from app.services.contact_discovery import ContactDiscoveryService
from app.services.outreach_draft import OutreachDraftService
from app.services.people import OUTREACH_FOLLOW_UP_DAYS, PeopleService
from app.web.schemas import (
    ContactDetailView,
    ContactDiscoveryResponse,
    ContactHistoryResponse,
    ContactUpdatePayload,
    GenerateDraftPayload,
    LinkContactPayload,
    MarkSentDetailResponse,
    MarkSentPayload,
    OutreachDraftView,
    PeopleSummaryResponse,
    ReplyPayload,
    SnoozePayload,
)

api_people = APIRouter(prefix="/api")

#: What a job/contact link is FOR. Free text on the model; a closed list here so
#: the UI can offer sensible options without the backend policing vocabulary.
LINK_PURPOSES = ("REFERRAL", "INFORMATIONAL", "RECRUITER", "HIRING_TEAM", "OTHER")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _contact_or_404(db: Session, contact_id: str) -> Contact:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _next_step(contact: Contact, *, overdue: bool, awaiting: bool) -> tuple[str, str]:
    """One concrete step for this relationship.

    The same rule NextActionService applies to a job: name the thing to do and
    why now, and never name something the system would do itself.
    """
    status = contact.status
    if status == ContactStatus.IDENTIFIED.value:
        return ("Draft outreach", "Recorded, not yet contacted.")
    if status == ContactStatus.OUTREACH_READY.value:
        return (
            "Send it yourself",
            "A draft is ready. Copy it into LinkedIn or your email client — "
            "this system never sends.",
        )
    if status == ContactStatus.OUTREACH_SENT.value:
        if overdue:
            return ("Send a follow-up", "Sent, no reply recorded, and the follow-up is due.")
        return ("Wait for a reply", "Sent. The follow-up reminder is set.")
    if status == ContactStatus.REPLIED.value:
        return ("Review the reply", "They answered. Decide whether a referral ask fits.")
    if status == ContactStatus.CONVERSATION.value:
        return ("Keep the conversation going", "Active thread.")
    if status == ContactStatus.REFERRAL_REQUESTED.value:
        return ("Await the referral", "Referral asked for; no outcome recorded yet.")
    if status == ContactStatus.REFERRED.value:
        return ("Apply with the referral", "Referred — make sure the application cites it.")
    if status == ContactStatus.NO_RESPONSE.value:
        return ("Close it or try once more", "No answer recorded.")
    if status == ContactStatus.DECLINED.value:
        return ("Nothing further", "They declined.")
    if status == ContactStatus.CLOSED.value:
        return ("Nothing further", "Closed by you.")
    if awaiting:
        return ("Wait for a reply", "An ask is outstanding.")
    return ("Review", "No recorded state suggests a specific step.")


def _contact_detail(db: Session, contact: Contact) -> dict[str, Any]:
    links = list(
        db.execute(select(JobContact).where(JobContact.contact_id == contact.contact_id))
        .scalars()
        .all()
    )
    linked_jobs: list[dict[str, Any]] = []
    for link in links:
        job = db.get(Job, link.job_id)
        if job is None:
            continue
        linked_jobs.append(
            {
                "job_id": job.job_id,
                "title": job.title,
                "company": job.company_name,
                "purpose": link.relevance or "OTHER",
            }
        )

    interactions = list(
        db.execute(
            select(OutreachInteraction).where(
                OutreachInteraction.contact_id == contact.contact_id
            )
        )
        .scalars()
        .all()
    )
    awaiting = any(i.sent_by_user and i.reply_received_at is None for i in interactions)
    overdue = bool(contact.next_follow_up and contact.next_follow_up < date.today())

    referral_state = ReferralState.NONE.value
    from app.models.misc import Referral

    referrals = list(
        db.execute(select(Referral).where(Referral.contact_id == contact.contact_id))
        .scalars()
        .all()
    )
    if referrals:
        rank = [
            ReferralState.SUBMITTED_WITH_REFERRAL,
            ReferralState.RECEIVED,
            ReferralState.REQUESTED,
            ReferralState.POTENTIAL,
            ReferralState.DECLINED,
            ReferralState.NO_RESPONSE,
        ]
        states = {r.status for r in referrals}
        referral_state = next((s.value for s in rank if s.value in states), ReferralState.NONE.value)

    action, detail = _next_step(contact, overdue=overdue, awaiting=awaiting)
    return ContactDetailView(
        contact_id=contact.contact_id,
        name=contact.name,
        company_name=contact.company_name,
        role=contact.role,
        profile_url=contact.profile_url,
        email=contact.email,
        contact_type=contact.relationship_type,
        warmth=contact.warmth,
        status=contact.status,
        shared_context=contact.shared_context,
        last_contact_at=_iso(as_utc(contact.last_contact_at)),
        next_follow_up=contact.next_follow_up.isoformat() if contact.next_follow_up else None,
        notes=contact.notes,
        linked_jobs=linked_jobs,
        referral_state=referral_state,
        outreach_count=len(interactions),
        awaiting_reply=awaiting,
        follow_up_overdue=overdue,
        next_action=action,
        next_action_detail=detail,
    ).model_dump()


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


# ---------------------------------------------------------------------------
# The People page
# ---------------------------------------------------------------------------


@api_people.get("/people/summary", response_model=PeopleSummaryResponse)
def people_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything the People page shows, counted from records."""
    service = PeopleService(db)
    contacts = list(
        db.execute(select(Contact).where(Contact.is_demo.is_(False))).scalars().all()
    )
    rows = [_contact_detail(db, c) for c in contacts]

    action_states = {
        ContactStatus.IDENTIFIED.value,
        ContactStatus.OUTREACH_READY.value,
        ContactStatus.REPLIED.value,
    }
    action_required = [
        r for r in rows if r["status"] in action_states or r["follow_up_overdue"]
    ]
    recent = sorted(
        [r for r in rows if r["last_contact_at"]],
        key=lambda r: r["last_contact_at"],
        reverse=True,
    )[:8]

    return {
        "kpis": service.summary(),
        "action_required": action_required,
        "follow_ups_due": [_follow_up_row(f) for f in service.due_follow_ups(within_days=7)],
        "recent": recent,
        "contacts": rows,
        "filters": {
            "companies": sorted({r["company_name"] for r in rows if r["company_name"]}),
            "statuses": sorted({r["status"] for r in rows}),
            "relationships": sorted({r["contact_type"] for r in rows}),
            "referral_states": sorted({r["referral_state"] for r in rows}),
        },
    }


@api_people.get("/contacts/{contact_id}/history", response_model=ContactHistoryResponse)
def contact_history(contact_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    contact = _contact_or_404(db, contact_id)
    interactions = list(
        db.execute(
            select(OutreachInteraction)
            .where(OutreachInteraction.contact_id == contact_id)
            .order_by(OutreachInteraction.created_at.desc())
        )
        .scalars()
        .all()
    )
    follow_ups = list(
        db.execute(select(FollowUp).where(FollowUp.contact_id == contact_id)).scalars().all()
    )
    return {
        "contact": _contact_detail(db, contact),
        "outreach": [
            {
                "id": i.id,
                "contact_id": i.contact_id,
                "job_id": i.job_id,
                "channel": i.channel,
                "draft_body": i.draft_body,
                "sent_by_user": i.sent_by_user,
                "sent_at": _iso(as_utc(i.sent_at)),
                "reply_received_at": _iso(as_utc(i.reply_received_at)),
                "created_at": _iso(as_utc(i.created_at)),
            }
            for i in interactions
        ],
        "follow_ups": [_follow_up_row(f) for f in follow_ups],
    }


@api_people.post("/contacts/{contact_id}/update", response_model=ContactDetailView)
def update_contact(
    contact_id: str, payload: ContactUpdatePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    contact = _contact_or_404(db, contact_id)
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        PeopleService(db).update_contact(contact, **fields)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _contact_detail(db, contact)


@api_people.post("/contacts/{contact_id}/close", response_model=ContactDetailView)
def close_contact(
    contact_id: str, payload: ReplyPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    contact = _contact_or_404(db, contact_id)
    PeopleService(db).close_contact(contact, notes=payload.notes)
    db.commit()
    return _contact_detail(db, contact)


@api_people.post("/contacts/{contact_id}/reply", response_model=ContactDetailView)
def record_reply(
    contact_id: str, payload: ReplyPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """A human heard back. Never inferred — silence is not a reply."""
    contact = _contact_or_404(db, contact_id)
    when = _parse_datetime(payload.received_at)
    PeopleService(db).record_reply(contact, when=when, notes=payload.notes)
    db.commit()
    return _contact_detail(db, contact)


# ---------------------------------------------------------------------------
# Per-job networking
# ---------------------------------------------------------------------------


@api_people.get("/jobs/{job_id}/contact-discovery", response_model=ContactDiscoveryResponse)
def contact_discovery(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Who to contact about this job: people already known, plus searches to run.

    Returns strings. Nothing is fetched, no account is used, and the user picks
    the actual person.
    """
    job = _job_or_404(db, job_id)
    return ContactDiscoveryService(db).for_job(job).as_dict()


@api_people.post("/jobs/{job_id}/contacts/link", response_model=ContactDetailView)
def link_contact(
    job_id: str, payload: LinkContactPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Link an existing contact to this job. One person, many roles."""
    job = _job_or_404(db, job_id)
    contact = _contact_or_404(db, payload.contact_id)
    purpose = payload.purpose.upper()
    if purpose not in LINK_PURPOSES:
        purpose = "OTHER"
    PeopleService(db).link_to_job(contact, job, relevance=purpose)
    db.commit()
    return _contact_detail(db, contact)


@api_people.post("/jobs/{job_id}/contacts/{contact_id}/unlink", response_model=ContactDetailView)
def unlink_contact(job_id: str, contact_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    contact = _contact_or_404(db, contact_id)
    PeopleService(db).unlink_from_job(contact, job)
    db.commit()
    return _contact_detail(db, contact)


# ---------------------------------------------------------------------------
# Outreach: drafted here, sent by the user
# ---------------------------------------------------------------------------


@api_people.post("/contacts/{contact_id}/generate-draft", response_model=OutreachDraftView)
def generate_draft(
    contact_id: str, payload: GenerateDraftPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Compose a draft from recorded facts and store it.

    Storing it is not sending it: `sent_by_user` stays False until a human says
    otherwise, and there is no transport in this process.
    """
    contact = _contact_or_404(db, contact_id)
    job = _job_or_404(db, payload.job_id) if payload.job_id else None

    draft = OutreachDraftService().draft(contact=contact, job=job, channel=payload.channel)
    service = PeopleService(db)
    interaction = service.record_outreach_draft(
        contact,
        job=job,
        draft_body=draft.body,
        channel=draft.channel,
        evidence_ids=draft.evidence_ids,
    )
    if contact.status == ContactStatus.IDENTIFIED.value:
        service.set_outreach_state(contact, ContactStatus.OUTREACH_READY)
    db.commit()

    payload_out = draft.as_dict()
    payload_out["interaction_id"] = interaction.id
    return payload_out


@api_people.post("/outreach/{interaction_id}/sent", response_model=MarkSentDetailResponse)
def mark_sent(
    interaction_id: str, payload: MarkSentPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Record that *the user* sent this, and set the follow-up reminder."""
    interaction = db.get(OutreachInteraction, interaction_id)
    if interaction is None:
        raise HTTPException(status_code=404, detail="Outreach record not found")

    service = PeopleService(db)
    service.mark_sent_by_user(
        interaction,
        when=_parse_datetime(payload.sent_at),
        channel=payload.channel,
        notes=payload.notes,
        follow_up_on=payload.follow_up_on,
    )
    contact = db.get(Contact, interaction.contact_id)
    db.commit()
    return {
        "id": interaction.id,
        "sent_by_user": True,
        "sent_at": _iso(as_utc(interaction.sent_at)),
        "contact_status": contact.status if contact else "",
        "follow_up_due": (
            contact.next_follow_up.isoformat() if contact and contact.next_follow_up else None
        ),
        "note": (
            f"Recorded as sent by you. A follow-up is set for "
            f"{contact.next_follow_up.isoformat() if contact and contact.next_follow_up else 'later'}"
            f" — {OUTREACH_FOLLOW_UP_DAYS} days is the default and you can change it."
        ),
    }


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


@api_people.post("/follow-ups/{follow_up_id}/snooze")
def snooze_follow_up(
    follow_up_id: str, payload: SnoozePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Push a reminder out. It stays open — snoozing is not completing."""
    follow_up = db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    PeopleService(db).snooze_follow_up(follow_up, days=payload.days)
    if follow_up.contact_id:
        contact = db.get(Contact, follow_up.contact_id)
        if contact is not None:
            contact.next_follow_up = follow_up.due_date
    db.commit()
    return _follow_up_row(follow_up)


@api_people.get("/follow-ups/networking")
def networking_follow_ups(within_days: int = 7, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Overdue / today / next 7 days, split so the page does not re-derive it."""
    rows = PeopleService(db).due_follow_ups(within_days=within_days)
    networking = [
        f
        for f in rows
        if f.kind
        in (FollowUpKind.NETWORKING_OUTREACH.value, FollowUpKind.REFERRAL_REQUEST.value)
    ]
    today = date.today()
    return {
        "overdue": [_follow_up_row(f) for f in networking if f.due_date < today],
        "today": [_follow_up_row(f) for f in networking if f.due_date == today],
        "upcoming": [_follow_up_row(f) for f in networking if f.due_date > today],
        "count": len(networking),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed) or utcnow()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
