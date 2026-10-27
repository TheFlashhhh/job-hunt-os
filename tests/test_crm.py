"""Application lifecycle, status history, stale detection."""

from __future__ import annotations

from datetime import date, timedelta

from app.models.base import utcnow
from app.models.crm import ApplicationStatusHistory
from app.models.enums import ApplicationStatus, ReferralState
from app.services.crm import ApplicationService
from app.services.pipeline import JobPipeline, RawJobInput

#: Ingestion itself writes a DISCOVERED history row — discovery is an event in
#: the funnel, not a pre-state. Tests count transitions on top of that.
INITIAL_ROWS = 1


def _later(application):
    """History rows after the automatic DISCOVERED entry."""
    rows = sorted(application.status_history, key=lambda h: h.changed_at)
    return [r for r in rows if not (r.from_status is None and r.to_status == "DISCOVERED")]


def _job(session, sample_jd, company="Northwind Analytics"):
    return (
        JobPipeline(session)
        .ingest(
            RawJobInput(
                company_name=company,
                title="Data Scientist, Product",
                description=sample_jd,
                location="Boston, MA",
            )
        )
        .job
    )


# ---------------------------------------------------------------------------
# Transitions and history
# ---------------------------------------------------------------------------


def test_transition_records_history(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)

    result = service.transition(job, ApplicationStatus.SHORTLISTED, note="Strong Boston fit")

    assert result.changed is True
    assert result.from_status == ApplicationStatus.DISCOVERED.value
    assert result.to_status == ApplicationStatus.SHORTLISTED.value
    assert job.application.current_status == ApplicationStatus.SHORTLISTED.value

    history = _later(job.application)
    assert len(history) == 1
    assert history[0].note == "Strong Boston fit"


