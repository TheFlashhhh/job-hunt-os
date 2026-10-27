"""Resume, people, interview, operations and opportunity entities.

Grouped here because Slice 1 only creates the tables; Slices 2, 4 and 6 fill
them in. Keeping the schema present from the start means no destructive
migration later.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id, utcnow
from app.models.enums import (
    AnalysisMode,
    ContactStatus,
    ContactType,
    ContactWarmth,
    FollowUpKind,
    FollowUpPriority,
    LLMTier,
    OpportunityType,
    RecommendationKind,
    ReferralState,
)

# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class ResumeFamily(Base, TimestampMixin):
    __tablename__ = "resume_family"

    resume_family_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("RF")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    target_role_families: Mapped[list | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    versions: Mapped[list[ResumeVersion]] = relationship(back_populates="family")


class ResumeVersion(Base, TimestampMixin):
    __tablename__ = "resume_version"

    resume_version_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("RV")
    )
    resume_family_id: Mapped[str] = mapped_column(
        ForeignKey("resume_family.resume_family_id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    evidence_ids_used: Mapped[list | None] = mapped_column(JSON)
    safe_claim_ids_used: Mapped[list | None] = mapped_column(JSON)
    evidence_bank_version: Mapped[str | None] = mapped_column(String(32))
    #: PDF path. Set once ``ResumeDocumentService`` has actually rendered a file —
    #: a row existing does not mean a document exists yet.
    file_path: Mapped[str | None] = mapped_column(String(1000))
    docx_path: Mapped[str | None] = mapped_column(String(1000))
    notes: Mapped[str | None] = mapped_column(Text)

    family: Mapped[ResumeFamily] = relationship(back_populates="versions")


class CoverLetterVersion(Base, TimestampMixin):
    """One generated or user-edited cover letter for one job.

    Versioned rather than overwritten, for the same reason resumes are: the
    letter that got an interview is worth keeping, and a regeneration that
    silently replaced an edited draft would destroy work the user did by hand.
    ``user_edited`` is what separates "the system wrote this" from "the user wrote
    this", and only a save from the editor sets it.
    """

    __tablename__ = "cover_letter_version"

    cover_letter_version_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("CL")
    )
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    application_id: Mapped[str | None] = mapped_column(String(64), index=True)

    #: Monotonic per job, so "version 3" means something to a human.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    label: Mapped[str | None] = mapped_column(String(300))
    #: The letter itself, as plain paragraphs. Rendering adds the furniture.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    salutation: Mapped[str | None] = mapped_column(String(300))

    #: DRAFT | SAVED | SUPERSEDED | EXTERNAL
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", index=True)
    #: True once a human saved their own wording. Regeneration never clobbers
    #: one of these without being asked.
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    role_archetype: Mapped[str | None] = mapped_column(String(64))
    #: Every Evidence ID and safe claim behind a specific sentence, so a
    #: finished letter can be traced exactly like a resume bullet.
    evidence_ids_used: Mapped[list | None] = mapped_column(JSON)
    safe_claim_ids_used: Mapped[list | None] = mapped_column(JSON)
    evidence_bank_version: Mapped[str | None] = mapped_column(String(32))
    #: How this version came to exist: generator version, inputs, what was
    #: deliberately left out.
    generation_provenance: Mapped[dict | None] = mapped_column(JSON)

    file_path: Mapped[str | None] = mapped_column(String(1000))
    docx_path: Mapped[str | None] = mapped_column(String(1000))
    notes: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class ResumeRecommendation(Base):
    """Recommended tailoring changes. Every claim links to Evidence IDs."""

    __tablename__ = "resume_recommendation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("RREC"))
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resume_family_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(
        String(32), default=RecommendationKind.EMPHASIZE.value, index=True
    )
    #: Ordering hint so the highest-value advice appears first.
    rank: Mapped[int] = mapped_column(Integer, default=0)
    #: Set when the recommendation targets one parsed requirement.
    requirement_text: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence_ids: Mapped[list | None] = mapped_column(JSON)
    safe_claim_ids: Mapped[list | None] = mapped_column(JSON)
    verdict: Mapped[str | None] = mapped_column(String(32))
    #: None = undecided. The user must accept or reject each line before a
    #: document is generated from it — nothing is silently included.
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# People / networking
# ---------------------------------------------------------------------------


class Contact(Base, TimestampMixin):
    __tablename__ = "contact"

    contact_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("CON")
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(300), index=True)
    company_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(300))
    profile_url: Mapped[str | None] = mapped_column(String(1000))
    email: Mapped[str | None] = mapped_column(String(300))
    relationship_type: Mapped[str] = mapped_column(
        String(64), default=ContactType.OTHER.value, index=True
    )
    warmth: Mapped[str] = mapped_column(String(32), default=ContactWarmth.UNKNOWN.value)
    shared_context: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=ContactStatus.IDENTIFIED.value)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_follow_up: Mapped[date | None] = mapped_column(Date, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class JobContact(Base):
    __tablename__ = "job_contact"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("JC"))
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contact_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relevance: Mapped[str | None] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutreachInteraction(Base):
    """Drafts are generated; sending is always manual (architecture §17)."""

    __tablename__ = "outreach_interaction"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("OUT"))
    contact_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(64), default="MANUAL")
    direction: Mapped[str] = mapped_column(String(16), default="OUTBOUND")
    draft_body: Mapped[str | None] = mapped_column(Text)
    #: False until the user confirms they personally sent it.
    sent_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_ids_used: Mapped[list | None] = mapped_column(JSON)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Referral(Base):
    __tablename__ = "referral"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("REF"))
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contact_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    company_name: Mapped[str | None] = mapped_column(String(300), index=True)
    #: A ReferralState, not a ContactStatus: where the *referral* stands, which
    #: is a different question from where the relationship stands.
    status: Mapped[str] = mapped_column(String(32), default=ReferralState.POTENTIAL.value)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class FollowUp(Base):
    __tablename__ = "follow_up"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("FU"))
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), default=FollowUpKind.OTHER.value, index=True)
    priority: Mapped[str] = mapped_column(String(16), default=FollowUpPriority.NORMAL.value)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    contact_id: Mapped[str | None] = mapped_column(String(64), index=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Interviews
# ---------------------------------------------------------------------------


class Interview(Base):
    __tablename__ = "interview"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("INT"))
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    application_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    format: Mapped[str | None] = mapped_column(String(64))
    interviewers: Mapped[list | None] = mapped_column(JSON)
    outcome: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InterviewPrepItem(Base):
    __tablename__ = "interview_prep_item"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("PREP"))
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    role_family: Mapped[str | None] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text)
    evidence_ids: Mapped[list | None] = mapped_column(JSON)
    #: Marks talking points the Evidence Bank flags MISSING_DETAIL_FOR_INTERVIEW.
    missing_detail: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Operations / telemetry
# ---------------------------------------------------------------------------


class AIUsage(Base):
    """Real measured usage. Values are never fabricated — null when unavailable."""

    __tablename__ = "ai_usage"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("AIU"))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    task_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tier: Mapped[str] = mapped_column(String(32), default=LLMTier.DETERMINISTIC.value)
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    analysis_id: Mapped[str | None] = mapped_column(String(64))

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer)

    mode: Mapped[str] = mapped_column(String(32), default=AnalysisMode.SYNCHRONOUS.value)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="SUCCESS")
    error: Mapped[str | None] = mapped_column(Text)


class ApifyRun(Base):
    """Channel B run telemetry — feeds the 'is Apify worth keeping?' analysis."""

    __tablename__ = "apify_run"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("APR"))
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    actor_build: Mapped[str | None] = mapped_column(String(120))
    purpose: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str | None] = mapped_column(String(200))
    query_input: Mapped[dict | None] = mapped_column(JSON)
    collection_run_id: Mapped[str | None] = mapped_column(String(64), index=True)

    dataset_id: Mapped[str | None] = mapped_column(String(200))
    records_requested: Mapped[int | None] = mapped_column(Integer)
    records_returned: Mapped[int | None] = mapped_column(Integer)
    compute_units: Mapped[float | None] = mapped_column(Float)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    error: Mapped[str | None] = mapped_column(Text)


class SystemTask(Base):
    __tablename__ = "system_task"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("TASK"))
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    schedule: Mapped[str | None] = mapped_column(String(200))
    last_attempted_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="IDLE")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[dict | None] = mapped_column(JSON)


class SystemSetting(Base):
    """Mutable runtime settings. Career *policy* lives in YAML, not here."""

    __tablename__ = "system_setting"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# Career opportunities (conferences, grants, fellowships…)
# ---------------------------------------------------------------------------


class CareerOpportunity(Base, TimestampMixin):
    __tablename__ = "career_opportunity"

    opportunity_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("OPP")
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    opportunity_type: Mapped[str] = mapped_column(
        String(64), default=OpportunityType.CONFERENCE.value
    )
    organization: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(200))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    website: Mapped[str | None] = mapped_column(String(1000))
    eligibility: Mapped[str | None] = mapped_column(Text)
    application_deadline: Mapped[date | None] = mapped_column(Date, index=True)
    funding_deadline: Mapped[date | None] = mapped_column(Date)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    funding_available: Mapped[bool | None] = mapped_column(Boolean)
    volunteer_opportunity: Mapped[bool | None] = mapped_column(Boolean)
    career_value: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), default="DISCOVERED")
    notes: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
