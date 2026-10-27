"""Pydantic response models for the JSON API.

These exist so the OpenAPI document describes what each endpoint actually
returns. Without them FastAPI reports `dict[str, Any]`, which
`openapi-typescript` renders as `{}` — and a frontend facing `{}` has no choice
but to hand-write the shape, which is exactly the backend/frontend drift the
generated client is supposed to prevent.

These models describe *shape only*. No threshold, weight, verdict or rule lives
here; every value is computed in the services layer and passed through. A field
added here must already exist in the service output.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class JobRow(Base):
    """One job as it appears in a list or as the header of a detail page."""

    job_id: str
    company: str
    title: str
    lane: str
    role_family: str | None = None
    role_priority: str | None = None
    location: str | None = None
    remote: str | None = None
    posted_date: str | None = None
    first_seen: str | None = None
    deadline: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    weekly_hours: int | None = None
    requires_enrollment: bool | None = None
    eligibility: str | None = None
    authorization: str | None = None
    sponsorship: str | None = None
    legitimacy: str | None = None
    screen_status: str | None = None
    priority: float
    band: str
    application_status: str
    discovery_source: str | None = None
    canonical_source: str | None = None
    url: str | None = None
    next_action: str | None = None
    employment_type: str | None = None
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    lifecycle_status: str | None = None
    #: Slice 5 — a conservative, JD-text-derived signal the priority score
    #: cannot hide. See app/services/experience_fit.py.
    experience_fit: str = "UNKNOWN"
    experience_fit_reason: str = ""
    #: True once ``last_seen``/``first_seen`` is older than the staleness
    #: threshold in app/services/quality_guards.py.
    is_stale: bool = False

    # -- functional classification (D-46) --------------------------------
    # What the JD describes, as opposed to what the title says. `role_family`
    # above is the old title-pattern verdict; the two disagreeing is
    # informative rather than a bug, so both are carried.
    functional_primary: str | None = None
    functional_match: bool = False
    #: Unusual title, relevant function. NOT the same as a reach.
    hidden_fit: bool = False
    experience_band: str | None = None
    start_fit: str | None = None
    #: Function AND band AND US AND not an internship AND an active lane.
    active_target: bool = False
    #: PRIMARY_TARGET | SECONDARY_TARGET | DEPRIORITIZED (D-49). Software
    #: engineering is a real target that ranks behind a primary data/AI role at
    #: similar fit — a second axis, not a second lane.
    target_priority: str = ""
    lane_archived: bool = False


class JobsResponse(Base):
    #: Total matching the filter, not the page size.
    count: int
    returned: int = 0
    offset: int = 0
    limit: int = 50
    #: When True, jobs that failed deterministic screening are excluded.
    relevant_only: bool = True
    #: Prefilter view: ALL | PLAUSIBLE | UNCERTAIN | IRRELEVANT. Irrelevant
    #: records stay inspectable for debugging and source analytics; they are
    #: simply not the default view.
    prefilter: str = "ALL"
    sort: str = "RECOMMENDED"
    #: Echoed so the UI can render its own filter state without re-deriving it.
    function: str | None = None
    hidden_fit: bool = False
    experience_band: str | None = None
    #: Archived lanes are excluded unless this was asked for.
    include_archived: bool = False
    target_priority: str | None = None
    jobs: list[JobRow]


class Kpis(Base):
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


class FunnelStage(Base):
    stage: str
    count: int
    conversion_from_previous: float | None = None
    denominator: int | None = None
    #: True when the denominator is too small for the rate to mean anything.
    small_sample: bool


class SourcePerformance(Base):
    source: str
    jobs_discovered: int
    canonical_verified: int
    high_priority: int
    shortlisted: int
    applied: int
    callbacks: int
    offers: int
    small_sample: bool


class TodaySummary(Base):
    due_today: list[str]
    due_soon: list[str]
    high_priority_unapplied: list[str]
    hidden_fits_to_review: list[str]
    authorization_questions: list[str]
    total_actions: int


class DashboardResponse(Base):
    kpis: Kpis
    funnel: list[FunnelStage]
    lane_counts: dict[str, int]
    sources: list[SourcePerformance]
    today: TodaySummary


# ---------------------------------------------------------------------------
# Job detail
# ---------------------------------------------------------------------------


class EligibilityView(Base):
    status: str | None = None
    authorization_state: str | None = None
    authorization_wording: str | None = None
    reasons: list[str] = []
    #: True unless an assessment explicitly cleared it. Never defaults to False.
    requires_isso_verification: bool = True


class SponsorshipView(Base):
    role_signal: str | None = None
    quote: str | None = None
    company_context: str | None = None


class PriorityView(Base):
    total: float
    band: str
    dimensions: dict[str, float]
    max_dimensions: dict[str, float]
    #: Always ends by stating this is not a probability of being hired.
    explanation: str


class RequirementView(Base):
    text: str
    kind: str
    support_status: str
    evidence_ids: list[str] = []
    confidence: float = 0.0
    explanation: str = ""
    transferable_only: bool = False
    ownership_gated: bool = False


class SourceRef(Base):
    connector: str | None = None
    channel: str | None = None
    role: str | None = None
    url: str | None = None
    first_seen: str | None = None


class ProvenanceView(Base):
    discovery_source: str | None = None
    canonical_source: str | None = None
    canonical_verification: str | None = None
    parser_version: str | None = None
    evidence_bank_version: str | None = None
    scoring_config_version: str | None = None


class GuardWarningView(Base):
    code: str
    message: str


class GuardResultView(Base):
    blocked: bool
    block_reason: str | None = None
    warnings: list[GuardWarningView] = []
    experience_fit: str = "UNKNOWN"
    experience_fit_reason: str = ""


class WhyThisMatchesView(Base):
    """The user-facing explanation of a functional match.

    Evidence of the classifier's reasoning, not of the candidate's fitness.
    Internal scoring never appears here.
    """

    available: bool = False
    primary_function: str = ""
    secondary_functions: list[str] = []
    domains: list[str] = []
    strong_signals: list[str] = []
    supporting_signals: list[str] = []
    experience_band: str = ""
    experience_label: str = ""
    experience_detail: str = ""
    employer_title: str | None = None
    title_alignment: str = ""
    hidden_fit: bool = False
    target_priority: str = ""
    specialization_fit: str = ""
    reasons: list[str] = []
    concerns: list[str] = []
    exclusion_reasons: list[str] = []
    start: str = ""
    classifier_version: str = ""
    detail: str = ""


class JobDetailResponse(Base):
    job: JobRow
    why_this_matches: WhyThisMatchesView | None = None
    description: str = ""
    eligibility: EligibilityView
    sponsorship: SponsorshipView
    legitimacy: str | None = None
    priority: PriorityView | None = None
    why_apply: str | None = None
    risks: list[str] = []
    missing_requirements: list[str] = []
    requirements: list[RequirementView] = []
    sources: list[SourceRef] = []
    provenance: ProvenanceView
    guard: GuardResultView


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------


class TransitionResponse(Base):
    job_id: str
    changed: bool
    from_status: str | None = None
    to_status: str
    #: Advisory only. The transition has already happened.
    warnings: list[str] = []


class NextActionResponse(Base):
    job_id: str
    next_action: str | None = None
    next_action_due: str | None = None


class TimelineEntryView(Base):
    """One status change.

    ``from`` is a Python keyword, so the field is declared as ``from_`` and
    aliased. ``serialization_alias`` keeps the JSON key as ``from`` — the
    frontend and the OpenAPI document both see the natural name.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    at: str
    from_: str | None = Field(default=None, alias="from")
    to: str
    by: str
    note: str | None = None
    warnings: list[str] = []


