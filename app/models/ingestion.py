"""Ingestion entities: companies, collection runs, raw staging, normalized jobs.

Raw staging is mandatory (architecture §12). Source provenance is never
destroyed by deduplication (§13): one normalized ``Job`` may carry many
``JobSourceAssociation`` rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id, utcnow
from app.models.enums import (
    AuthorizationState,
    CanonicalVerification,
    CollectionRunStatus,
    DiscoveryChannel,
    DuplicateVerdict,
    EligibilityStatus,
    EmploymentType,
    ExperienceLevel,
    JobLifecycleStatus,
    LegitimacyStatus,
    NormalizationStatus,
    PostedDateConfidence,
    RemoteStatus,
    RoleFamily,
    RolePriority,
    ScreenStatus,
    SearchLane,
    SourceCapability,
    SourceRole,
    SponsorshipCompanyContext,
    SponsorshipRoleSignal,
    WatchlistScope,
)

if TYPE_CHECKING:
    from app.models.analysis import JobAnalysis
    from app.models.crm import Application


class Company(Base, TimestampMixin):
    """Curated watchlist company (Channel A) or an employer discovered elsewhere."""

    __tablename__ = "company"

    company_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("CO")
    )
    company_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    careers_url: Mapped[str | None] = mapped_column(String(1000))
    website: Mapped[str | None] = mapped_column(String(1000))

    # ATS wiring — the board token is what Channel A connectors need.
    ats_type: Mapped[str | None] = mapped_column(String(64))
    ats_identifier: Mapped[str | None] = mapped_column(String(300))
    ats_capability: Mapped[str | None] = mapped_column(String(64))

    on_watchlist: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    watchlist_scope: Mapped[str] = mapped_column(String(32), default=WatchlistScope.BOTH.value)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    locations: Mapped[list | None] = mapped_column(JSON)
    target_role_families: Mapped[list | None] = mapped_column(JSON)

    sponsorship_company_context: Mapped[str] = mapped_column(
        String(64), default=SponsorshipCompanyContext.NOT_CHECKED.value
    )
    sponsorship_notes: Mapped[str | None] = mapped_column(Text)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    #: A board used only to prove connector behaviour. Everything it produces is
    #: excluded from real analytics, exactly like demo data.
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    #: Per-connector knobs (page bounds, detail limits) without a schema change
    #: for every new connector option.
    connector_config: Mapped[dict | None] = mapped_column(JSON)
    source_metadata: Mapped[dict | None] = mapped_column(JSON)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_status: Mapped[str | None] = mapped_column(String(64))
    last_health_detail: Mapped[str | None] = mapped_column(Text)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_collection_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    # -- company registry (Discovery Coverage V2) ---------------------------
    # NULL on a row the pipeline created only to hang jobs off: that row is an
    # employer name, not a registry entry. ``monitored_companies`` is the only
    # writer of these columns.
    registry_source: Mapped[str | None] = mapped_column(String(32), index=True)
    monitoring_status: Mapped[str | None] = mapped_column(String(32), index=True)
    source_verification: Mapped[str | None] = mapped_column(String(32))
    discovered_from_job_id: Mapped[str | None] = mapped_column(String(64))
    #: Postings the board returned on the last successful poll.
    last_jobs_seen: Mapped[int | None] = mapped_column(Integer)
    #: New jobs that poll created.
    last_new_jobs: Mapped[int | None] = mapped_column(Integer)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")


class CollectionRun(Base):
    """One invocation of a connector. Idempotent, resumable, catch-up safe."""

    __tablename__ = "collection_run"

    collection_run_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("RUN")
    )
    connector: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(200))
    discovery_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    search_profile: Mapped[str | None] = mapped_column(String(120))
    lane: Mapped[str | None] = mapped_column(String(64))

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company_id: Mapped[str | None] = mapped_column(String(64), index=True)
    company_name: Mapped[str | None] = mapped_column(String(300))
    source_capability: Mapped[str | None] = mapped_column(String(64))
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(32), default=CollectionRunStatus.PENDING.value)
    #: Records the source returned, before validation.
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    #: Records that survived normalization.
    records_valid: Mapped[int] = mapped_column(Integer, default=0)
    #: $0 for structured public sources. Non-zero only once Apify exists.
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    http_stats: Mapped[dict | None] = mapped_column(JSON)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_changed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    jobs_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    connector_version: Mapped[str | None] = mapped_column(String(32))
    triggered_by: Mapped[str] = mapped_column(String(64), default="manual")

    raw_records: Mapped[list[RawJobSourceRecord]] = relationship(back_populates="collection_run")


class RawJobSourceRecord(Base):
    """Immutable, append-only staging record. Enables reprocessing without refetch."""

    __tablename__ = "raw_job_source_record"
    __table_args__ = (
        Index("ix_raw_connector_external", "connector", "external_id"),
        Index("ix_raw_content_hash", "source_content_hash"),
    )

    raw_record_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("RAW")
    )
    collection_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("collection_run.collection_run_id", ondelete="SET NULL")
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    discovery_channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    connector: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_capability: Mapped[str] = mapped_column(
        String(64), default=SourceCapability.MANUAL.value
    )

    external_id: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    canonical_url: Mapped[str | None] = mapped_column(String(2000))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    raw_payload: Mapped[str | None] = mapped_column(Text)
    payload_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    parser_version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    normalization_status: Mapped[str] = mapped_column(
        String(32), default=NormalizationStatus.PENDING.value, index=True
    )
    normalization_error: Mapped[str | None] = mapped_column(Text)

    # Channel B provenance
    apify_actor_id: Mapped[str | None] = mapped_column(String(200))
    apify_run_id: Mapped[str | None] = mapped_column(String(200))

    sanitization_report: Mapped[dict | None] = mapped_column(JSON)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("job.job_id", ondelete="SET NULL"), index=True
    )

    collection_run: Mapped[CollectionRun | None] = relationship(back_populates="raw_records")


class Job(Base, TimestampMixin):
    """Normalized job. One per real-world opening; many source records."""

    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_lane_screen", "search_lane", "screen_status"),
        Index("ix_job_dedupe", "normalized_company", "normalized_title", "normalized_location"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("JOB"))
    company_id: Mapped[str | None] = mapped_column(
        ForeignKey("company.company_id", ondelete="SET NULL"), index=True
    )
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_company: Mapped[str] = mapped_column(String(300), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    location_raw: Mapped[str | None] = mapped_column(String(500))
    normalized_location: Mapped[str | None] = mapped_column(String(300), index=True)
    location_country: Mapped[str | None] = mapped_column(String(8))
    location_state: Mapped[str | None] = mapped_column(String(64))
    location_city: Mapped[str | None] = mapped_column(String(120))
    remote_status: Mapped[str] = mapped_column(String(32), default=RemoteStatus.UNKNOWN.value)

    employment_type: Mapped[str] = mapped_column(String(32), default=EmploymentType.UNKNOWN.value)
    search_lane: Mapped[str] = mapped_column(
        String(64), default=SearchLane.BOTH_OR_AMBIGUOUS.value, index=True
    )
    lane_override: Mapped[str | None] = mapped_column(String(64))
    role_family: Mapped[str] = mapped_column(String(64), default=RoleFamily.UNRELATED.value)
    role_priority: Mapped[str] = mapped_column(String(32), default=RolePriority.UNRELATED.value)
    experience_level: Mapped[str] = mapped_column(String(32), default=ExperienceLevel.UNKNOWN.value)

    comp_min: Mapped[float | None] = mapped_column()
    comp_max: Mapped[float | None] = mapped_column()
    comp_currency: Mapped[str | None] = mapped_column(String(8))
    comp_source: Mapped[str | None] = mapped_column(String(64))

    # Dates — posted_date and first_seen are NEVER conflated.
    posted_date: Mapped[date | None] = mapped_column(Date)
    posted_date_confidence: Mapped[str] = mapped_column(
        String(16), default=PostedDateConfidence.UNKNOWN.value
    )
    posted_date_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    disappeared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Consecutive successful polls of this job's source that did not return it.
    #: Only a *successful* poll increments this — a source outage must never
    #: look like a closed job.
    consecutive_misses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_confirmed_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    repost_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    reposted_from_job_id: Mapped[str | None] = mapped_column(String(64))
    lifecycle_status: Mapped[str] = mapped_column(String(32), default=JobLifecycleStatus.OPEN.value)

    deadline: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    weekly_hours: Mapped[int | None] = mapped_column(Integer)
    requires_enrollment: Mapped[bool | None] = mapped_column(Boolean)
    academic_credit_required: Mapped[bool | None] = mapped_column(Boolean)

    # Provenance
    canonical_url: Mapped[str | None] = mapped_column(String(2000), index=True)
    discovery_source: Mapped[str] = mapped_column(String(64), default="MANUAL_IMPORT")
    canonical_source: Mapped[str | None] = mapped_column(String(64))
    canonical_verification: Mapped[str] = mapped_column(
        String(32), default=CanonicalVerification.UNKNOWN.value
    )
    ats_platform: Mapped[str | None] = mapped_column(String(64))
    ats_org_slug: Mapped[str | None] = mapped_column(String(200))
    ats_job_id: Mapped[str | None] = mapped_column(String(200))

    # Hashes — load-bearing for cost control
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    normalized_content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    last_analyzed_hash: Mapped[str | None] = mapped_column(String(64))
    description_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)

    # Screening / eligibility / authorization / sponsorship / legitimacy
    #: Cheap deterministic prefilter verdict. Decides whether this posting ever
    #: earns expensive analysis. Versioned so the filter can be re-run without
    #: forcing unrelated analysis.
    prescreen_outcome: Mapped[str | None] = mapped_column(String(32), index=True)
    prescreen_reasons: Mapped[list | None] = mapped_column(JSON)
    prescreen_detail: Mapped[str | None] = mapped_column(Text)
    prescreen_version: Mapped[str | None] = mapped_column(String(32))

    # -- functional classification (D-46) ------------------------------
    # What the job actually DOES, as opposed to what HR called it. Scalar
    # columns are the ones the Jobs page filters on; the full profile —
    # capabilities, negative signals, reasons — lives in the JSON blob so
    # adding a field to the classifier is not a migration.
    functional_primary: Mapped[str | None] = mapped_column(String(64), index=True)
    functional_match: Mapped[bool | None] = mapped_column(Boolean, index=True)
    #: Unusual title, relevant function. NOT the same as a reach — that is
    #: `experience_band`, and a job can be both or neither.
    hidden_fit: Mapped[bool | None] = mapped_column(Boolean, index=True)
    #: ENTRY_COMPATIBLE | EARLY_CAREER | REACH | OVERLEVELLED | EXPERIENCE_UNSTATED
    experience_band: Mapped[str | None] = mapped_column(String(32), index=True)
    start_fit: Mapped[str | None] = mapped_column(String(32))
    #: Would this appear in the active search? Function AND experience band AND
    #: US AND not an internship AND an active lane. Stored rather than derived
    #: in every query, because the Jobs page, the dashboard and the next-action
    #: engine all need the same answer and must not each compute their own.
    active_target: Mapped[bool | None] = mapped_column(Boolean, index=True)
    #: PRIMARY_TARGET | SECONDARY_TARGET | DEPRIORITIZED (D-49). A second axis,
    #: not a second lane: both target tiers are active and both appear in the
    #: feed, with primary ranked ahead at similar fit.
    target_priority: Mapped[str | None] = mapped_column(String(32), index=True)
    functional_profile: Mapped[dict | None] = mapped_column(JSON)
    #: Which rules produced the above, so a reclassification is comparable.
    functional_classifier_version: Mapped[str | None] = mapped_column(String(32), index=True)

    screen_status: Mapped[str] = mapped_column(String(32), default=ScreenStatus.PASSED.value)
    screen_reason: Mapped[str | None] = mapped_column(Text)
    eligibility_status: Mapped[str] = mapped_column(
        String(32), default=EligibilityStatus.UNCERTAIN.value
    )
    authorization_state: Mapped[str] = mapped_column(
        String(64), default=AuthorizationState.AUTHORIZATION_REQUIRES_VERIFICATION.value
    )
    authorization_wording_verbatim: Mapped[str | None] = mapped_column(Text)
    sponsorship_role_signal: Mapped[str] = mapped_column(
        String(64), default=SponsorshipRoleSignal.UNKNOWN.value
    )
    sponsorship_quote: Mapped[str | None] = mapped_column(Text)
    legitimacy_status: Mapped[str] = mapped_column(
        String(32), default=LegitimacyStatus.LEGITIMACY_NORMAL.value
    )

    # Reproducibility (architecture §X)
    parser_version: Mapped[str | None] = mapped_column(String(32))
    llm_task_version: Mapped[str | None] = mapped_column(String(32))
    evidence_bank_version: Mapped[str | None] = mapped_column(String(32))
    scoring_config_version: Mapped[str | None] = mapped_column(String(32))

    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    user_notes: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    sources: Mapped[list[JobSourceAssociation]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    versions: Mapped[list[JobVersion]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    analyses: Mapped[list[JobAnalysis]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    application: Mapped[Application | None] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def latest_analysis(self) -> JobAnalysis | None:
        if not self.analyses:
            return None
        return max(self.analyses, key=lambda a: a.created_at)


class JobSourceAssociation(Base):
    """Preserves every source a job was seen through. Never dropped on merge."""

    __tablename__ = "job_source_association"
    __table_args__ = (Index("ix_jsa_source_external", "connector", "source_external_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("JSA"))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job.job_id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_record_id: Mapped[str | None] = mapped_column(String(64))
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    discovery_channel: Mapped[str] = mapped_column(
        String(32), default=DiscoveryChannel.C_MANUAL.value
    )
    source_external_id: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    role: Mapped[str] = mapped_column(String(32), default=SourceRole.DISCOVERY.value)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    posted_date_claimed: Mapped[date | None] = mapped_column(Date)
    posted_date_confidence: Mapped[str] = mapped_column(
        String(16), default=PostedDateConfidence.UNKNOWN.value
    )

    job: Mapped[Job] = relationship(back_populates="sources")


class JobVersion(Base):
    """Snapshot of the fields that matter when a job's content changes."""

    __tablename__ = "job_version"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("JV"))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job.job_id", ondelete="CASCADE"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    normalized_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_fields: Mapped[list | None] = mapped_column(JSON)
    change_categories: Mapped[list | None] = mapped_column(JSON)
    snapshot: Mapped[dict | None] = mapped_column(JSON)

    job: Mapped[Job] = relationship(back_populates="versions")


class DedupeDecision(Base):
    """User verdicts are permanent and outrank future automatic evaluation."""

    __tablename__ = "dedupe_decision"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("DD"))
    pair_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    job_id_a: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id_b: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), default=DuplicateVerdict.DISTINCT.value)
    decided_by: Mapped[str] = mapped_column(String(16), default="SYSTEM")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confidence: Mapped[float | None] = mapped_column()
    signals: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
