"""Contacts, outreach records, follow-ups and referrals.

Nothing in this module sends anything. Outreach is *drafted and recorded*; the
act of sending is the user's, performed in their own email client or LinkedIn
session, and reported back here afterwards. `OutreachInteraction.sent_by_user`
is the flag for that, and only a human sets it. There is deliberately no
transport, no SMTP client, and no LinkedIn call anywhere in this file.

The referral rules are advisory in the same spirit. Asking two people at the
same company for a referral to the same job is usually a mistake, but sometimes
it is exactly right — the first contact went quiet, or the second is much closer
to the team. So a duplicate ask produces a visible warning and proceeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import as_utc, utcnow
from app.models.enums import (
    ContactStatus,
    ContactType,
    ContactWarmth,
    FollowUpKind,
    FollowUpPriority,
    ReferralState,
)
from app.models.ingestion import Job
from app.models.misc import Contact, FollowUp, JobContact, OutreachInteraction, Referral

#: Outreach states that mean an ask is currently outstanding with that person.
_ASK_OUTSTANDING = {
    ContactStatus.OUTREACH_SENT,
    ContactStatus.REPLIED,
    ContactStatus.CONVERSATION,
    ContactStatus.REFERRAL_REQUESTED,
}

_REFERRAL_OPEN = {ReferralState.REQUESTED, ReferralState.RECEIVED}

#: Default gap before a networking follow-up is worth sending.
DEFAULT_FOLLOW_UP_DAYS = 7

#: Gap after an outreach message specifically. Shorter than the general
#: default: a cold message that gets no answer in four days usually will not,
#: and a job search moves faster than a relationship does. The user can change
#: the date when marking the message sent — this is only the suggestion.
OUTREACH_FOLLOW_UP_DAYS = 4


@dataclass
class ContactWarning:
    code: str
    message: str


@dataclass
class ReferralOutcome:
    referral: Referral
    created: bool
    warnings: list[ContactWarning] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """Always False. Referral rules warn; they never block."""
        return False


class PeopleService:
    """Manual contact management. No automated sending, ever."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- contacts --------------------------------------------------------
    def create_contact(
        self,
        *,
        name: str,
        company_name: str | None = None,
        role: str | None = None,
        profile_url: str | None = None,
        email: str | None = None,
        contact_type: ContactType | str = ContactType.OTHER,
        warmth: ContactWarmth | str = ContactWarmth.UNKNOWN,
        shared_context: str | None = None,
        notes: str | None = None,
        status: ContactStatus | str = ContactStatus.IDENTIFIED,
        next_follow_up: date | None = None,
        is_demo: bool = False,
    ) -> Contact:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("A contact needs a name.")

        contact = Contact(
            name=clean_name,
            company_name=(company_name or "").strip() or None,
            role=(role or "").strip() or None,
            profile_url=(profile_url or "").strip() or None,
            email=(email or "").strip() or None,
            relationship_type=ContactType(contact_type).value,
            warmth=ContactWarmth(warmth).value,
            shared_context=(shared_context or "").strip() or None,
            status=ContactStatus(status).value,
            notes=(notes or "").strip() or None,
            next_follow_up=next_follow_up,
            is_demo=is_demo,
        )
        self.session.add(contact)
        self.session.flush()
        return contact

    def update_contact(self, contact: Contact, **fields: object) -> Contact:
        allowed = {
            "name",
            "company_name",
            "role",
            "profile_url",
            "email",
            "shared_context",
            "notes",
            "next_follow_up",
        }
        for key, value in fields.items():
            if key in allowed:
                setattr(contact, key, value)
            elif key == "contact_type":
                contact.relationship_type = ContactType(value).value  # type: ignore[arg-type]
            elif key == "warmth":
                contact.warmth = ContactWarmth(value).value  # type: ignore[arg-type]
            elif key == "status":
                contact.status = ContactStatus(value).value  # type: ignore[arg-type]
            else:
                raise ValueError(f"Unknown contact field: {key}")
        self.session.flush()
        return contact

    def set_outreach_state(
        self, contact: Contact, status: ContactStatus | str, *, when: datetime | None = None
    ) -> Contact:
        resolved = ContactStatus(status)
        contact.status = resolved.value
        if resolved in {ContactStatus.OUTREACH_SENT, ContactStatus.REPLIED}:
            contact.last_contact_at = when or utcnow()
        self.session.flush()
        return contact

    def link_to_job(
        self, contact: Contact, job: Job, *, relevance: str | None = None
    ) -> JobContact:
        existing = self.session.execute(
            select(JobContact).where(
                JobContact.job_id == job.job_id, JobContact.contact_id == contact.contact_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            if relevance:
                existing.relevance = relevance
            self.session.flush()
            return existing
        link = JobContact(
            job_id=job.job_id,
            contact_id=contact.contact_id,
            relevance=relevance,
            is_demo=contact.is_demo or job.is_demo,
        )
        self.session.add(link)
        self.session.flush()
        return link

    def contacts_for_job(self, job: Job) -> list[Contact]:
        contact_ids = [
            row.contact_id
            for row in self.session.execute(
                select(JobContact).where(JobContact.job_id == job.job_id)
            ).scalars()
        ]
        direct = (
            self.session.execute(select(Contact).where(Contact.contact_id.in_(contact_ids)))
            .scalars()
            .all()
            if contact_ids
            else []
        )

        # Same-company contacts are surfaced too: someone recorded for one
        # Acme role is a plausible path into another Acme role.
        same_company = []
        if job.normalized_company:
            same_company = list(
                self.session.execute(
                    select(Contact).where(
                        Contact.company_name.is_not(None),
                        Contact.contact_id.notin_(contact_ids or [""]),
                    )
                )
                .scalars()
                .all()
            )
            same_company = [
                c
                for c in same_company
                if _same_company(c.company_name or "", job.company_name or "")
            ]
        return list(direct) + same_company

    # -- outreach (drafted, never sent) ----------------------------------
    def record_outreach_draft(
        self,
        contact: Contact,
        *,
        job: Job | None = None,
        draft_body: str,
        channel: str = "MANUAL",
        evidence_ids: list[str] | None = None,
    ) -> OutreachInteraction:
        """Store a draft. ``sent_by_user`` stays False until a human says so."""
        interaction = OutreachInteraction(
            contact_id=contact.contact_id,
            job_id=job.job_id if job else None,
            channel=channel,
            direction="OUTBOUND",
            draft_body=draft_body,
            sent_by_user=False,
            evidence_ids_used=evidence_ids or None,
            is_demo=contact.is_demo or bool(job and job.is_demo),
        )
        self.session.add(interaction)
        self.session.flush()
        return interaction

    def mark_sent_by_user(
        self,
        interaction: OutreachInteraction,
        *,
        when: datetime | None = None,
        channel: str | None = None,
        notes: str | None = None,
        follow_up_on: date | None = None,
        job: Job | None = None,
    ) -> OutreachInteraction:
        """Record that the *user* sent this. The system never sets this itself.

        A sent message with no reminder is a message that gets forgotten, so
        this is also where the follow-up is scheduled — ``follow_up_on`` when
        the user picked a date, ``OUTREACH_FOLLOW_UP_DAYS`` out otherwise. The
        reminder is a reminder: it prompts the user, and sends nothing.
        """
        interaction.sent_by_user = True
        interaction.sent_at = when or utcnow()
        if channel:
            interaction.channel = channel.upper()

        contact = self.session.get(Contact, interaction.contact_id)
        if contact is not None:
            if contact.status in {
                ContactStatus.IDENTIFIED.value,
                ContactStatus.OUTREACH_READY.value,
            }:
                self.set_outreach_state(
                    contact, ContactStatus.OUTREACH_SENT, when=interaction.sent_at
                )
            else:
                contact.last_contact_at = interaction.sent_at
            if notes:
                contact.notes = _append_note(contact.notes, notes)

            due = follow_up_on or (
                interaction.sent_at.date() + timedelta(days=OUTREACH_FOLLOW_UP_DAYS)
            )
            contact.next_follow_up = due
            linked_job = job or (
                self.session.get(Job, interaction.job_id) if interaction.job_id else None
            )
            self.create_follow_up(
                subject=f"Follow up with {contact.name}"
                + (f" about {linked_job.title}" if linked_job else ""),
                due_date=due,
                kind=FollowUpKind.NETWORKING_OUTREACH,
                job=linked_job,
                contact=contact,
                notes=notes,
            )
        self.session.flush()
        return interaction

    # -- follow-ups ------------------------------------------------------
    def create_follow_up(
        self,
        *,
        subject: str,
        due_date: date,
        kind: FollowUpKind | str = FollowUpKind.OTHER,
        priority: FollowUpPriority | str = FollowUpPriority.NORMAL,
        job: Job | None = None,
        contact: Contact | None = None,
        notes: str | None = None,
        is_demo: bool | None = None,
    ) -> FollowUp:
        text = (subject or "").strip()
        if not text:
            raise ValueError("A follow-up needs a subject.")
        # A follow-up inherits demo status from whatever it hangs off, so
        # verification runs cannot leak into the real Today panel.
        inherited = bool((job and job.is_demo) or (contact and contact.is_demo))
        follow_up = FollowUp(
            subject=text,
            due_date=due_date,
            kind=FollowUpKind(kind).value,
            priority=FollowUpPriority(priority).value,
            job_id=job.job_id if job else None,
            contact_id=contact.contact_id if contact else None,
            notes=(notes or "").strip() or None,
            is_demo=inherited if is_demo is None else is_demo,
        )
        self.session.add(follow_up)
        self.session.flush()
        return follow_up

    def complete_follow_up(self, follow_up: FollowUp) -> FollowUp:
        follow_up.completed = True
        follow_up.completed_at = utcnow()
        self.session.flush()
        return follow_up

    def due_follow_ups(
        self,
        *,
        within_days: int = 3,
        include_overdue: bool = True,
        include_demo: bool = False,
    ) -> list[FollowUp]:
        horizon = date.today() + timedelta(days=within_days)
        stmt = select(FollowUp).where(FollowUp.completed.is_(False), FollowUp.due_date <= horizon)
        if not include_demo:
            stmt = stmt.where(FollowUp.is_demo.is_(False))
        if not include_overdue:
            stmt = stmt.where(FollowUp.due_date >= date.today())
        rows = list(self.session.execute(stmt.order_by(FollowUp.due_date)).scalars().all())
        return sorted(rows, key=lambda f: (_priority_rank(f.priority), f.due_date))

    # -- referrals -------------------------------------------------------
    def request_referral(
        self,
        *,
        job: Job,
        contact: Contact,
        state: ReferralState | str = ReferralState.REQUESTED,
        notes: str | None = None,
    ) -> ReferralOutcome:
        """Record a referral ask. Duplicates warn; they are never blocked."""
        resolved = ReferralState(state)
        warnings = self._referral_warnings(job, contact, resolved)

        referral = self.session.execute(
            select(Referral).where(
                Referral.job_id == job.job_id, Referral.contact_id == contact.contact_id
            )
        ).scalar_one_or_none()

        created = referral is None
        if referral is None:
            referral = Referral(
                job_id=job.job_id,
                contact_id=contact.contact_id,
                company_name=job.company_name,
                is_demo=job.is_demo or contact.is_demo,
            )
            self.session.add(referral)

        referral.status = resolved.value
        if notes:
            referral.notes = notes
        if resolved == ReferralState.REQUESTED and referral.requested_at is None:
            referral.requested_at = utcnow()
        if resolved in {
            ReferralState.RECEIVED,
            ReferralState.DECLINED,
            ReferralState.SUBMITTED_WITH_REFERRAL,
        }:
            referral.resolved_at = utcnow()

        if resolved == ReferralState.REQUESTED:
            self.set_outreach_state(contact, ContactStatus.REFERRAL_REQUESTED)
        elif resolved in {ReferralState.RECEIVED, ReferralState.SUBMITTED_WITH_REFERRAL}:
            self.set_outreach_state(contact, ContactStatus.REFERRED)
        elif resolved == ReferralState.DECLINED:
            self.set_outreach_state(contact, ContactStatus.DECLINED)

        self.session.flush()
        return ReferralOutcome(referral=referral, created=created, warnings=warnings)

    def _referral_warnings(
        self, job: Job, contact: Contact, state: ReferralState
    ) -> list[ContactWarning]:
        warnings: list[ContactWarning] = []
        if state not in {ReferralState.REQUESTED, ReferralState.POTENTIAL}:
            return warnings

        open_on_job = [
            r
            for r in self.session.execute(
                select(Referral).where(Referral.job_id == job.job_id)
            ).scalars()
            if r.contact_id != contact.contact_id and ReferralState(r.status) in _REFERRAL_OPEN
        ]
        if open_on_job:
            names = ", ".join(
                filter(
                    None,
                    (
                        getattr(self.session.get(Contact, r.contact_id), "name", None)
                        for r in open_on_job
                    ),
                )
            )
            warnings.append(
                ContactWarning(
                    code="DUPLICATE_ASK_SAME_JOB",
                    message=(
                        "There is already an open referral ask for this job"
                        + (f" with {names}" if names else "")
                        + ". Two asks at the same company can read as scattershot — "
                        "proceed if that is deliberate."
                    ),
                )
            )

        same_company_open = [
            r
            for r in self.session.execute(
                select(Referral).where(
                    Referral.company_name == job.company_name,
                    Referral.job_id != job.job_id,
                )
            ).scalars()
            if ReferralState(r.status) in _REFERRAL_OPEN
        ]
        if same_company_open:
            warnings.append(
                ContactWarning(
                    code="OPEN_ASK_SAME_COMPANY",
                    message=(
                        f"An open referral ask already exists at {job.company_name} for a "
                        "different role."
                    ),
                )
            )

        last_contact = as_utc(contact.last_contact_at)
        if ContactStatus(contact.status) in _ASK_OUTSTANDING and last_contact:
            days = (utcnow() - last_contact).days
            if days < 3:
                warnings.append(
                    ContactWarning(
                        code="RECENT_CONTACT",
                        message=(
                            f"{contact.name} was contacted {days} day(s) ago. Consider waiting "
                            "before asking again."
                        ),
                    )
                )
        return warnings

    def unlink_from_job(self, contact: Contact, job: Job) -> bool:
        """Remove one job/contact link. The contact and its history survive.

        One person can matter for several roles, so a link is the thing being
        removed here — never the relationship.
        """
        link = self.session.execute(
            select(JobContact).where(
                JobContact.job_id == job.job_id, JobContact.contact_id == contact.contact_id
            )
        ).scalar_one_or_none()
        if link is None:
            return False
        self.session.delete(link)
        self.session.flush()
        return True

    def linked_contacts(self, job: Job) -> list[Contact]:
        """Contacts explicitly linked to this job."""
        contact_ids = [
            row.contact_id
            for row in self.session.execute(
                select(JobContact).where(JobContact.job_id == job.job_id)
            ).scalars()
        ]
        if not contact_ids:
            return []
        return list(
            self.session.execute(select(Contact).where(Contact.contact_id.in_(contact_ids)))
            .scalars()
            .all()
        )

    def same_company_contacts(self, job: Job) -> list[Contact]:
        """People already known at this employer, excluding ones already linked.

        Someone recorded for one role at a company is a plausible path into another
        role — but they are a SUGGESTION to link, not a link, so they are kept
        separate from :meth:`linked_contacts` rather than blended into it.
        """
        linked = {c.contact_id for c in self.linked_contacts(job)}
        rows = self.session.execute(
            select(Contact).where(Contact.company_name.is_not(None), Contact.is_demo.is_(False))
        ).scalars()
        return [
            c
            for c in rows
            if c.contact_id not in linked
            and _same_company(c.company_name or "", job.company_name or "")
        ]

    def record_reply(
        self, contact: Contact, *, when: datetime | None = None, notes: str | None = None
    ) -> Contact:
        """A human heard back. Recorded, never inferred from silence."""
        moment = when or utcnow()
        contact.status = ContactStatus.REPLIED.value
        contact.last_contact_at = moment
        if notes:
            contact.notes = _append_note(contact.notes, notes)

        # Close the loop on the outstanding draft, so a reply stops the
        # follow-up clock rather than leaving it ticking.
        latest = self.session.execute(
            select(OutreachInteraction)
            .where(
                OutreachInteraction.contact_id == contact.contact_id,
                OutreachInteraction.sent_by_user.is_(True),
                OutreachInteraction.reply_received_at.is_(None),
            )
            .order_by(OutreachInteraction.created_at.desc())
        ).scalars().first()
        if latest is not None:
            latest.reply_received_at = moment

        for follow_up in self._open_follow_ups(contact):
            follow_up.completed = True
            follow_up.completed_at = moment
            follow_up.notes = (follow_up.notes or "") + " Closed by a reply."
        self.session.flush()
        return contact

    def close_contact(self, contact: Contact, *, notes: str | None = None) -> Contact:
        """Deliberately done with this person, as distinct from being ignored."""
        contact.status = ContactStatus.CLOSED.value
        if notes:
            contact.notes = _append_note(contact.notes, notes)
        for follow_up in self._open_follow_ups(contact):
            follow_up.completed = True
            follow_up.completed_at = utcnow()
        self.session.flush()
        return contact

    def snooze_follow_up(self, follow_up: FollowUp, *, days: int = 7) -> FollowUp:
        """Push a reminder out. It stays open — snoozing is not completing."""
        base = max(follow_up.due_date, date.today())
        follow_up.due_date = base + timedelta(days=max(1, days))
        self.session.flush()
        return follow_up

    def _open_follow_ups(self, contact: Contact) -> list[FollowUp]:
        return list(
            self.session.execute(
                select(FollowUp).where(
                    FollowUp.contact_id == contact.contact_id, FollowUp.completed.is_(False)
                )
            )
            .scalars()
            .all()
        )

    # -- the People page -------------------------------------------------
    def summary(self, *, include_demo: bool = False) -> dict[str, Any]:
        """KPIs for the People page, counted from records rather than declared."""
        stmt = select(Contact)
        if not include_demo:
            stmt = stmt.where(Contact.is_demo.is_(False))
        contacts = list(self.session.execute(stmt).scalars().all())

        needs_outreach = [
            c
            for c in contacts
            if c.status in (ContactStatus.IDENTIFIED.value, ContactStatus.OUTREACH_READY.value)
        ]
        awaiting = [
            c
            for c in contacts
            if c.status
            in (ContactStatus.OUTREACH_SENT.value, ContactStatus.REFERRAL_REQUESTED.value)
        ]
        due = self.due_follow_ups(within_days=7, include_demo=include_demo)

        referral_stmt = select(Referral)
        if not include_demo:
            referral_stmt = referral_stmt.where(Referral.is_demo.is_(False))
        referrals = list(self.session.execute(referral_stmt).scalars().all())
        live_referrals = [
            r
            for r in referrals
            if ReferralState(r.status)
            in {
                ReferralState.REQUESTED,
                ReferralState.RECEIVED,
                ReferralState.SUBMITTED_WITH_REFERRAL,
            }
        ]

        return {
            "contacts": len(contacts),
            "need_outreach": len(needs_outreach),
            "awaiting_reply": len(awaiting),
            "follow_ups_due": len([f for f in due if f.due_date <= date.today()]),
            "follow_ups_next_7_days": len(due),
            "referrals": len(live_referrals),
        }

    def referrals_for_job(self, job: Job) -> list[Referral]:
        return list(
            self.session.execute(select(Referral).where(Referral.job_id == job.job_id))
            .scalars()
            .all()
        )

    def referral_state_for_job(self, job: Job) -> ReferralState:
        """The furthest-along referral state across every contact on this job."""
        rank = [
            ReferralState.SUBMITTED_WITH_REFERRAL,
            ReferralState.RECEIVED,
            ReferralState.REQUESTED,
            ReferralState.POTENTIAL,
            ReferralState.DECLINED,
            ReferralState.NO_RESPONSE,
        ]
        states = {ReferralState(r.status) for r in self.referrals_for_job(job)}
        for candidate in rank:
            if candidate in states:
                return candidate
        return ReferralState.NONE


def _append_note(existing: str | None, addition: str | None) -> str | None:
    """Append without losing what was already recorded."""
    addition = (addition or "").strip()
    if not addition:
        return existing
    if not existing:
        return addition
    return "\n".join([existing.rstrip(), addition])


def _priority_rank(priority: str | None) -> int:
    order = {
        FollowUpPriority.URGENT.value: 0,
        FollowUpPriority.HIGH.value: 1,
        FollowUpPriority.NORMAL.value: 2,
        FollowUpPriority.LOW.value: 3,
    }
    return order.get(priority or FollowUpPriority.NORMAL.value, 2)


def _same_company(left: str, right: str) -> bool:
    from app.services.text import normalize_company

    if not left or not right:
        return False
    return normalize_company(left) == normalize_company(right)