class NoteView(Base):
    body: str
    at: str
    author: str


class ApplicationResponse(Base):
    job_id: str
    application_id: str
    current_status: str
    applied_at: str | None = None
    next_action: str | None = None
    next_action_due: str | None = None
    deadline: str | None = None
    resume_version_id: str | None = None
    referral_state: str
    referral_used: bool
    is_stale: bool
    days_since_change: int | None = None
    stages_reached: list[str]
    timeline: list[TimelineEntryView]
    notes: list[NoteView]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class ContactView(Base):
    contact_id: str
    name: str
    company_name: str | None = None
    role: str | None = None
    profile_url: str | None = None
    contact_type: str
    warmth: str
    status: str
    shared_context: str | None = None
    last_contact_at: str | None = None
    next_follow_up: str | None = None
    notes: str | None = None


class ContactsResponse(Base):
    count: int
    contacts: list[ContactView]


class JobContactsResponse(Base):
    job_id: str
    count: int
    contacts: list[ContactView]


class DraftResponse(Base):
    id: str
    contact_id: str
    job_id: str | None = None
    sent_by_user: bool
    #: States plainly that storing a draft is not sending it.
    note: str


class MarkSentResponse(Base):
    id: str
    sent_by_user: bool
    sent_at: str | None = None


class FollowUpView(Base):
    id: str
    subject: str
    kind: str
    priority: str
    due_date: str
    overdue: bool
    completed: bool = False
    job_id: str | None = None
    contact_id: str | None = None
    notes: str | None = None


class FollowUpsResponse(Base):
    count: int
    follow_ups: list[FollowUpView]


class ReferralView(Base):
    id: str
    contact_id: str
    contact_name: str | None = None
    state: str
    requested_at: str | None = None
    resolved_at: str | None = None
    notes: str | None = None


class ReferralsResponse(Base):
    job_id: str
    job_state: str
    referrals: list[ReferralView]


class ReferralWarning(Base):
    code: str
    message: str


