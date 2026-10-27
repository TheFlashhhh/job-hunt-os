"""Dashboard aggregation: KPIs, funnel, Today panel, source analytics.

Demo rows are excluded from every metric so they can never contaminate real
funnel analytics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.analysis import JobAnalysis
from app.models.base import utcnow
from app.models.crm import Application
from app.models.enums import (
    FUNNEL_ORDER,
    ApplicationStatus,
    EligibilityStatus,
    ScreenStatus,
)
from app.models.ingestion import Job
from app.models.misc import FollowUp

PERIODS = {"TODAY": 1, "WEEK": 7, "MONTH": 30, "ALL": None}


@dataclass
class KpiSet:
    jobs_found: int = 0
    eligible: int = 0
    high_priority: int = 0
    shortlisted: int = 0
    applied: int = 0
    referral_applications: int = 0
    callbacks: int = 0
    online_assessments: int = 0
    interviews: int = 0
    final_rounds: int = 0
    rejections: int = 0
    offers: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


@dataclass
class TodayPanel:
    due_today: list[Job] = field(default_factory=list)
    due_soon: list[Job] = field(default_factory=list)
    high_priority_unapplied: list[Job] = field(default_factory=list)
    #: Was `fall_needs_action` until 2026-08-24. The Fall lane is archived,
    #: so the slot now carries the thing that replaced it as the reason to
    #: open the Jobs page: roles whose title hides relevant work. D-46.
    hidden_fits_to_review: list[Job] = field(default_factory=list)
    stale_applications: list[Application] = field(default_factory=list)
    authorization_questions: list[Job] = field(default_factory=list)
    sponsorship_risks: list[Job] = field(default_factory=list)
    follow_ups: list[FollowUp] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return (
            len(self.due_today)
            + len(self.due_soon)
            + len(self.high_priority_unapplied)
            + len(self.hidden_fits_to_review)
            + len(self.stale_applications)
            + len(self.follow_ups)
        )


class DashboardService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- filters ---------------------------------------------------------
    def _job_filter(self, lane: str | None, period: str | None) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [Job.is_demo.is_(False)]
        if lane and lane != "ALL":
            conditions.append(Job.search_lane == lane)
        days = PERIODS.get(period or "ALL")
        if days:
            conditions.append(Job.first_seen >= utcnow() - timedelta(days=days))
        return conditions

    # -- KPIs ------------------------------------------------------------
    def kpis(self, *, lane: str | None = None, period: str | None = "ALL") -> KpiSet:
        conditions = self._job_filter(lane, period)
        jobs = (
            self.session.execute(
                select(Job)
                .where(*conditions)
                # Both relationships the loop reads, loaded in two IN queries
                # rather than one query per job (15k+ round trips).
                .options(
                    selectinload(Job.analyses).selectinload(JobAnalysis.priority_score),
                    selectinload(Job.application),
                )
            )
            .scalars()
            .all()
        )
        kpi = KpiSet(jobs_found=len(jobs))

        for job in jobs:
            if job.eligibility_status in (
                EligibilityStatus.ELIGIBLE.value,
                EligibilityStatus.LIKELY_ELIGIBLE.value,
            ):
                kpi.eligible += 1
            analysis = job.latest_analysis
            if analysis and analysis.priority_score and analysis.priority_score.band == "HIGH":
                kpi.high_priority += 1

            app = job.application
            if app is None:
                continue
            status = app.current_status
            if status == ApplicationStatus.SHORTLISTED.value:
                kpi.shortlisted += 1
            if app.applied_at is not None:
                kpi.applied += 1
                if app.referral_used:
                    kpi.referral_applications += 1
            if status == ApplicationStatus.RECRUITER_SCREEN.value:
                kpi.callbacks += 1
            if status == ApplicationStatus.OA.value:
                kpi.online_assessments += 1
            if status in (ApplicationStatus.INTERVIEW_1.value, ApplicationStatus.INTERVIEW_2.value):
                kpi.interviews += 1
            if status == ApplicationStatus.FINAL.value:
                kpi.final_rounds += 1
            if status == ApplicationStatus.REJECTED.value:
                kpi.rejections += 1
            if status == ApplicationStatus.OFFER.value:
                kpi.offers += 1

        return kpi

    # -- funnel ----------------------------------------------------------
    def funnel(self, *, lane: str | None = None) -> list[dict[str, Any]]:
        """Counts of applications that ever *reached* each stage."""
        from app.models.crm import ApplicationStatusHistory

        conditions: list[ColumnElement[bool]] = [Job.is_demo.is_(False)]
        if lane and lane != "ALL":
            conditions.append(Job.search_lane == lane)

        rows = self.session.execute(
            select(
                ApplicationStatusHistory.to_status,
                func.count(func.distinct(Application.application_id)),
            )
            .join(
                Application, Application.application_id == ApplicationStatusHistory.application_id
            )
            .join(Job, Job.job_id == Application.job_id)
            .where(*conditions)
            .group_by(ApplicationStatusHistory.to_status)
        ).all()
        reached = {status: count for status, count in rows}

        out: list[dict[str, Any]] = []
        previous: int | None = None
        for stage in FUNNEL_ORDER:
            count = reached.get(stage.value, 0)
            conversion = None
            if previous is not None and previous > 0:
                conversion = round(100.0 * count / previous, 1)
            out.append(
                {
                    "stage": stage.value,
                    "count": count,
                    "conversion_from_previous": conversion,
                    "denominator": previous,
                    # Below this many observations, a rate is noise.
                    "small_sample": previous is not None and previous < 10,
                }
            )
            previous = count
        return out

    # -- today panel ------------------------------------------------------
    def today_panel(self, *, horizon_days: int = 7, stale_days: int = 14) -> TodayPanel:
        today = date.today()
        panel = TodayPanel()

        open_jobs = (
            self.session.execute(
                select(Job).where(
                    Job.is_demo.is_(False),
                    Job.screen_status.in_([ScreenStatus.PASSED.value, ScreenStatus.FLAGGED.value]),
                )
            )
            .scalars()
            .all()
        )

        for job in open_jobs:
            app = job.application
            applied = app is not None and app.applied_at is not None

            if job.deadline and not applied:
                if job.deadline == today:
                    panel.due_today.append(job)
                elif today < job.deadline <= today + timedelta(days=horizon_days):
                    panel.due_soon.append(job)

            analysis = job.latest_analysis
            score = analysis.priority_score if analysis else None
            if score and score.band == "HIGH" and not applied:
                panel.high_priority_unapplied.append(job)

            # A hidden fit is only worth surfacing while it is still
            # actionable: unapplied, open, and reachable on experience.
            if (
                job.hidden_fit
                and not applied
                and job.experience_band != "OVERLEVELLED"
                and job.eligibility_status != EligibilityStatus.INELIGIBLE.value
            ):
                panel.hidden_fits_to_review.append(job)

            if job.eligibility_status == EligibilityStatus.UNCERTAIN.value:
                panel.authorization_questions.append(job)

            if job.sponsorship_role_signal in (
                "NO_FUTURE_SPONSORSHIP",
                "NO_SPONSORSHIP_THIS_ROLE",
                "PERMANENT_AUTHORIZATION_REQUIRED",
                "CITIZENSHIP_REQUIRED",
            ):
                panel.sponsorship_risks.append(job)

        cutoff = utcnow() - timedelta(days=stale_days)
        panel.stale_applications = list(
            self.session.execute(
                select(Application)
                .join(Job, Job.job_id == Application.job_id)
                .where(
                    Application.is_demo.is_(False),
                    Application.applied_at.is_not(None),
                    Application.applied_at < cutoff,
                    Application.current_status.in_(
                        [
                            ApplicationStatus.APPLIED.value,
                            ApplicationStatus.RECRUITER_SCREEN.value,
                        ]
                    ),
                )
            )
            .scalars()
            .all()
        )

        panel.follow_ups = list(
            self.session.execute(
                select(FollowUp)
                .where(
                    FollowUp.completed.is_(False), FollowUp.due_date <= today + timedelta(days=3)
                )
                .order_by(FollowUp.due_date)
            )
            .scalars()
            .all()
        )

        panel.due_today.sort(key=lambda j: j.deadline or today)
        panel.due_soon.sort(key=lambda j: j.deadline or today)
        panel.high_priority_unapplied.sort(key=_score_key, reverse=True)
        panel.hidden_fits_to_review.sort(key=_score_key, reverse=True)
        panel.hidden_fits_to_review = panel.hidden_fits_to_review[:12]
        return panel

    # -- source analytics --------------------------------------------------
    def source_performance(self) -> list[dict[str, Any]]:
        """Per-source counts. Measures whether each channel earns its place."""
        jobs = (
            self.session.execute(
                select(Job)
                .where(Job.is_demo.is_(False))
                .options(
                    selectinload(Job.analyses).selectinload(JobAnalysis.priority_score),
                    selectinload(Job.application),
                )
            )
            .scalars()
            .all()
        )
        buckets: dict[str, dict[str, Any]] = {}
        for job in jobs:
            key = job.discovery_source or "UNKNOWN"
            bucket = buckets.setdefault(
                key,
                {
                    "source": key,
                    "jobs_discovered": 0,
                    "canonical_verified": 0,
                    "high_priority": 0,
                    "shortlisted": 0,
                    "applied": 0,
                    "callbacks": 0,
                    "offers": 0,
                },
            )
            bucket["jobs_discovered"] += 1
            if job.canonical_verification == "CANONICAL_VERIFIED":
                bucket["canonical_verified"] += 1
            analysis = job.latest_analysis
            if analysis and analysis.priority_score and analysis.priority_score.band == "HIGH":
                bucket["high_priority"] += 1
            app = job.application
            if app:
                if app.current_status == ApplicationStatus.SHORTLISTED.value:
                    bucket["shortlisted"] += 1
                if app.applied_at:
                    bucket["applied"] += 1
                if app.current_status == ApplicationStatus.RECRUITER_SCREEN.value:
                    bucket["callbacks"] += 1
                if app.current_status == ApplicationStatus.OFFER.value:
                    bucket["offers"] += 1

        out = sorted(buckets.values(), key=lambda b: b["jobs_discovered"], reverse=True)
        for bucket in out:
            bucket["small_sample"] = bucket["applied"] < 30
        return out

    # -- lane split --------------------------------------------------------
    def lane_counts(self) -> dict[str, int]:
        rows = self.session.execute(
            select(Job.search_lane, func.count())
            .where(Job.is_demo.is_(False))
            .group_by(Job.search_lane)
        ).all()
        return {lane: count for lane, count in rows}


def _score_key(job: Job) -> float:
    analysis = job.latest_analysis
    if analysis and analysis.priority_score:
        return analysis.priority_score.total
    return 0.0


# ---------------------------------------------------------------------------
# Slice 2 — execution sections
# ---------------------------------------------------------------------------


@dataclass
class ExecutionPanel:
    """What the search needs from the candidate right now, grouped by kind of work.

    Deliberately separate from :class:`TodayPanel`, which answers "what is
    time-critical". This answers "where is the search stalling" — an
    application with no next action is not urgent, but it is how a search
    quietly dies.
    """

    applications_needing_action: list[dict[str, Any]] = field(default_factory=list)
    networking_follow_ups: list[dict[str, Any]] = field(default_factory=list)
    people_contacted: dict[str, int] = field(default_factory=dict)
    referrals: dict[str, int] = field(default_factory=dict)
    stale_applications: list[dict[str, Any]] = field(default_factory=list)
    resume_recommendations_pending: list[dict[str, Any]] = field(default_factory=list)
    recurring_skill_gaps: list[dict[str, Any]] = field(default_factory=list)
    #: Slice 5 dashboard — applications actually *submitted* in the last 7
    #: days (by ``applied_at``), distinct from ``kpis().applied`` (ever
    #: applied) and from `period=WEEK` on /api/dashboard (which windows by
    #: when the JOB was first seen, not when the application was submitted).
    applications_this_week: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "applications_needing_action": self.applications_needing_action,
            "networking_follow_ups": self.networking_follow_ups,
            "people_contacted": self.people_contacted,
            "referrals": self.referrals,
            "stale_applications": self.stale_applications,
            "resume_recommendations_pending": self.resume_recommendations_pending,
            "recurring_skill_gaps": self.recurring_skill_gaps,
            "applications_this_week": self.applications_this_week,
        }


class ExecutionDashboardService:
    """The Slice 2 view: not "what did I find" but "what am I doing about it"."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def panel(self, *, lane: str | None = None) -> ExecutionPanel:
        from app.services.crm import ApplicationService
        from app.services.gaps import SkillGapService
        from app.services.people import PeopleService

        applications = ApplicationService(self.session)
        people = PeopleService(self.session)

        panel = ExecutionPanel()
        panel.applications_needing_action = [
            self._application_row(a, applications) for a in applications.needing_action(lane=lane)
        ]
        panel.stale_applications = [
            self._application_row(a, applications)
            for a in applications.stale_applications(lane=lane)
        ]
        panel.networking_follow_ups = [
            self._follow_up_row(f) for f in people.due_follow_ups(within_days=7)
        ]
        panel.people_contacted = self._contact_counts()
        panel.referrals = self._referral_counts()
        panel.resume_recommendations_pending = self._recommendations_pending(lane=lane)
        panel.recurring_skill_gaps = [
            gap.as_dict() for gap in SkillGapService(self.session).baseline(lane=lane).gaps[:8]
        ]
        panel.applications_this_week = self._applications_this_week(lane=lane)
        return panel

    def _applications_this_week(self, *, lane: str | None) -> int:
        conditions: list[Any] = [
            Application.is_demo.is_(False),
            Application.applied_at.is_not(None),
            Application.applied_at >= utcnow() - timedelta(days=7),
        ]
        if lane and lane != "ALL":
            conditions.append(Job.search_lane == lane)
        return (
            self.session.execute(
                select(func.count(func.distinct(Application.application_id)))
                .join(Job, Job.job_id == Application.job_id)
                .where(*conditions)
            ).scalar()
            or 0
        )

    # -- rows ------------------------------------------------------------
    def _application_row(self, application: Application, service: Any) -> dict[str, Any]:
        job = application.job
        return {
            "job_id": application.job_id,
            "application_id": application.application_id,
            "company": job.company_name if job else "",
            "title": job.title if job else "",
            "lane": job.search_lane if job else None,
            "status": application.current_status,
            "next_action": application.next_action,
            "next_action_due": (
                application.next_action_due.isoformat() if application.next_action_due else None
            ),
            "days_since_change": service.days_since_last_change(application),
            "deadline": job.deadline.isoformat() if job and job.deadline else None,
        }

    def _follow_up_row(self, follow_up: FollowUp) -> dict[str, Any]:
        return {
            "id": follow_up.id,
            "subject": follow_up.subject,
            "kind": follow_up.kind,
            "priority": follow_up.priority,
            "due_date": follow_up.due_date.isoformat(),
            "overdue": follow_up.due_date < date.today(),
            "job_id": follow_up.job_id,
            "contact_id": follow_up.contact_id,
        }

    def _contact_counts(self) -> dict[str, int]:
        from app.models.misc import Contact

        counts: dict[str, int] = {"total": 0}
        for contact in self.session.execute(
            select(Contact).where(Contact.is_demo.is_(False))
        ).scalars():
            counts["total"] += 1
            counts[contact.status] = counts.get(contact.status, 0) + 1
        return counts

    def _referral_counts(self) -> dict[str, int]:
        from app.models.misc import Referral

        counts: dict[str, int] = {"total": 0}
        for referral in self.session.execute(
            select(Referral).where(Referral.is_demo.is_(False))
        ).scalars():
            counts["total"] += 1
            counts[referral.status] = counts.get(referral.status, 0) + 1
        return counts

    def _recommendations_pending(self, *, lane: str | None = None) -> list[dict[str, Any]]:
        """Shortlisted-or-later jobs with no resume recommendations generated.

        A shortlisted job with no tailoring advice is the most common place to
        lose time before applying.
        """
        from app.models.misc import ResumeRecommendation

        interesting = {
            ApplicationStatus.SHORTLISTED.value,
            ApplicationStatus.NETWORKING.value,
            ApplicationStatus.READY_TO_APPLY.value,
        }
        stmt = (
            select(Job)
            .join(Application, Application.job_id == Job.job_id)
            .where(Job.is_demo.is_(False), Application.current_status.in_(interesting))
        )
        if lane and lane != "ALL":
            stmt = stmt.where(Job.search_lane == lane)

        out: list[dict[str, Any]] = []
        for job in self.session.execute(stmt).scalars():
            has_any = (
                self.session.execute(
                    select(ResumeRecommendation).where(ResumeRecommendation.job_id == job.job_id)
                )
                .scalars()
                .first()
                is not None
            )
            if not has_any:
                out.append(
                    {
                        "job_id": job.job_id,
                        "company": job.company_name,
                        "title": job.title,
                        "lane": job.search_lane,
                        "status": job.application.current_status if job.application else None,
                    }
                )
        return out
