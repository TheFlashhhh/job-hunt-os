"""Application tracking.

Two rules govern everything here.

**History is append-only.** A status is never edited in place and never
deleted. If a status was set by mistake, the correction is a new row saying so.
Funnel analytics read history, so a rewritten history is a lie about the search.

**Transitions are guided, not policed.** The lifecycle has a natural order, but
real searches skip stages — a referral can take a job straight from DISCOVERED
to APPLIED, and a recruiter can reach out before anything is submitted. So an
unusual transition produces a *warning attached to the history row*, not a
rejection. The only genuinely blocked move is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import as_utc, utcnow
from app.models.crm import Application, ApplicationNote, ApplicationStatusHistory
from app.models.enums import (
    FUNNEL_ORDER,
    TERMINAL_STATUSES,
    ApplicationStatus,
    ReferralState,
    SearchLane,
)
from app.models.ingestion import Job

#: Stages in the order a search normally moves through them.
_STAGE_INDEX: dict[str, int] = {s.value: i for i, s in enumerate(FUNNEL_ORDER)}

#: Stages that sit outside the linear funnel but are legitimate at many points.
_OFF_FUNNEL = {ApplicationStatus.NETWORKING.value, ApplicationStatus.INTERVIEW_2.value}

_TERMINAL = {s.value for s in TERMINAL_STATUSES}

#: How long an application can sit in an active post-submission state before it
#: is worth chasing. Configurable per call; this is the default.
DEFAULT_STALE_DAYS = 14

_AWAITING_REPLY = {
    ApplicationStatus.APPLIED.value,
    ApplicationStatus.RECRUITER_SCREEN.value,
    ApplicationStatus.OA.value,
    ApplicationStatus.INTERVIEW_1.value,
    ApplicationStatus.INTERVIEW_2.value,
    ApplicationStatus.FINAL.value,
}


@dataclass
class TransitionResult:
    application: Application
    changed: bool
    from_status: str | None
    to_status: str
    warnings: list[str] = field(default_factory=list)
    history_id: str | None = None


@dataclass
class TimelineEntry:
    at: datetime
    from_status: str | None
    to_status: str
    changed_by: str
    note: str | None
    warnings: list[str] = field(default_factory=list)


class ApplicationService:
    """Read and advance one job's application record."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- access ----------------------------------------------------------
    def get_or_create(self, job: Job) -> Application:
        if job.application is not None:
            return job.application
        application = Application(
            job_id=job.job_id,
            current_status=ApplicationStatus.DISCOVERED.value,
            is_demo=job.is_demo,
        )
        self.session.add(application)
        self.session.flush()
        return application

    # -- transitions -----------------------------------------------------
    def transition(
        self,
        job: Job,
        new_status: ApplicationStatus | str,
        *,
        note: str | None = None,
        changed_by: str = "USER",
        applied_at: datetime | None = None,
    ) -> TransitionResult:
        """Move the application to ``new_status``, recording history."""
        target = ApplicationStatus(new_status)
        application = self.get_or_create(job)
        previous = application.current_status

        if previous == target.value:
            return TransitionResult(
                application=application,
                changed=False,
                from_status=previous,
                to_status=target.value,
                warnings=[f"Already {target.value}; nothing recorded."],
            )

        warnings = self._transition_warnings(previous, target)

        application.current_status = target.value
        if target == ApplicationStatus.APPLIED and application.applied_at is None:
            application.applied_at = applied_at or utcnow()
        if target in TERMINAL_STATUSES and not application.outcome:
            application.outcome = target.value

        # Reaching a terminal state clears a next action that can no longer
        # happen; leaving it behind produces a permanently overdue task.
        if target in TERMINAL_STATUSES:
            application.next_action = None
            application.next_action_due = None

        history = ApplicationStatusHistory(
            application_id=application.application_id,
            from_status=previous,
            to_status=target.value,
            changed_by=changed_by,
            note=note or None,
            extra={"warnings": warnings} if warnings else None,
        )
        self.session.add(history)
        if note:
            self.session.add(
                ApplicationNote(
                    application_id=application.application_id, body=note, author=changed_by
                )
            )
        self.session.flush()

        return TransitionResult(
            application=application,
            changed=True,
            from_status=previous,
            to_status=target.value,
            warnings=warnings,
            history_id=history.id,
        )

    def _transition_warnings(self, previous: str, target: ApplicationStatus) -> list[str]:
        warnings: list[str] = []

        if previous in _TERMINAL and target.value not in _TERMINAL:
            warnings.append(
                f"Reopening from {previous}. The earlier {previous} remains in the history."
            )

        before = _STAGE_INDEX.get(previous)
        after = _STAGE_INDEX.get(target.value)
        if before is not None and after is not None:
            if after < before:
                warnings.append(
                    f"Moving backwards ({previous} → {target.value}). Recorded as entered."
                )
            elif after - before > 1 and target.value not in _OFF_FUNNEL:
                skipped = [
                    stage.value
                    for stage in FUNNEL_ORDER[before + 1 : after]
                    if stage.value not in _OFF_FUNNEL
                ]
                if skipped:
                    warnings.append("Skipped " + ", ".join(skipped) + ".")

        if target == ApplicationStatus.APPLIED and previous == ApplicationStatus.DISCOVERED.value:
            warnings.append("Applied straight from DISCOVERED — no review or shortlist recorded.")

        return warnings

    # -- fields ----------------------------------------------------------
    def set_next_action(self, job: Job, action: str | None, due: date | None = None) -> Application:
        application = self.get_or_create(job)
        application.next_action = (action or "").strip() or None
        application.next_action_due = due
        self.session.flush()
        return application

    def set_resume_version(self, job: Job, resume_version_id: str | None) -> Application:
        application = self.get_or_create(job)
        application.resume_version_id = resume_version_id
        self.session.flush()
        return application

    def set_referral(
        self,
        job: Job,
        *,
        state: ReferralState | str | None = None,
        contact_id: str | None = None,
    ) -> Application:
        """Mirror the referral summary onto the application row."""
        application = self.get_or_create(job)
        if contact_id is not None:
            application.referral_contact_id = contact_id or None
        if state is not None:
            resolved = ReferralState(state)
            application.referral_used = resolved in {
                ReferralState.RECEIVED,
                ReferralState.SUBMITTED_WITH_REFERRAL,
            }
        self.session.flush()
        return application

    def add_note(self, job: Job, body: str, *, author: str = "USER") -> ApplicationNote | None:
        text = (body or "").strip()
        if not text:
            return None
        application = self.get_or_create(job)
        note = ApplicationNote(application_id=application.application_id, body=text, author=author)
        self.session.add(note)
        self.session.flush()
        return note

    # -- reads -----------------------------------------------------------
    def timeline(self, application: Application) -> list[TimelineEntry]:
        rows = sorted(application.status_history, key=lambda h: as_utc(h.changed_at) or utcnow())
        return [
            TimelineEntry(
                at=as_utc(row.changed_at) or utcnow(),
                from_status=row.from_status,
                to_status=row.to_status,
                changed_by=row.changed_by,
                note=row.note,
                warnings=list((row.extra or {}).get("warnings") or []),
            )
            for row in rows
        ]

    def stages_reached(self, application: Application) -> set[str]:
        """Every stage this application has ever been in, including the first."""
        reached = {ApplicationStatus.DISCOVERED.value, application.current_status}
        for row in application.status_history:
            reached.add(row.to_status)
            if row.from_status:
                reached.add(row.from_status)
        return reached

    def is_stale(self, application: Application, *, stale_days: int = DEFAULT_STALE_DAYS) -> bool:
        if application.current_status not in _AWAITING_REPLY:
            return False
        reference = as_utc(application.applied_at) or self._last_change(application)
        if reference is None:
            return False
        return reference < utcnow() - timedelta(days=stale_days)

    def days_since_last_change(self, application: Application) -> int | None:
        reference = self._last_change(application) or as_utc(application.applied_at)
        if reference is None:
            return None
        return (utcnow() - reference).days

    def _last_change(self, application: Application) -> datetime | None:
        if not application.status_history:
            return as_utc(application.created_at)
        return max(as_utc(row.changed_at) or utcnow() for row in application.status_history)

    # -- collections -----------------------------------------------------
    def stale_applications(
        self, *, stale_days: int = DEFAULT_STALE_DAYS, lane: str | None = None
    ) -> list[Application]:
        stmt = (
            select(Application)
            .join(Job, Job.job_id == Application.job_id)
            .where(Application.is_demo.is_(False))
        )
        if lane and lane != "ALL":
            stmt = stmt.where(Job.search_lane == lane)
        rows = self.session.execute(stmt).scalars().all()
        return [a for a in rows if self.is_stale(a, stale_days=stale_days)]

    def needing_action(self, *, lane: str | None = None) -> list[Application]:
        """Applications with an action that is due, overdue, or undefined."""
        today = date.today()
        stmt = (
            select(Application)
            .join(Job, Job.job_id == Application.job_id)
            .where(Application.is_demo.is_(False))
        )
        if lane and lane != "ALL":
            stmt = stmt.where(Job.search_lane == lane)

        out: list[Application] = []
        for application in self.session.execute(stmt).scalars():
            if application.current_status in _TERMINAL:
                continue
            if application.next_action_due and application.next_action_due <= today:
                out.append(application)
            elif application.next_action is None and application.current_status in {
                ApplicationStatus.SHORTLISTED.value,
                ApplicationStatus.NETWORKING.value,
                ApplicationStatus.READY_TO_APPLY.value,
            }:
                # An active application with nothing planned is the thing most
                # likely to quietly stall.
                out.append(application)
        return out


def lane_of(application: Application) -> str:
    job = application.job
    return job.search_lane if job else SearchLane.NEITHER.value