class ReferralUpsertResponse(Base):
    job_id: str
    contact_id: str
    state: str
    created: bool
    #: Always False. Referral rules warn; they never block.
    blocked: bool
    warnings: list[ReferralWarning] = []


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class RecommendationView(Base):
    #: None only for a not-yet-persisted, in-memory recommendation. Every
    #: recommendation returned by an endpoint that can be acted on has one.
    id: str | None = None
    kind: str
    body: str
    rationale: str
    evidence_ids: list[str] = []
    #: Human-readable project/experience names for `evidence_ids`, resolved from
    #: the Evidence Bank. The UI leads with these so a reviewer sees "Epidemic
    #: Engine" rather than "EV-EE-01"; the raw IDs stay available for audit.
    evidence_labels: list[str] = []
    safe_claim_ids: list[str] = []
    requirement_text: str | None = None
    verdict: str | None = None
    rank: int = 0
    #: None = undecided. Only EMPHASIZE/SAFE_REWORD kinds are ever meant to
    #: be accepted onto a document; DO_NOT_ADD/GAP/DE_EMPHASIZE are guidance.
    accepted: bool | None = None


class RecommendationsResponse(Base):
    job_id: str | None = None
    resume_base: str | None = None
    evidence_bank_version: str | None = None
    counts: dict[str, int] = {}
    cached: bool
    recommendations: list[RecommendationView]


# ---------------------------------------------------------------------------
# Slice 5 — Prepare Application / Resume Review / Generation / Download
# ---------------------------------------------------------------------------


class PrepareApplicationResponse(Base):
    job_id: str
    resume_base: str
    guard: GuardResultView
    recommendations: list[RecommendationView]


class DecisionPayload(Base):
    accepted: bool | None


class DecisionResponse(Base):
    recommendation_id: str
    accepted: bool | None


class GenerateResumeResponse(Base):
    resume_version_id: str
    job_id: str
    resume_base: str
    label: str
    accepted_count: int
    evidence_ids_used: list[str] = []
    generated_at: str
    pdf_available: bool
    docx_available: bool
    pdf_filename: str
    docx_filename: str


class ResumeVersionView(Base):
    resume_version_id: str
    resume_base: str
    label: str
    job_id: str | None = None
    created_at: str
    pdf_available: bool
    docx_available: bool


class ResumeVersionsResponse(Base):
    job_id: str
    versions: list[ResumeVersionView]


class MarkAppliedPayload(Base):
    applied_at: str | None = None
    resume_version_id: str | None = None
    notes: str | None = None
    referral_used: bool = False
    referral_contact_id: str | None = None
    confirmation_id: str | None = None


class ApplicationChecklistView(Base):
    canonical_posting_verified: bool
    eligibility_reviewed: bool
    resume_prepared: bool
    networking_checked: bool
    authorization_reviewed: bool
    #: What the posting says about a cover letter, and whether one is on file.
    #: A REQUIRED letter gates readiness; an optional one never does.
    cover_letter_status: str = "UNKNOWN"
    cover_letter_required: bool = False
    cover_letter_recorded: bool = False
    ready_to_apply: bool


class ApplicationTabResponse(Base):
    job_id: str
    checklist: ApplicationChecklistView
    canonical_url: str | None = None
    guard: GuardResultView
    cover_letter_requirement: dict[str, Any] | None = None


class InterviewPrepView(Base):
    job_id: str
    stage: str | None = None
    likely_technical_topics: list[str] = []
    likely_project_stories: list[str] = []
    evidence_to_review: list[str] = []
    known_gaps: list[str] = []
    questions_to_practice: list[str] = []


class ResumeBaseView(Base):
    name: str
    description: str | None = None
    target_role_families: list[str] = []
    active: bool
    #: Resume Library fields. The display name is what the person sees; `name`
    #: is the internal ID and never a primary label.
    display_name: str = ""
    #: Archetype labels positioned onto this base, from role_targeting.yaml.
    best_for: list[str] = []
    #: ISO timestamp of the last library generation, or null if never.
    last_generated_at: str | None = None
    pdf_filename: str = ""
    docx_filename: str = ""


class ResumeBasesResponse(Base):
    bases: list[ResumeBaseView]


# ---------------------------------------------------------------------------
# Skill gaps and execution
# ---------------------------------------------------------------------------


class RepresentativeJob(Base):
    job_id: str
    company: str
    title: str


class SkillGapView(Base):
    skill: str
    job_count: int
    weighted_job_count: float
    required_count: int
    preferred_count: int
    mostly: str
    role_families: dict[str, int]
    lanes: dict[str, int]
    evidence_level: str
    evidence_strength: float
    representative_jobs: list[RepresentativeJob]


class SkillGapsResponse(Base):
    jobs_considered: int
    small_sample: bool
    #: States the population explicitly — SHORTLISTED and APPLIED only.
    population: str
    #: States that no learning tasks are created from this.
    note: str
    gaps: list[SkillGapView]


