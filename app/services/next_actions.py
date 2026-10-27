"""What to do next with one job.

A tracked job with no obvious next step is a job that quietly stalls. This turns
CRM state plus the job's actual risks into one concrete instruction.

Two rules keep it useful rather than decorative:

**Never emit a generic task.** "Follow up" tells the user nothing they did not
already know. Every action names the thing to do and why now — the deadline, the
missing ISSO check, the contact who has not replied, the days of silence since
submission. If there is genuinely nothing specific to say, the fallback still
names the stage rather than inventing urgency.

**Never emit an action the user cannot take here.** The system prepares, tracks
and guides; the user submits. So the action at ``READY_TO_APPLY`` is "open the
employer's form and submit", not "submit for me".

The engine reads state and risks and returns advice. It writes nothing, and it
does not transition anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.models.base import as_utc, utcnow
from app.models.crm import Application
from app.models.enums import ApplicationStatus, ContactStatus
from app.models.ingestion import Job
from app.services.search_strategy import lane_is_active

#: Days of silence after submission before chasing is worth suggesting.
QUIET_AFTER_APPLIED_DAYS = 14
#: Days before a stated deadline that it becomes the most urgent thing.
DEADLINE_WARNING_DAYS = 7

#: Opening words of a record saved from a URL that could not be read. Written
#: by ``ManualJobUrlImportService.save_url_only``; nothing else produces it.
_VERIFY_FIRST_MARKER = "VERIFY FIRST"


@dataclass
class NextAction:
    job_id: str
    status: str
    action: str
    detail: str
    urgency: str  # URGENT | SOON | NORMAL
    due: date | None = None
    #: Which tab answers this action, so the UI can link straight there.
    href: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "action": self.action,
            "detail": self.detail,
            "urgency": self.urgency,
            "due": self.due.isoformat() if self.due else None,
            "href": self.href,
        }


class NextActionService:
    """One concrete next step per job, from real state and real risks."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def for_job(self, job: Job) -> NextAction:
        application = job.application
        status = application.current_status if application else ApplicationStatus.DISCOVERED.value

        # A record saved from an unreadable URL has no description, no
        # requirements and no fit — so there is nothing to review, prepare or
        # apply to, whatever stage it is nominally in. Checked before the funnel
        # so the instruction stays honest at every state.
        if (job.description or "").startswith(_VERIFY_FIRST_MARKER):
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Verify this listing",
                detail=(
                    "Saved from a URL that could not be read automatically. Open the "
                    "link and paste the description so it can be analysed."
                ),
                urgency="SOON",
                href="?tab=overview",
            )

        # A deadline outranks the funnel: an unsubmitted application with three
        # days left is more urgent than whatever stage it is nominally in.
        deadline = self._deadline_pressure(job, status)
        if deadline is not None:
            return deadline

        builder = {
            ApplicationStatus.DISCOVERED.value: self._discovered,
            ApplicationStatus.REVIEW.value: self._review,
            ApplicationStatus.SHORTLISTED.value: self._shortlisted,
            ApplicationStatus.NETWORKING.value: self._networking,
            ApplicationStatus.READY_TO_APPLY.value: self._ready,
            ApplicationStatus.APPLIED.value: self._applied,
            ApplicationStatus.RECRUITER_SCREEN.value: self._screen,
            ApplicationStatus.OA.value: self._assessment,
            ApplicationStatus.INTERVIEW_1.value: self._interview,
            ApplicationStatus.INTERVIEW_2.value: self._interview,
            ApplicationStatus.FINAL.value: self._final,
            ApplicationStatus.OFFER.value: self._offer,
            ApplicationStatus.REJECTED.value: self._closed,
            ApplicationStatus.WITHDRAWN.value: self._closed,
            ApplicationStatus.ARCHIVED.value: self._closed,
        }.get(status)

        if builder is None:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Review this job",
                detail=f"Its state is {status}, which has no standard next step.",
                urgency="NORMAL",
                href="?tab=overview",
            )
        return builder(job, application, status)

    def for_jobs(self, jobs: list[Job]) -> list[NextAction]:
        order = {"URGENT": 0, "SOON": 1, "NORMAL": 2}
        actions = [self.for_job(job) for job in jobs]
        actions.sort(key=lambda a: (order.get(a.urgency, 3), a.due or date.max))
        return actions

    # -- pre-application -------------------------------------------------
    def _discovered(self, job: Job, application: Application | None, status: str) -> NextAction:
        # An archived-lane record has no active next step. Saying "review fit"
        # on a Fall 2026 internship would be asking for work on a pathway that
        # is not being pursued — D-46.
        if not lane_is_active(job.search_lane):
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Archived — no action",
                detail=(
                    "This is a Fall 2026 internship record. That lane is archived; the "
                    "record is kept for history."
                ),
                urgency="NONE",
                href="?tab=overview",
            )

        # A hidden fit earns a different instruction from an obvious one: the
        # question is whether the classifier read it right, and that is answered
        # on the Overview tab where the reasoning is shown.
        if job.hidden_fit and job.functional_match:
            function = (job.functional_primary or "").replace("_", " ").lower()
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Review functional match",
                detail=(
                    f"The title does not say data, but the description reads as "
                    f"{function}. Check whether that is right before spending more on it."
                ),
                urgency="NORMAL",
                href="?tab=overview",
            )

        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Review fit and eligibility",
            detail=self._fit_summary(job),
            urgency="NORMAL",
            href="?tab=fit",
        )

    def _review(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Prepare the application",
            detail=(
                f"Fit is reviewed. Build the tailored resume for "
                f"{job.role_family or 'this role'} and check what the Evidence Bank "
                f"will and will not support."
            ),
            urgency="NORMAL",
            href="?tab=resume",
        )

    def _shortlisted(self, job: Job, application: Application | None, status: str) -> NextAction:
        contacts = len(self._contacts(job))
        if contacts == 0:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Look for a referral path",
                detail=(
                    f"No contacts on file at {job.company_name}. A referral is worth "
                    f"more here than another hour of resume tuning."
                ),
                urgency="NORMAL",
                href="?tab=networking",
            )
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Check the referral path",
            detail=f"{contacts} contact(s) on file at {job.company_name}.",
            urgency="NORMAL",
            href="?tab=networking",
        )

    def _networking(self, job: Job, application: Application | None, status: str) -> NextAction:
        contacts = self._contacts(job)
        if not contacts:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Find someone to contact",
                detail=(
                    f"This job is at the networking stage with nobody recorded at "
                    f"{job.company_name}. Find People to Contact opens the searches."
                ),
                urgency="NORMAL",
                href="?tab=networking",
            )

        by_status = {c.status for c in contacts}
        overdue = [c for c in contacts if c.next_follow_up and c.next_follow_up < date.today()]
        replied = [c for c in contacts if c.status == ContactStatus.REPLIED.value]
        referred = [c for c in contacts if c.status == ContactStatus.REFERRED.value]
        drafted = [c for c in contacts if c.status == ContactStatus.OUTREACH_READY.value]
        waiting = [c for c in contacts if c.status == ContactStatus.OUTREACH_SENT.value]

        if referred:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Apply, citing the referral",
                detail=(
                    f"{referred[0].name} referred you. A referral goes stale — submit while "
                    f"it is live."
                ),
                urgency="URGENT",
                href="?tab=application",
            )
        if replied:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Review the reply",
                detail=(
                    f"{replied[0].name} answered. Decide whether a referral ask fits before "
                    f"the thread goes cold."
                ),
                urgency="SOON",
                href="?tab=networking",
            )
        if overdue:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Send the follow-up",
                detail=(
                    f"{len(overdue)} follow-up(s) due with {job.company_name}. "
                    f"Do not let the application wait on a referral that is not coming."
                ),
                urgency="SOON",
                due=min(c.next_follow_up for c in overdue if c.next_follow_up),
                href="?tab=networking",
            )
        if drafted:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Send the outreach you drafted",
                detail=(
                    f"A draft for {drafted[0].name} is ready. Copy it into LinkedIn or your "
                    f"email client — this system never sends."
                ),
                urgency="NORMAL",
                href="?tab=networking",
            )
        if waiting:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Await a reply",
                detail=(
                    f"{len(waiting)} message(s) sent, reminder set. Meanwhile the application "
                    f"itself does not have to wait."
                ),
                urgency="NORMAL",
                href="?tab=networking",
            )
        if ContactStatus.REFERRAL_REQUESTED.value in by_status:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Await the referral",
                detail="A referral has been asked for and no outcome is recorded yet.",
                urgency="NORMAL",
                href="?tab=networking",
            )
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Draft outreach",
            detail=f"{len(contacts)} contact(s) recorded at {job.company_name}, none contacted.",
            urgency="NORMAL",
            href="?tab=networking",
        )

    def _contacts(self, job: Job) -> list[Any]:
        """Contacts linked to this job.

        Resolved through ``job_contact`` rather than a ``job.contacts``
        relationship, which does not exist — the previous
        ``getattr(job, "contacts", [])`` always returned the empty default, so
        every networking action here was unreachable.
        """
        session = self.session
        if session is None:
            return []
        from sqlalchemy import select

        from app.models.misc import Contact, JobContact

        contact_ids = [
            row.contact_id
            for row in session.execute(
                select(JobContact).where(JobContact.job_id == job.job_id)
            ).scalars()
        ]
        if not contact_ids:
            return []
        return list(
            session.execute(select(Contact).where(Contact.contact_id.in_(contact_ids)))
            .scalars()
            .all()
        )

    def _ready(self, job: Job, application: Application | None, status: str) -> NextAction:
        if not job.canonical_url:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Find the employer's application link",
                detail=(
                    "No canonical application URL is on file, so there is nothing "
                    "verified to submit through."
                ),
                urgency="SOON",
                href="?tab=application",
            )
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Submit the application",
            detail=(
                "Open the employer's form, submit it yourself, then record it here. "
                "Nothing is ever submitted automatically."
            ),
            urgency="SOON",
            href="?tab=application",
        )

    # -- post-application ------------------------------------------------
    def _applied(self, job: Job, application: Application | None, status: str) -> NextAction:
        days = self._days_since(application)
        if days is not None and days >= QUIET_AFTER_APPLIED_DAYS:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Chase or close this out",
                detail=(
                    f"{days} days since you applied with no recorded response. Either "
                    f"follow up through a contact or mark it closed so it stops "
                    f"occupying the pipeline."
                ),
                urgency="SOON",
                href="?tab=networking",
            )
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Track the response",
            detail=(
                f"Applied{f' {days} days ago' if days is not None else ''}. Start the "
                f"interview-prep seed now rather than the night before a screen."
            ),
            urgency="NORMAL",
            href="?tab=interview",
        )

    def _screen(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Prepare your recruiter-screen stories",
            detail=(
                "Two project stories and your work-authorization answer. The "
                "authorization question is asked on almost every screen."
            ),
            urgency="SOON",
            href="?tab=interview",
        )

    def _assessment(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Complete the online assessment",
            detail="Assessments usually expire. Check the deadline in the invite.",
            urgency="URGENT",
            href="?tab=interview",
        )

    def _interview(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Prepare for this round",
            detail=(
                f"Role-specific preparation for {job.title}: the matched requirements "
                f"and the known gaps are both on the Interview tab."
            ),
            urgency="SOON",
            href="?tab=interview",
        )

    def _final(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Final-round preparation and logistics",
            detail="Confirm the schedule, the format and who you are meeting.",
            urgency="URGENT",
            href="?tab=interview",
        )

    def _offer(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Review the offer",
            detail=(
                "Check start date, work-authorization requirements and the response "
                "deadline before replying."
            ),
            urgency="URGENT",
            href="?tab=application",
        )

    def _closed(self, job: Job, application: Application | None, status: str) -> NextAction:
        return NextAction(
            job_id=job.job_id,
            status=status,
            action="Capture anything useful, then archive",
            detail=(
                "If any feedback was given, record it — it is the only feedback this "
                "search will ever get."
            ),
            urgency="NORMAL",
            href="?tab=application",
        )

    # -- helpers ---------------------------------------------------------
    def _deadline_pressure(self, job: Job, status: str) -> NextAction | None:
        if job.deadline is None:
            return None
        if status in {
            ApplicationStatus.APPLIED.value,
            ApplicationStatus.RECRUITER_SCREEN.value,
            ApplicationStatus.OA.value,
            ApplicationStatus.INTERVIEW_1.value,
            ApplicationStatus.INTERVIEW_2.value,
            ApplicationStatus.FINAL.value,
            ApplicationStatus.OFFER.value,
            ApplicationStatus.REJECTED.value,
            ApplicationStatus.WITHDRAWN.value,
            ApplicationStatus.ARCHIVED.value,
        }:
            return None

        remaining = (job.deadline - date.today()).days
        if remaining < 0:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action="Deadline has passed",
                detail=(
                    f"The stated deadline was {job.deadline.isoformat()}. Confirm the "
                    f"posting is still open before spending more time on it."
                ),
                urgency="SOON",
                due=job.deadline,
                href="?tab=overview",
            )
        if remaining <= DEADLINE_WARNING_DAYS:
            return NextAction(
                job_id=job.job_id,
                status=status,
                action=f"Apply within {remaining} day(s)",
                detail=(
                    f"The posting states a {job.deadline.isoformat()} deadline and this "
                    f"application is still at {status}."
                ),
                urgency="URGENT",
                due=job.deadline,
                href="?tab=resume",
            )
        return None

    def _days_since(self, application: Application | None) -> int | None:
        if application is None:
            return None
        reference = as_utc(application.applied_at)
        if reference is None:
            return None
        return (utcnow() - reference).days

    def _fit_summary(self, job: Job) -> str:
        """Say what is actually uncertain, not "review this job"."""
        parts: list[str] = []
        if job.eligibility_status == "UNCERTAIN":
            parts.append("eligibility is unresolved")
        if job.authorization_state == "AUTHORIZATION_REQUIRES_VERIFICATION":
            parts.append("work authorization needs checking")
        analysis = job.latest_analysis
        score = analysis.priority_score if analysis else None
        if score is not None:
            parts.append(f"priority {score.total:.0f} ({score.band})")
        if not parts:
            parts.append("no analysis has run yet")
        return f"{job.company_name}: " + ", ".join(parts) + "."


def due_within(action: NextAction, days: int) -> bool:
    return action.due is not None and action.due <= date.today() + timedelta(days=days)
