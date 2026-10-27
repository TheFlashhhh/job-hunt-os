"""Contacts, outreach, follow-ups and referrals.

The safety property under test throughout: nothing here sends anything. The
system records that a human sent a message; it never performs the send.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.base import utcnow
from app.models.enums import (
    ContactStatus,
    ContactType,
    ContactWarmth,
    FollowUpKind,
    FollowUpPriority,
    ReferralState,
)
from app.services.people import PeopleService
from app.services.pipeline import JobPipeline, RawJobInput


def _job(session, jd, company="Northwind Analytics", title="Data Scientist, Product"):
    return (
        JobPipeline(session)
        .ingest(
            RawJobInput(company_name=company, title=title, description=jd, location="Boston, MA")
        )
        .job
    )


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def test_create_contact_with_full_detail(session):
    contact = PeopleService(session).create_contact(
        name="Priya Raman",
        company_name="Northwind Analytics",
        role="Senior Data Scientist",
        profile_url="https://www.linkedin.com/in/example",
        contact_type=ContactType.BU_ALUM,
        warmth=ContactWarmth.ALUMNI,
        shared_context="BU MS Data Science, two years ahead",
        notes="Met at the fall career fair",
    )
    assert contact.contact_id.startswith("CON")
    assert contact.relationship_type == "BU_ALUM"
    assert contact.warmth == "ALUMNI"
    assert contact.status == ContactStatus.IDENTIFIED.value


def test_contact_requires_a_name(session):
    with pytest.raises(ValueError):
        PeopleService(session).create_contact(name="   ")


@pytest.mark.parametrize("contact_type", list(ContactType))
def test_every_contact_type_is_accepted(session, contact_type):
    contact = PeopleService(session).create_contact(name="X", contact_type=contact_type)
    assert contact.relationship_type == contact_type.value


@pytest.mark.parametrize("status", list(ContactStatus))
def test_every_outreach_state_is_reachable(session, status):
    service = PeopleService(session)
    contact = service.create_contact(name="X")
    service.set_outreach_state(contact, status)
    assert contact.status == status.value


def test_marking_outreach_sent_stamps_last_contact(session):
    service = PeopleService(session)
    contact = service.create_contact(name="X")
    assert contact.last_contact_at is None
    service.set_outreach_state(contact, ContactStatus.OUTREACH_SENT)
    assert contact.last_contact_at is not None


def test_unknown_contact_field_is_rejected(session):
    service = PeopleService(session)
    contact = service.create_contact(name="X")
    with pytest.raises(ValueError):
        service.update_contact(contact, salary_expectation="lots")


# ---------------------------------------------------------------------------
# Job linkage
# ---------------------------------------------------------------------------


def test_contact_links_to_a_job_without_duplicating(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    contact = service.create_contact(name="Priya", company_name="Northwind Analytics")

    first = service.link_to_job(contact, job, relevance="Team lead")
    second = service.link_to_job(contact, job, relevance="Hiring manager")
    assert first.id == second.id
    assert second.relevance == "Hiring manager"


def test_same_company_contacts_surface_for_a_job(session, sample_jd):
    """Someone recorded for one Acme role is a path into another Acme role."""
    service = PeopleService(session)
    job = _job(session, sample_jd, company="Northwind Analytics")
    service.create_contact(name="Priya", company_name="Northwind Analytics, Inc.")
    service.create_contact(name="Unrelated", company_name="Some Other Co")

    names = {c.name for c in service.contacts_for_job(job)}
    assert "Priya" in names
    assert "Unrelated" not in names


# ---------------------------------------------------------------------------
# Outreach — drafted, never sent
# ---------------------------------------------------------------------------


def test_a_draft_is_never_marked_sent_by_the_system(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    contact = service.create_contact(name="Priya")

    interaction = service.record_outreach_draft(
        contact,
        job=job,
        draft_body="Hi Priya — I'm a BU MSDS student...",
        evidence_ids=["EV-EE-01"],
    )
    assert interaction.sent_by_user is False
    assert interaction.sent_at is None
    assert interaction.evidence_ids_used == ["EV-EE-01"]


def test_only_an_explicit_user_action_marks_a_draft_sent(session):
    service = PeopleService(session)
    contact = service.create_contact(name="Priya")
    interaction = service.record_outreach_draft(contact, draft_body="Hello")

    service.mark_sent_by_user(interaction)

    assert interaction.sent_by_user is True
    assert interaction.sent_at is not None
    assert contact.status == ContactStatus.OUTREACH_SENT.value


def test_marking_sent_does_not_regress_a_later_state(session):
    service = PeopleService(session)
    contact = service.create_contact(name="Priya", status=ContactStatus.CONVERSATION)
    interaction = service.record_outreach_draft(contact, draft_body="Hello")
    service.mark_sent_by_user(interaction)
    assert contact.status == ContactStatus.CONVERSATION.value


def test_no_transport_exists_in_the_people_service():
    """Guard against a future 'just add SMTP' change."""
    import pathlib

    source = pathlib.Path("app/services/people.py").read_text(encoding="utf-8")
    for forbidden in ("smtplib", "sendmail", "requests.post", "httpx.post", "linkedin.com/api"):
        assert forbidden not in source, f"{forbidden} must not appear in the people service"


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(FollowUpKind))
def test_every_follow_up_kind_is_supported(session, kind):
    follow_up = PeopleService(session).create_follow_up(
        subject=f"Test {kind.value}", due_date=date.today(), kind=kind
    )
    assert follow_up.kind == kind.value


def test_follow_up_requires_a_subject(session):
    with pytest.raises(ValueError):
        PeopleService(session).create_follow_up(subject="  ", due_date=date.today())


def test_due_follow_ups_include_overdue_and_sort_by_priority(session):
    service = PeopleService(session)
    service.create_follow_up(
        subject="Low, due today", due_date=date.today(), priority=FollowUpPriority.LOW
    )
    service.create_follow_up(
        subject="Urgent, overdue",
        due_date=date.today() - timedelta(days=2),
        priority=FollowUpPriority.URGENT,
    )
    service.create_follow_up(subject="Far future", due_date=date.today() + timedelta(days=60))

    due = service.due_follow_ups()
    subjects = [f.subject for f in due]
    assert subjects[0] == "Urgent, overdue"
    assert "Low, due today" in subjects
    assert "Far future" not in subjects


def test_completed_follow_ups_drop_out(session):
    service = PeopleService(session)
    follow_up = service.create_follow_up(subject="Chase recruiter", due_date=date.today())
    assert service.due_follow_ups()

    service.complete_follow_up(follow_up)
    assert follow_up.completed_at is not None
    assert service.due_follow_ups() == []


def test_follow_up_can_attach_to_job_and_contact(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    contact = service.create_contact(name="Priya")
    follow_up = service.create_follow_up(
        subject="Ask about the referral",
        due_date=date.today(),
        kind=FollowUpKind.REFERRAL_REQUEST,
        job=job,
        contact=contact,
    )
    assert follow_up.job_id == job.job_id
    assert follow_up.contact_id == contact.contact_id


# ---------------------------------------------------------------------------
# Referrals — warn, never block
# ---------------------------------------------------------------------------


def test_referral_request_is_recorded(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    contact = service.create_contact(name="Priya", company_name="Northwind Analytics")

    outcome = service.request_referral(job=job, contact=contact)

    assert outcome.created is True
    assert outcome.referral.status == ReferralState.REQUESTED.value
    assert outcome.referral.requested_at is not None
    assert contact.status == ContactStatus.REFERRAL_REQUESTED.value
    assert outcome.warnings == []


def test_duplicate_ask_on_the_same_job_warns_but_proceeds(session, sample_jd):
    """The user's rule: warn, do not block."""
    service = PeopleService(session)
    job = _job(session, sample_jd)
    first = service.create_contact(name="Priya", company_name="Northwind Analytics")
    second = service.create_contact(name="Sam", company_name="Northwind Analytics")

    service.request_referral(job=job, contact=first)
    outcome = service.request_referral(job=job, contact=second)

    assert outcome.blocked is False
    assert outcome.referral.status == ReferralState.REQUESTED.value
    codes = {w.code for w in outcome.warnings}
    assert "DUPLICATE_ASK_SAME_JOB" in codes
    assert any("Priya" in w.message for w in outcome.warnings)