class ActionRow(Base):
    job_id: str
    application_id: str
    company: str
    title: str
    lane: str | None = None
    status: str
    next_action: str | None = None
    next_action_due: str | None = None
    days_since_change: int | None = None
    deadline: str | None = None


class PendingRecommendationRow(Base):
    job_id: str
    company: str
    title: str
    lane: str
    status: str | None = None


class ExecutionResponse(Base):
    applications_needing_action: list[ActionRow]
    networking_follow_ups: list[FollowUpView]
    people_contacted: dict[str, int]
    referrals: dict[str, int]
    stale_applications: list[ActionRow]
    resume_recommendations_pending: list[PendingRecommendationRow]
    recurring_skill_gaps: list[SkillGapView]
    applications_this_week: int = 0


class EnumsResponse(Base):
    application_status: list[str]
    contact_type: list[str]
    contact_status: list[str]
    contact_warmth: list[str]
    follow_up_kind: list[str]
    follow_up_priority: list[str]
    referral_state: list[str]


# ---------------------------------------------------------------------------
# Health and claim check
# ---------------------------------------------------------------------------


class HealthResponse(Base):
    status: str
    evidence_bank_version: str
    counts: dict[str, Any]


class ClaimViolation(Base):
    model_config = ConfigDict(extra="allow")

    type: str


class ClaimCheckResponse(Base):
    model_config = ConfigDict(extra="allow")

    verdict: str
    reasons: list[str] = []
    violations: list[dict[str, Any]] = []
    matched_claim_ids: list[str] = []
    matched_evidence_ids: list[str] = []
    ownership_downgrade: str | None = None
    suggested_text: str | None = None


# ---------------------------------------------------------------------------
# Slice 3 — structured discovery
# ---------------------------------------------------------------------------


class CollectionRunView(Base):
    collection_run_id: str
    connector: str
    source: str | None = None
    company_name: str | None = None
    search_profile: str | None = None
    lane: str | None = None
    status: str
    source_capability: str | None = None
    is_fixture: bool
    started_at: str | None = None
    completed_at: str | None = None
    records_received: int
    records_valid: int
    jobs_seen: int
    jobs_new: int
    jobs_changed: int
    jobs_unchanged: int
    jobs_failed: int
    #: $0 for structured public sources.
    estimated_cost_usd: float
    connector_version: str | None = None
    error_summary: str | None = None


class CollectionRunsResponse(Base):
    count: int
    #: Fixture runs are excluded unless explicitly requested.
    includes_fixtures: bool
    runs: list[CollectionRunView]


class SourceHealthView(Base):
    reachable: bool
    #: True when the source answered but this company is not publicly exposed.
    #: A per-company plan setting, not an outage.
    capability_failure: bool
    status: str
    detail: str
    postings_visible: int | None = None
    checked_at: str | None = None


class WatchlistCompanyView(Base):
    company_name: str
    ats_type: str
    ats_identifier: str
    capability: str | None = None
    watchlist_scope: str
    tier: str = "TIER_B"
    fall_signal: str = "UNKNOWN"
    industry_category: str | None = None
    active: bool
    #: False for a watchlisted company on an unsupported ATS. Distinct from
    #: inactive: the company is still wanted, just not pollable yet.
    automated_collection_enabled: bool = False
    is_fixture: bool
    config_valid: bool | None = None
    config_problems: list[str] = []
    health: SourceHealthView | None = None
    last_successful_collection_at: str | None = None
    last_failure_at: str | None = None


class WatchlistResponse(Base):
    companies: int
    #: On the watchlist and wanted in the search universe.
    watchlist_active: int
    #: Of those, how many Channel A can actually poll today.
    automation_enabled: int
    #: On the watchlist but on an unsupported ATS — covered manually for now.
    manual_only: int
    inactive: int
    fixtures: int
    by_ats: dict[str, int]
    by_tier: dict[str, int] = {}
    problems: list[str]
    #: Set while the real watchlist is unpopulated and awaiting approval.
    awaiting_approval: bool
    watchlist_version: str | None = None
    activated_date: str | None = None
    entries: list[WatchlistCompanyView]


class SourceHealthResponse(Base):
    count: int
    sources: list[WatchlistCompanyView]


class SearchProfileView(Base):
    profile_id: str
    lane: str
    enabled: bool
    locations: list[str] = []
    remote: bool | None = None
    employment_type: list[str] = []
    title_keywords: list[str] = []
    term_keywords: list[str] = []
    exclusion_keywords: list[str] = []
    source_scope: list[str] = []
    frequency_hours: int | None = None
    max_results: int | None = None


class SearchProfilesResponse(Base):
    count: int
    profiles: list[SearchProfileView]


class DiscoveredJobView(Base):
    job_id: str
    company: str
    title: str
    lane: str
    location: str | None = None
    priority: float
    band: str
    eligibility: str | None = None
    discovery_source: str | None = None
    canonical_source: str | None = None
    lifecycle_status: str
    first_seen: str | None = None
    application_status: str