def test_history_is_append_only_across_many_moves(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    for status in (
        ApplicationStatus.REVIEW,
        ApplicationStatus.SHORTLISTED,
        ApplicationStatus.NETWORKING,
        ApplicationStatus.READY_TO_APPLY,
        ApplicationStatus.APPLIED,
    ):
        service.transition(job, status)

    rows = _later(job.application)
    assert [r.to_status for r in rows] == [
        "REVIEW",
        "SHORTLISTED",
        "NETWORKING",
        "READY_TO_APPLY",
        "APPLIED",
    ]
    # Each row records where it came from, so the chain is reconstructable.
    assert rows[0].from_status == "DISCOVERED"
    assert rows[-1].from_status == "READY_TO_APPLY"


def test_rejection_does_not_erase_earlier_progress(session, sample_jd):
    """A rejected application must still show it reached interview."""
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    for status in (
        ApplicationStatus.SHORTLISTED,
        ApplicationStatus.APPLIED,
        ApplicationStatus.INTERVIEW_1,
        ApplicationStatus.REJECTED,
    ):
        service.transition(job, status)

    reached = service.stages_reached(job.application)
    assert "INTERVIEW_1" in reached
    assert "APPLIED" in reached
    assert job.application.current_status == "REJECTED"


def test_repeat_transition_is_a_no_op(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.SHORTLISTED)
    result = service.transition(job, ApplicationStatus.SHORTLISTED)

    assert result.changed is False
    assert len(_later(job.application)) == 1
    assert "Already SHORTLISTED" in result.warnings[0]


def test_applied_at_is_set_once(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.APPLIED)
    first = job.application.applied_at
    assert first is not None

    service.transition(job, ApplicationStatus.RECRUITER_SCREEN)
    service.transition(job, ApplicationStatus.APPLIED)
    assert job.application.applied_at == first


# ---------------------------------------------------------------------------
# Warnings — guidance, not enforcement
# ---------------------------------------------------------------------------


def test_skipping_stages_warns_but_succeeds(session, sample_jd):
    """A referral can legitimately jump straight to APPLIED."""
    job = _job(session, sample_jd)
    result = ApplicationService(session).transition(job, ApplicationStatus.APPLIED)

    assert result.changed is True
    assert job.application.current_status == "APPLIED"
    assert result.warnings
    assert any("DISCOVERED" in w for w in result.warnings)


def test_moving_backwards_warns_but_succeeds(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.APPLIED)
    result = service.transition(job, ApplicationStatus.SHORTLISTED)

    assert result.changed is True
    assert any("backwards" in w for w in result.warnings)


def test_reopening_a_terminal_application_warns(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.WITHDRAWN)
    result = service.transition(job, ApplicationStatus.SHORTLISTED)

    assert result.changed is True
    assert any("Reopening" in w for w in result.warnings)


def test_warnings_are_stored_on_the_history_row(session, sample_jd):
    job = _job(session, sample_jd)
    ApplicationService(session).transition(job, ApplicationStatus.APPLIED)
    row = _later(job.application)[0]
    assert row.extra is not None
    assert row.extra["warnings"]


def test_timeline_exposes_warnings(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.APPLIED)
    timeline = [t for t in service.timeline(job.application) if t.to_status == "APPLIED"]
    assert len(timeline) == 1
    assert timeline[0].warnings


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def test_next_action_and_due_date(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    due = date.today() + timedelta(days=2)
    service.set_next_action(job, "Email the hiring manager", due)

    assert job.application.next_action == "Email the hiring manager"
    assert job.application.next_action_due == due


def test_terminal_status_clears_the_next_action(session, sample_jd):
    """A closed application must not keep generating overdue work."""
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.set_next_action(job, "Follow up", date.today() - timedelta(days=1))
    service.transition(job, ApplicationStatus.REJECTED)

    assert job.application.next_action is None
    assert job.application.next_action_due is None


def test_referral_state_mirrors_onto_the_application(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.set_referral(job, state=ReferralState.REQUESTED, contact_id="CON-1")
    assert job.application.referral_used is False
    assert job.application.referral_contact_id == "CON-1"

    service.set_referral(job, state=ReferralState.RECEIVED)
    assert job.application.referral_used is True


def test_notes_accumulate(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.add_note(job, "Recruiter is a BU alum")
    service.add_note(job, "Deadline moved")
    assert len(job.application.notes) == 2
    assert service.add_note(job, "   ") is None


# ---------------------------------------------------------------------------
# Stale detection and action queues
# ---------------------------------------------------------------------------


def test_stale_detection_uses_the_applied_date(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.APPLIED)
    application = job.application

    assert service.is_stale(application) is False
    application.applied_at = utcnow() - timedelta(days=30)
    session.flush()
    assert service.is_stale(application) is True


def test_a_shortlisted_job_is_not_stale(session, sample_jd):
    """Stale means 'waiting on them', not 'waiting on me'."""
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.SHORTLISTED)
    job.application.applied_at = utcnow() - timedelta(days=90)
    session.flush()
    assert service.is_stale(job.application) is False


def test_rejected_applications_are_never_stale(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.APPLIED)
    job.application.applied_at = utcnow() - timedelta(days=90)
    service.transition(job, ApplicationStatus.REJECTED)
    session.flush()
    assert service.is_stale(job.application) is False


def test_stale_applications_query(session, sample_jd, fulltime_jd):
    service = ApplicationService(session)
    stale_job = _job(session, sample_jd, company="Old Corp")
    fresh_job = _job(session, sample_jd, company="Fresh Corp")
    service.transition(stale_job, ApplicationStatus.APPLIED)
    service.transition(fresh_job, ApplicationStatus.APPLIED)
    stale_job.application.applied_at = utcnow() - timedelta(days=45)
    session.flush()

    stale = service.stale_applications()
    assert [a.job_id for a in stale] == [stale_job.job_id]


def test_needing_action_surfaces_shortlisted_jobs_with_no_plan(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.SHORTLISTED)
    session.flush()

    assert [a.job_id for a in service.needing_action()] == [job.job_id]

    service.set_next_action(job, "Draft outreach", date.today() + timedelta(days=5))
    session.flush()
    assert service.needing_action() == []


def test_needing_action_includes_overdue(session, sample_jd):
    job = _job(session, sample_jd)
    service = ApplicationService(session)
    service.transition(job, ApplicationStatus.APPLIED)
    service.set_next_action(job, "Chase recruiter", date.today() - timedelta(days=1))
    session.flush()
    assert [a.job_id for a in service.needing_action()] == [job.job_id]


def test_status_history_survives_a_direct_query(session, sample_jd):
    from sqlalchemy import select

    job = _job(session, sample_jd)
    ApplicationService(session).transition(job, ApplicationStatus.SHORTLISTED)
    session.flush()
    rows = (
        session.execute(
            select(ApplicationStatusHistory).where(
                ApplicationStatusHistory.to_status == "SHORTLISTED"
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].changed_by == "USER"