def test_open_ask_at_the_same_company_warns(session, sample_jd, fulltime_jd):
    service = PeopleService(session)
    job_a = _job(session, sample_jd, company="Northwind Analytics")
    job_b = _job(
        session, fulltime_jd, company="Northwind Analytics", title="Data Engineer, New Grad 2027"
    )
    priya = service.create_contact(name="Priya", company_name="Northwind Analytics")
    sam = service.create_contact(name="Sam", company_name="Northwind Analytics")

    service.request_referral(job=job_a, contact=priya)
    outcome = service.request_referral(job=job_b, contact=sam)

    assert outcome.blocked is False
    assert "OPEN_ASK_SAME_COMPANY" in {w.code for w in outcome.warnings}


def test_recently_contacted_person_warns(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    contact = service.create_contact(name="Priya", company_name="Northwind Analytics")
    service.set_outreach_state(contact, ContactStatus.OUTREACH_SENT, when=utcnow())

    outcome = service.request_referral(job=job, contact=contact)
    assert "RECENT_CONTACT" in {w.code for w in outcome.warnings}
    assert outcome.blocked is False


def test_declined_referral_updates_both_records(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    contact = service.create_contact(name="Priya", company_name="Northwind Analytics")
    service.request_referral(job=job, contact=contact)

    outcome = service.request_referral(job=job, contact=contact, state=ReferralState.DECLINED)
    assert outcome.created is False
    assert outcome.referral.status == ReferralState.DECLINED.value
    assert outcome.referral.resolved_at is not None
    assert contact.status == ContactStatus.DECLINED.value


def test_job_referral_state_reports_the_furthest_progress(session, sample_jd):
    service = PeopleService(session)
    job = _job(session, sample_jd)
    priya = service.create_contact(name="Priya", company_name="Northwind Analytics")
    sam = service.create_contact(name="Sam", company_name="Northwind Analytics")

    assert service.referral_state_for_job(job) == ReferralState.NONE

    service.request_referral(job=job, contact=priya, state=ReferralState.DECLINED)
    assert service.referral_state_for_job(job) == ReferralState.DECLINED

    service.request_referral(job=job, contact=sam, state=ReferralState.RECEIVED)
    assert service.referral_state_for_job(job) == ReferralState.RECEIVED


def test_a_declined_referral_does_not_trigger_a_duplicate_warning(session, sample_jd):
    """A closed ask is not an outstanding one."""
    service = PeopleService(session)
    job = _job(session, sample_jd)
    priya = service.create_contact(name="Priya", company_name="Northwind Analytics")
    sam = service.create_contact(name="Sam", company_name="Northwind Analytics")

    service.request_referral(job=job, contact=priya, state=ReferralState.DECLINED)
    outcome = service.request_referral(job=job, contact=sam)

    assert "DUPLICATE_ASK_SAME_JOB" not in {w.code for w in outcome.warnings}


# ---------------------------------------------------------------------------
# Demo isolation
# ---------------------------------------------------------------------------


def test_demo_records_are_isolated_from_real_counts(session, sample_jd):
    """Verification data must never contaminate real analytics.

    Found during the Slice 2 quality-gate walk: contacts, follow-ups and
    referrals created while exercising the live API were counted as real,
    because only Contact had an is_demo flag and Referral had no filter.
    """
    from app.services.dashboard import ExecutionDashboardService

    service = PeopleService(session)
    demo_job = (
        JobPipeline(session)
        .ingest(
            RawJobInput(
                company_name="Demo Co",
                title="Data Scientist, Product",
                description=sample_jd,
                location="Boston, MA",
                is_demo=True,
            )
        )
        .job
    )

    demo_contact = service.create_contact(name="Demo Person", is_demo=True)
    real_contact = service.create_contact(name="Real Person")

    service.create_follow_up(subject="Demo follow-up", due_date=date.today(), contact=demo_contact)
    service.create_follow_up(subject="Real follow-up", due_date=date.today())
    service.request_referral(job=demo_job, contact=demo_contact)
    session.flush()

    # Follow-ups inherit demo status from what they hang off.
    subjects = {f.subject for f in service.due_follow_ups()}
    assert subjects == {"Real follow-up"}
    assert len(service.due_follow_ups(include_demo=True)) == 2

    panel = ExecutionDashboardService(session).panel()
    assert panel.people_contacted["total"] == 1
    assert panel.referrals["total"] == 0
    assert real_contact.is_demo is False


def test_referral_on_a_demo_job_is_marked_demo(session, sample_jd):
    service = PeopleService(session)
    demo_job = (
        JobPipeline(session)
        .ingest(
            RawJobInput(
                company_name="Demo Co",
                title="Data Scientist, Product",
                description=sample_jd,
                location="Boston, MA",
                is_demo=True,
            )
        )
        .job
    )
    contact = service.create_contact(name="Someone")
    outcome = service.request_referral(job=demo_job, contact=contact)
    assert outcome.referral.is_demo is True