class DiscoveredJobsResponse(Base):
    count: int
    jobs: list[DiscoveredJobView]


class ConnectorInfoView(Base):
    connector_id: str
    source_type: str
    capability: str
    discovery_channel: str
    version: str
    docs_url: str
    rate_limit: dict[str, Any]


class CollectionSummaryResponse(Base):
    connectors: list[ConnectorInfoView]
    #: Sources deliberately not implemented in this slice, with the reason.
    deferred_sources: dict[str, str]
    runs_total: int
    runs_last_7_days: int
    jobs_from_automation: int
    last_run_at: str | None = None
    watchlist: WatchlistResponse


class BaseResumeEntryView(Base):
    heading: str
    subheading: str
    kind: str
    bullets: list[str]


class BaseResumeView(Base):
    """The complete base resume, before any job tailoring.

    Exposed so the UI can show what the proposed changes actually modify —
    seven changes are edits to this document, not the document itself.
    """

    base_name: str
    name: str
    location: str
    contact_line: str
    education_lines: list[str]
    entries: list[BaseResumeEntryView]
    skill_lines: list[str]
    total_bullets: int
    #: Since 2026-09-24 the preview IS the library document: built with no job
    #: through the same builder and fitted to one page by the same loop, so
    #: what this shows is exactly what the download holds.
    display_name: str = ""
    summary: str = ""
    education: list[BaseResumeEducationView] = []
    page_count: int = 1
    #: What the one-page fitter removed, if anything. Diagnostics only.
    fit_reductions: list[str] = []
    best_for: list[str] = []
    pdf_filename: str = ""
    docx_filename: str = ""


class BaseResumeEducationView(Base):
    institution: str
    degree: str
    dates: str = ""
    location: str = ""
    coursework: str = ""


class GenerateBaseResumeResponse(Base):
    """A base rendered on its own - no Job, Application or ResumeVersion."""

    base_name: str
    display_name: str
    generated_at: str
    page_count: int
    entry_count: int
    bullet_count: int
    pdf_filename: str
    docx_filename: str
    fit_reductions: list[str] = []


# ---------------------------------------------------------------------------
# Slice 5.1 — role targeting
# ---------------------------------------------------------------------------


class TargetingExplanationView(Base):
    """The "why this resume looks different" panel.

    Deliberately excludes the scoring weights and per-bullet dimensions. Those
    exist (see ``resume_targeting.BulletScore``) and are useful in tests and the
    audit, but a review screen that leads with a formula invites the user to
    tune the formula instead of reading the resume.
    """

    archetype: str
    archetype_label: str
    intensity: str
    resume_base: str
    prioritized: list[str] = []
    deemphasized: list[str] = []
    lead_projects: list[str] = []
    unsupported_jd_terms: list[str] = []
    notes: list[str] = []


class AuditFindingView(Base):
    code: str
    severity: str
    message: str
    detail: list[str] = []


class TargetingAuditView(Base):
    """Internal QA. NOT an ATS score and never presented as one."""

    role_archetype: str
    resume_base: str
    jd_requirement_coverage: float
    supported_keyword_coverage: float
    unsupported_keyword_leakage: int
    primary_competency_coverage: float
    role_family_alignment: float
    ownership_risk_count: int
    duplicate_claim_count: int
    generic_content_ratio: float
    project_relevance: float
    skills_relevance: float
    total_bullets: int
    clean: bool
    findings: list[AuditFindingView] = []
    note: str


class ResumeTargetingResponse(Base):
    job_id: str
    role_archetype: str
    archetype_label: str
    resume_base: str
    intensity: str
    business_context: str
    top_required_capabilities: list[str] = []
    experience_bar: str = ""
    targeting: TargetingExplanationView | None = None
    audit: TargetingAuditView | None = None


# ---------------------------------------------------------------------------
# Slice 5.1 — manual URL import
# ---------------------------------------------------------------------------


class ImportUrlPayload(Base):
    url: str
    #: What the user intends to do with it. Drives the post-import state.
    intent: str = "REVIEW"
    #: Paste-JD fallback, used when automatic extraction failed.
    description: str | None = None
    company_name: str | None = None
    title: str | None = None
    location: str | None = None
    #: SAVE_URL_ONLY records the link and nothing else — it must not invent
    #: fields the fetch never produced.
    save_url_only: bool = False


class AlreadyAppliedPayload(Base):
    applied_at: str
    resume_version_id: str | None = None
    external_resume: bool = False
    referral_used: bool = False
    referral_contact_id: str | None = None
    confirmation_id: str | None = None
    notes: str | None = None


