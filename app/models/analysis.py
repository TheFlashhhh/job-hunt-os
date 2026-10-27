"""Analysis entities: requirements, evidence matches, assessments, priority.

Nothing here stores evidence *content* — only Evidence IDs plus the Evidence
Bank version used, so ``data/*.yaml`` stays authoritative (architecture §21).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id, utcnow
from app.models.enums import (
    AnalysisMode,
    AuthorizationState,
    EligibilityStatus,
    LegitimacyStatus,
    RequirementKind,
    SponsorshipCompanyContext,
    SponsorshipRoleSignal,
    SupportStatus,
)

if TYPE_CHECKING:
    from app.models.ingestion import Job


class JobAnalysis(Base, TimestampMixin):
    """One analysis pass over a job. Versioned by the hash it was computed from."""

    __tablename__ = "job_analysis"

    analysis_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("ANA")
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job.job_id", ondelete="CASCADE"), nullable=False, index=True
    )

    analyzed_hash: Mapped[str | None] = mapped_column(String(64))
    analysis_mode: Mapped[str] = mapped_column(String(32), default=AnalysisMode.SYNCHRONOUS.value)
    fast_path: Mapped[bool] = mapped_column(Boolean, default=False)

    # Parsed JD structure
    responsibilities: Mapped[list | None] = mapped_column(JSON)
    technologies: Mapped[list | None] = mapped_column(JSON)
    domain: Mapped[str | None] = mapped_column(String(200))
    education_requirement: Mapped[str | None] = mapped_column(Text)
    experience_requirement: Mapped[str | None] = mapped_column(Text)
    years_experience_min: Mapped[int | None] = mapped_column(Integer)
    years_experience_max: Mapped[int | None] = mapped_column(Integer)

    # Narrative outputs — never a hiring probability
    why_apply: Mapped[str | None] = mapped_column(Text)
    risks: Mapped[list | None] = mapped_column(JSON)
    strongest_evidence: Mapped[list | None] = mapped_column(JSON)
    missing_requirements: Mapped[list | None] = mapped_column(JSON)

    # Reproducibility
    evidence_bank_version: Mapped[str | None] = mapped_column(String(32))
    scoring_config_version: Mapped[str | None] = mapped_column(String(32))
    parser_version: Mapped[str | None] = mapped_column(String(32))
    llm_task_version: Mapped[str | None] = mapped_column(String(32))
    models_used: Mapped[dict | None] = mapped_column(JSON)
    deterministic_only: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="analyses")
    requirements: Mapped[list[JobRequirement]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )
    priority_score: Mapped[PriorityScore | None] = relationship(
        back_populates="analysis", uselist=False, cascade="all, delete-orphan"
    )
    eligibility: Mapped[EligibilityAssessment | None] = relationship(
        back_populates="analysis", uselist=False, cascade="all, delete-orphan"
    )
    sponsorship: Mapped[SponsorshipSignal | None] = relationship(
        back_populates="analysis", uselist=False, cascade="all, delete-orphan"
    )
    legitimacy: Mapped[LegitimacyAssessment | None] = relationship(
        back_populates="analysis", uselist=False, cascade="all, delete-orphan"
    )


class JobRequirement(Base):
    """A single extracted requirement or responsibility from the JD."""

    __tablename__ = "job_requirement"

    requirement_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("REQ")
    )
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analysis.analysis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default=RequirementKind.AMBIGUOUS.value)
    category: Mapped[str | None] = mapped_column(String(64))
    keywords: Mapped[list | None] = mapped_column(JSON)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    #: How the parser recovered this line: BULLET | SECTION_LINE | PROSE.
    #: Surfaced so a badly-parsed JD is visibly badly-parsed.
    extraction: Mapped[str] = mapped_column(String(16), default="BULLET")
    extraction_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    analysis: Mapped[JobAnalysis] = relationship(back_populates="requirements")
    matches: Mapped[list[EvidenceMatch]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan"
    )


class EvidenceMatch(Base):
    """Requirement → Evidence Bank mapping. Stores Evidence IDs, never content."""

    __tablename__ = "evidence_match"

    match_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("EM")
    )
    requirement_id: Mapped[str] = mapped_column(
        ForeignKey("job_requirement.requirement_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    support_status: Mapped[str] = mapped_column(String(32), default=SupportStatus.UNKNOWN.value)
    evidence_ids: Mapped[list | None] = mapped_column(JSON)
    skill_ids: Mapped[list | None] = mapped_column(JSON)
    safe_claim_ids: Mapped[list | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[str | None] = mapped_column(Text)

    #: True when the only available evidence is transferable, not direct.
    transferable_only: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True when matched evidence carries unresolved ownership.
    ownership_gated: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_bank_version: Mapped[str | None] = mapped_column(String(32))

    requirement: Mapped[JobRequirement] = relationship(back_populates="matches")


class EligibilityAssessment(Base):
    """Non-authorization eligibility. Authorization is never computed here."""

    __tablename__ = "eligibility_assessment"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ELG"))
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analysis.analysis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=EligibilityStatus.UNCERTAIN.value)
    reasons: Mapped[list | None] = mapped_column(JSON)
    checks: Mapped[dict | None] = mapped_column(JSON)

    #: Copied from candidate.yaml per lane. NEVER computed per job.
    authorization_state: Mapped[str] = mapped_column(
        String(64), default=AuthorizationState.AUTHORIZATION_REQUIRES_VERIFICATION.value
    )
    authorization_note: Mapped[str | None] = mapped_column(Text)
    authorization_wording_verbatim: Mapped[str | None] = mapped_column(Text)
    requires_isso_verification: Mapped[bool] = mapped_column(Boolean, default=True)

    analysis: Mapped[JobAnalysis] = relationship(back_populates="eligibility")


class SponsorshipSignal(Base):
    """Two independent fields. Historical never renders as 'this role sponsors'."""

    __tablename__ = "sponsorship_signal"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("SPN"))
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analysis.analysis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_signal: Mapped[str] = mapped_column(
        String(64), default=SponsorshipRoleSignal.UNKNOWN.value
    )
    role_signal_quote: Mapped[str | None] = mapped_column(Text)
    company_context: Mapped[str] = mapped_column(
        String(64), default=SponsorshipCompanyContext.NOT_CHECKED.value
    )
    company_context_note: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)

    analysis: Mapped[JobAnalysis] = relationship(back_populates="sponsorship")


class LegitimacyAssessment(Base):
    """Separate axis from fit. Never modifies priority score."""

    __tablename__ = "legitimacy_assessment"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("LEG"))
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analysis.analysis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=LegitimacyStatus.LEGITIMACY_NORMAL.value
    )
    #: Required for any non-default status. No accusation without evidence.
    evidence: Mapped[list | None] = mapped_column(JSON)
    explanation: Mapped[str | None] = mapped_column(Text)

    analysis: Mapped[JobAnalysis] = relationship(back_populates="legitimacy")


class PriorityScore(Base):
    """Explainable 100-point score. Never a hiring probability."""

    __tablename__ = "priority_score"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("PRI"))
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("job_analysis.analysis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    lane: Mapped[str] = mapped_column(String(64), nullable=False)
    total: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    band: Mapped[str] = mapped_column(String(16), default="LOW")
    dimensions: Mapped[dict | None] = mapped_column(JSON)
    max_dimensions: Mapped[dict | None] = mapped_column(JSON)
    explanation: Mapped[str | None] = mapped_column(Text)
    scoring_config_version: Mapped[str | None] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    analysis: Mapped[JobAnalysis] = relationship(back_populates="priority_score")