class ImportJobResponse(Base):
    ok: bool
    job_id: str | None = None
    created: bool = False
    duplicate: bool = False
    #: Present when the URL resolved to a job already in the database.
    existing_job_id: str | None = None
    provider: str | None = None
    external_id: str | None = None
    canonical_url: str | None = None
    company: str | None = None
    title: str | None = None
    location: str | None = None
    description_chars: int = 0
    application_status: str | None = None
    #: Failure path. `error_code` is machine-readable so the UI can offer the
    #: right fallback rather than a generic "import failed".
    error_code: str | None = None
    error: str | None = None
    can_paste_description: bool = False
    messages: list[str] = []


class NextActionView(Base):
    job_id: str
    status: str
    action: str
    detail: str
    urgency: str
    due: str | None = None
    href: str | None = None


class NextActionsResponse(Base):
    actions: list[NextActionView]


# ---------------------------------------------------------------------------
# Collection status, freshness and the monitored company set
# ---------------------------------------------------------------------------


class ProviderStatusView(Base):
    """One ATS: implemented or not, how many boards use it, when it last worked."""

    provider: str
    implemented: bool
    monitored_companies: int
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    last_status: str | None = None
    jobs_collected: int
    note: str = ""
    #: The provider's rollout switch (collection_policy.yaml providers.<ATS>).
    enabled: bool = True
    #: Monitored boards by derived health. A board that has never run is
    #: counted as never_collected, never as healthy.
    healthy_boards: int = 0
    stale_boards: int = 0
    failing_boards: int = 0
    never_collected_boards: int = 0
    #: Jobs from this provider still OPEN (confirmed by their board).
    open_jobs: int = 0
    #: Jobs this provider first surfaced in the last 24 hours.
    new_jobs_24h: int = 0


class SweepView(Base):
    """The last GROUP of runs. One `collection_run` row is one company."""

    started_at: str | None = None
    completed_at: str | None = None
    companies: int
    jobs_new: int
    jobs_changed: int
    jobs_unchanged: int
    jobs_failed: int
    failures: list[str]
    duration_seconds: float | None = None
    triggered_by: str = ""
    closure_candidates: int


class CollectionStatusResponse(Base):
    """Everything the Collection page needs to answer "how fresh is this?"."""

    last_successful_at: str | None = None
    #: FRESH | AGING | STALE | NEVER_COLLECTED
    freshness: str
    hours_since_success: float | None = None
    interval_hours: float
    #: Populated from the operating system's scheduler, never projected from a
    #: cadence in config. A cadence is an intent.
    next_scheduled_at: str | None = None
    #: NOT_CONFIGURED | REGISTERED_NEXT_RUN_KNOWN | REGISTERED_NEXT_RUN_UNKNOWN
    #: | DISABLED | ERROR | UNSUPPORTED_PLATFORM. "Registered but the next run
    #: time could not be read" is not "not scheduled".
    schedule_source: str
    schedule: dict[str, Any]
    run_in_progress: bool
    monitored: dict[str, Any]
    providers: list[ProviderStatusView]
    latest_sweep: SweepView
    broad_discovery: dict[str, Any]
    corpus: dict[str, Any]
    cadences: dict[str, Any]
    task: dict[str, Any] | None = None


class CollectionRunStartResponse(Base):
    started: bool
    reason: str = ""
    detail: str = ""
    task: dict[str, Any] | None = None


class MonitoredCompanyView(Base):
    company_id: str | None = None
    company_name: str
    ats_type: str | None = None
    ats_identifier: str | None = None
    careers_url: str | None = None
    watchlist_scope: str = "BOTH"
    source: str
    monitored_since: str | None = None
    last_successful_collection_at: str | None = None
    jobs_collected: int = 0
    removable: bool = False


class MonitoredCompaniesResponse(Base):
    core_count: int
    discovered_count: int
    companies: list[MonitoredCompanyView]


class MonitorCompanyPayload(Base):
    company_name: str
    ats_type: str
    ats_identifier: str
    careers_url: str | None = None
    watchlist_scope: str = "BOTH"
    #: Set when the request came from a job page, purely for the audit note.
    job_id: str | None = None


class MonitorCompanyResponse(Base):
    ok: bool
    created: bool = False
    reason: str = ""
    detail: str = ""
    company: MonitoredCompanyView | None = None


class RegistryCompanyView(Base):
    """One company in the registry, seed or row, with its derived health."""

    company_id: str | None = None
    company_name: str
    ats_type: str
    ats_identifier: str | None = None
    careers_url: str | None = None
    #: CORE_SEED | DISCOVERED | USER_ADDED | IMPORTED_FROM_JOB | CURATED_EXPANSION
    registry_source: str
    #: ACTIVE | PAUSED | EXPERIMENTAL | MANUAL_ONLY | VERIFY_LATER
    monitoring_status: str
    #: CONFIRMED | LIKELY_REVIEW | UNKNOWN
    source_verification: str
    #: HEALTHY | STALE | FAILING | UNSUPPORTED | PAUSED | EXPERIMENTAL | NOT_YET_COLLECTED
    health: str
    last_successful_collection_at: str | None = None
    last_failure_at: str | None = None
    last_failure_reason: str | None = None
    last_jobs_seen: int | None = None
    last_new_jobs: int | None = None
    open_jobs: int = 0
    can_pause: bool = False
    can_resume: bool = False
    can_test: bool = False


class CoverageView(Base):
    """Coverage of the registry — the number that matters more than job count."""

    total: int
    monitored: int
    healthy: int
    stale: int
    failing: int
    not_yet_collected: int
    paused: int
    experimental: int
    manual_only: int
    verify_later: int
    supported_provider: int
    unknown_ats: int
    failed_recently: list[str]
    by_provider: dict[str, dict[str, int]]
    by_source: dict[str, int]
    by_monitoring_status: dict[str, int]
    by_health: dict[str, int]


class CompanyRegistryResponse(Base):
    coverage: CoverageView
    companies: list[RegistryCompanyView]


class SourceDetectionPayload(Base):
    company_name: str
    careers_url: str


class SourceDetectionResponse(Base):
    """What a careers URL resolves to. Detecting writes nothing."""

    #: CONFIRMED | LIKELY_REVIEW | UNKNOWN. Only CONFIRMED can be monitored.
    confidence: str
    provider: str | None = None
    identifier: str | None = None
    board_url: str | None = None
    #: URL_SHAPE | PAGE_LINK | NONE
    method: str
    detail: str
    postings_visible: int | None = None
    alternatives: list[str] = []
    can_monitor: bool
    #: Set when this board, or this employer, is already in the registry.
    already_in_registry: str | None = None


class AddCompanyPayload(Base):
    company_name: str
    careers_url: str
    #: Keep an employer whose careers site cannot be monitored, as a visible gap.
    manual_only: bool = False


class MonitoringChangePayload(Base):
    company_id: str | None = None
    company_name: str | None = None
    #: PAUSED | ACTIVE
    status: str


class RegistryActionResponse(Base):
    ok: bool
    created: bool = False
    reason: str = ""
    detail: str = ""
    company: RegistryCompanyView | None = None


class TestSourcePayload(Base):
    company_id: str | None = None
    company_name: str | None = None


class TestSourceResponse(Base):
    """One cheap read of a registry company's board. Ingests nothing."""

    ok: bool
    status: str
    detail: str
    postings_visible: int | None = None


class MonitorOfferView(Base):
    company_name: str
    ats_type: str
    ats_identifier: str
    careers_url: str | None = None


class WhyMissedResponse(Base):
    """Why automated discovery did not find a manually added job.

    Every field is derived from stored provenance and collection history. When
    the recorded facts support no reason, `reason` is UNKNOWN and says so.
    """

    job_id: str
    reason: str
    headline: str
    detail: str
    evidence: dict[str, Any]
    secondary_reasons: list[str]
    monitor_offer: MonitorOfferView | None = None


class ScheduleSetupResponse(Base):
    """What a local schedule would look like. Registers nothing."""

    registered: bool
    task_name: str
    interval_hours: float
    command: str
    register_command: str
    notes: list[str]


# ---------------------------------------------------------------------------
# People CRM — the browser surface for contacts, outreach and referrals
# ---------------------------------------------------------------------------


class ContactDetailView(Base):
    """A contact with everything the People page shows in one row."""

    contact_id: str
    name: str
    company_name: str | None = None
    role: str | None = None
    profile_url: str | None = None
    email: str | None = None
    contact_type: str
    warmth: str
    status: str
    shared_context: str | None = None
    last_contact_at: str | None = None
    next_follow_up: str | None = None
    notes: str | None = None
    #: Jobs this person is linked to, so one contact can serve several roles.
    linked_jobs: list[dict[str, Any]]
    referral_state: str
    outreach_count: int
    awaiting_reply: bool
    follow_up_overdue: bool
    #: The concrete step for this relationship, from NextActionService's rules.
    next_action: str
    next_action_detail: str


class PeopleSummaryResponse(Base):
    """The People page: KPIs counted from records, plus its four sections."""

    kpis: dict[str, int]
    action_required: list[ContactDetailView]
    follow_ups_due: list[FollowUpView]
    recent: list[ContactDetailView]
    contacts: list[ContactDetailView]
    #: Distinct values present in the data, for the filter controls.
    filters: dict[str, list[str]]


class ContactUpdatePayload(Base):
    name: str | None = None
    company_name: str | None = None
    role: str | None = None
    profile_url: str | None = None
    email: str | None = None
    contact_type: str | None = None
    warmth: str | None = None
    shared_context: str | None = None
    notes: str | None = None
    status: str | None = None


class LinkContactPayload(Base):
    contact_id: str
    #: REFERRAL | INFORMATIONAL | RECRUITER | HIRING_TEAM | OTHER
    purpose: str = "OTHER"


class SearchStrategyView(Base):
    """A search a human runs. Nothing here fetches anything."""

    persona: str
    label: str
    why: str
    query: str
    linkedin_url: str
    web_url: str
    rank: int


class ContactSuggestionView(Base):
    contact_id: str
    name: str
    role: str | None = None
    company_name: str | None = None
    relationship_type: str
    status: str
    already_linked: bool
    #: Why this person is worth contacting, in checkable words rather than a score.
    reasons: list[str]


class ContactDiscoveryResponse(Base):
    company_name: str
    function_label: str
    existing_contacts: list[ContactSuggestionView]
    strategies: list[SearchStrategyView]
    note: str


class GenerateDraftPayload(Base):
    job_id: str | None = None
    #: LINKEDIN | EMAIL
    channel: str = "LINKEDIN"


class OutreachDraftView(Base):
    channel: str
    subject: str | None = None
    body: str
    evidence_ids: list[str]
    safe_claim_ids: list[str]
    #: What the draft deliberately did not say, and why.
    omissions: list[str]
    over_limit: bool
    character_count: int
    #: Present once the draft has been stored.
    interaction_id: str | None = None


class MarkSentPayload(Base):
    channel: str = "LINKEDIN"
    sent_at: str | None = None
    notes: str | None = None
    follow_up_on: date | None = None


class MarkSentDetailResponse(Base):
    id: str
    sent_by_user: bool
    sent_at: str | None = None
    contact_status: str
    follow_up_due: str | None = None
    note: str


class ReplyPayload(Base):
    notes: str | None = None
    received_at: str | None = None


class SnoozePayload(Base):
    days: int = 7


class OutreachInteractionView(Base):
    id: str
    contact_id: str
    job_id: str | None = None
    channel: str
    draft_body: str | None = None
    sent_by_user: bool
    sent_at: str | None = None
    reply_received_at: str | None = None
    created_at: str | None = None


class ContactHistoryResponse(Base):
    contact: ContactDetailView
    outreach: list[OutreachInteractionView]
    follow_ups: list[FollowUpView]


# ---------------------------------------------------------------------------
# Cover letters — the other document an application asks for
# ---------------------------------------------------------------------------


class CoverLetterRequirementView(Base):
    """What the posting says about a cover letter, and the words it said it in."""

    #: COVER_LETTER_REQUIRED | COVER_LETTER_OPTIONAL | COVER_LETTER_NOT_MENTIONED
    #: | UNKNOWN
    status: str
    label: str
    required: bool
    #: The sentence the verdict came from, verbatim, so a user who disagrees can
    #: see exactly what was read.
    evidence: str = ""
    reason: str = ""
    matched_phrases: list[str] = []


class CoverLetterStoryView(Base):
    """One example the letter used, and why — the internal panel only.

    Evidence IDs appear HERE and never in the letter, the PDF, the DOCX or the
    copied text.
    """

    example: str
    why: str
    evidence_ids: list[str]


class CoverLetterVersionView(Base):
    cover_letter_version_id: str
    job_id: str
    version: int
    status: str
    user_edited: bool
    salutation: str | None = None
    content: str
    word_count: int
    role_archetype: str | None = None
    label: str | None = None
    created_at: str
    updated_at: str | None = None
    evidence_ids_used: list[str] = []
    safe_claim_ids_used: list[str] = []
    pdf_available: bool = False
    docx_available: bool = False
    pdf_filename: str | None = None
    docx_filename: str | None = None
    notes: str | None = None


class CoverLetterGenerateResponse(Base):
    version: CoverLetterVersionView
    why_these_examples: list[CoverLetterStoryView]
    #: What the letter deliberately did not say, and why.
    omissions: list[str]
    requirement: CoverLetterRequirementView


class CoverLetterVersionsResponse(Base):
    job_id: str
    requirement: CoverLetterRequirementView
    count: int
    versions: list[CoverLetterVersionView]
    #: The version the UI should open: the newest saved edit, else the newest draft.
    current: CoverLetterVersionView | None = None
    external_recorded: bool = False


class GenerateCoverLetterPayload(Base):
    """Everything optional here is opt-IN, and each choice is recorded."""

    #: A verified recipient the user chose. Never inferred, never guessed.
    recipient: str | None = None
    #: Name a referrer only when the referral state is REFERRED and the user
    #: asked for it.
    include_referral_contact_id: str | None = None
    #: Work-authorisation wording is never volunteered. When true, the exact
    #: sentence must be supplied and is used verbatim.
    include_work_authorization: bool = False
    work_authorization_text: str | None = None
    extra_context: str | None = None
    #: Replace an unsaved draft rather than adding a version.
    replace_draft: bool = False


class SaveCoverLetterPayload(Base):
    content: str
    salutation: str | None = None
    notes: str | None = None


class ExternalCoverLetterPayload(Base):
    """Recorded, not uploaded — the same path an external resume takes."""

    notes: str | None = None
