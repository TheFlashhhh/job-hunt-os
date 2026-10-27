"""Domain enums.

These mirror the vocabulary locked in ``context/ARCHITECTURE_VALIDATION.md`` v1.0
and ``context/EVIDENCE_BANK_SCHEMA.md``. Values are stored as strings so the
database stays readable and a Postgres migration stays practical.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum that renders as its value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------------------
# Search lanes
# ---------------------------------------------------------------------------
class SearchLane(StrEnum):
    FALL_2026_INTERNSHIP_COOP = "FALL_2026_INTERNSHIP_COOP"
    FULL_TIME_2027 = "FULL_TIME_2027"
    BOTH_OR_AMBIGUOUS = "BOTH_OR_AMBIGUOUS"
    NEITHER = "NEITHER"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
class DiscoveryChannel(StrEnum):
    A_ATS_WATCHLIST = "A_ATS_WATCHLIST"
    B_APIFY_BROAD = "B_APIFY_BROAD"
    C_MANUAL = "C_MANUAL"
    #: The user pasted a listing URL they found themselves. Kept distinct from
    #: C_MANUAL (paste-a-description) so source analytics can tell "I found this
    #: on LinkedIn" from "I typed this in", and so neither is ever reported as
    #: something an automated channel discovered.
    MANUAL_USER_URL = "MANUAL_USER_URL"


class SourceCapability(StrEnum):
    """Per-source access classification. ATS platforms do NOT behave the same."""

    DOCUMENTED_PUBLIC_API = "DOCUMENTED_PUBLIC_API"
    DOCUMENTED_PUBLIC_FEED = "DOCUMENTED_PUBLIC_FEED"
    PUBLIC_CAREERS_ENDPOINT = "PUBLIC_CAREERS_ENDPOINT"
    AUTHENTICATED_EMPLOYER_API = "AUTHENTICATED_EMPLOYER_API"
    UNDOCUMENTED_PUBLIC_ENDPOINT = "UNDOCUMENTED_PUBLIC_ENDPOINT"
    HTML_PARSER_REQUIRED = "HTML_PARSER_REQUIRED"
    BROWSER_EXTRACTION_REQUIRED = "BROWSER_EXTRACTION_REQUIRED"
    APIFY_DISCOVERY_CANDIDATE = "APIFY_DISCOVERY_CANDIDATE"
    MANUAL = "MANUAL"


class NormalizationStatus(StrEnum):
    PENDING = "PENDING"
    NORMALIZED = "NORMALIZED"
    FAILED = "FAILED"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"


class CollectionRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    #: Nothing to do — source disabled, or not due yet.
    SKIPPED = "SKIPPED"
    #: Stopped early by a budget guard rather than by an error.
    BUDGET_LIMIT_REACHED = "BUDGET_LIMIT_REACHED"


class SourceRole(StrEnum):
    DISCOVERY = "DISCOVERY"
    CANONICAL = "CANONICAL"
    CORROBORATING = "CORROBORATING"


# ---------------------------------------------------------------------------
# Job attributes
# ---------------------------------------------------------------------------
class RemoteStatus(StrEnum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"


class EmploymentType(StrEnum):
    FULL_TIME = "FULL_TIME"
    INTERNSHIP = "INTERNSHIP"
    CO_OP = "CO_OP"
    PART_TIME = "PART_TIME"
    CONTRACT = "CONTRACT"
    UNKNOWN = "UNKNOWN"


class RoleFamily(StrEnum):
    DATA_SCIENTIST = "DATA_SCIENTIST"
    ML_ENGINEER = "ML_ENGINEER"
    AI_ENGINEER = "AI_ENGINEER"
    APPLIED_AI = "APPLIED_AI"
    DATA_ANALYST = "DATA_ANALYST"
    PRODUCT_ANALYTICS = "PRODUCT_ANALYTICS"
    DATA_ENGINEERING = "DATA_ENGINEERING"
    ANALYTICS_ENGINEERING = "ANALYTICS_ENGINEERING"
    AI_SOFTWARE_ENGINEER = "AI_SOFTWARE_ENGINEER"
    SOFTWARE_ENGINEERING = "SOFTWARE_ENGINEERING"
    DECISION_SCIENCE = "DECISION_SCIENCE"
    TECHNICAL_PRODUCT = "TECHNICAL_PRODUCT"
    OTHER_ADJACENT = "OTHER_ADJACENT"
    UNRELATED = "UNRELATED"


class RolePriority(StrEnum):
    PRIMARY = "PRIMARY"
    ADJACENT = "ADJACENT"
    LOW_PRIORITY = "LOW_PRIORITY"
    UNRELATED = "UNRELATED"


class ExperienceLevel(StrEnum):
    INTERNSHIP = "INTERNSHIP"
    NEW_GRAD = "NEW_GRAD"
    ENTRY_LEVEL = "ENTRY_LEVEL"
    EARLY_CAREER = "EARLY_CAREER"
    MID_LEVEL = "MID_LEVEL"
    SENIOR = "SENIOR"
    STAFF_PLUS = "STAFF_PLUS"
    UNKNOWN = "UNKNOWN"


class PostedDateConfidence(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class JobLifecycleStatus(StrEnum):
    OPEN = "OPEN"
    #: Missing from recent polls but not yet confirmed gone. A transient source
    #: error must land here, never straight to CLOSED.
    POSSIBLY_CLOSED = "POSSIBLY_CLOSED"
    DISAPPEARED = "DISAPPEARED"
    CLOSED = "CLOSED"
    REPOSTED = "REPOSTED"


# ---------------------------------------------------------------------------
# Screening / eligibility / authorization
# ---------------------------------------------------------------------------
class ScreenStatus(StrEnum):
    PASSED = "PASSED"
    REJECTED = "REJECTED"
    FLAGGED = "FLAGGED"
    ARCHIVED = "ARCHIVED"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    LIKELY_ELIGIBLE = "LIKELY_ELIGIBLE"
    UNCERTAIN = "UNCERTAIN"
    INELIGIBLE = "INELIGIBLE"


class AuthorizationState(StrEnum):
    """NEVER set to AUTHORIZED_CONFIRMED by the system. User-only."""

    CPT_POSSIBLE_PENDING_ISSO = "CPT_POSSIBLE_PENDING_ISSO"
    AUTHORIZATION_REQUIRES_VERIFICATION = "AUTHORIZATION_REQUIRES_VERIFICATION"
    CPT_LIKELY_INCOMPATIBLE = "CPT_LIKELY_INCOMPATIBLE"
    AUTHORIZED_CONFIRMED = "AUTHORIZED_CONFIRMED"
    INELIGIBLE = "INELIGIBLE"


class SponsorshipRoleSignal(StrEnum):
    SPONSORSHIP_CONFIRMED = "SPONSORSHIP_CONFIRMED"
    SPONSORSHIP_LIKELY = "SPONSORSHIP_LIKELY"
    OPT_STEM_FRIENDLY_OR_EARLY_CAREER_FRIENDLY = "OPT_STEM_FRIENDLY_OR_EARLY_CAREER_FRIENDLY"
    UNKNOWN = "UNKNOWN"
    NO_SPONSORSHIP_THIS_ROLE = "NO_SPONSORSHIP_THIS_ROLE"
    NO_FUTURE_SPONSORSHIP = "NO_FUTURE_SPONSORSHIP"
    PERMANENT_AUTHORIZATION_REQUIRED = "PERMANENT_AUTHORIZATION_REQUIRED"
    CITIZENSHIP_REQUIRED = "CITIZENSHIP_REQUIRED"
    CLEARANCE_REQUIRED = "CLEARANCE_REQUIRED"


class SponsorshipCompanyContext(StrEnum):
    """Historical, company-level. NEVER renders as 'this role sponsors'."""

    HISTORICAL_H1B_FILER_HIGH = "HISTORICAL_H1B_FILER_HIGH"
    HISTORICAL_H1B_FILER_SOME = "HISTORICAL_H1B_FILER_SOME"
    NO_RECENT_H1B_RECORD = "NO_RECENT_H1B_RECORD"
    NOT_CHECKED = "NOT_CHECKED"


class LegitimacyStatus(StrEnum):
    """Separate axis from fit. High fit + LEGITIMACY_UNCERTAIN can coexist."""

    LEGITIMACY_NORMAL = "LEGITIMACY_NORMAL"
    LEGITIMACY_UNCERTAIN = "LEGITIMACY_UNCERTAIN"
    POSSIBLE_GHOST_POSTING = "POSSIBLE_GHOST_POSTING"
    POSSIBLE_SCAM = "POSSIBLE_SCAM"
    SOURCE_UNVERIFIED = "SOURCE_UNVERIFIED"


class CanonicalVerification(StrEnum):
    CANONICAL_VERIFIED = "CANONICAL_VERIFIED"
    SOURCE_ONLY = "SOURCE_ONLY"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    LISTING_CLOSED = "LISTING_CLOSED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Requirements & evidence matching
# ---------------------------------------------------------------------------
class RequirementKind(StrEnum):
    MUST_HAVE = "MUST_HAVE"
    STRONG_PREFERENCE = "STRONG_PREFERENCE"
    NICE_TO_HAVE = "NICE_TO_HAVE"
    AMBIGUOUS = "AMBIGUOUS"
    RESPONSIBILITY = "RESPONSIBILITY"


class SupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class ClaimVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
class DuplicateVerdict(StrEnum):
    SAME_JOB = "SAME_JOB"
    LIKELY_DUPLICATE = "LIKELY_DUPLICATE"
    REPOST = "REPOST"
    SIMILAR_DISTINCT_ROLE = "SIMILAR_DISTINCT_ROLE"
    DISTINCT = "DISTINCT"


class DecidedBy(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"


# ---------------------------------------------------------------------------
# Application CRM
# ---------------------------------------------------------------------------
class ApplicationStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    REVIEW = "REVIEW"
    SHORTLISTED = "SHORTLISTED"
    NETWORKING = "NETWORKING"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLIED = "APPLIED"
    RECRUITER_SCREEN = "RECRUITER_SCREEN"
    OA = "OA"
    INTERVIEW_1 = "INTERVIEW_1"
    INTERVIEW_2 = "INTERVIEW_2"
    FINAL = "FINAL"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    ARCHIVED = "ARCHIVED"


#: Ordered funnel used for conversion analytics. Terminal states excluded.
FUNNEL_ORDER: list[ApplicationStatus] = [
    ApplicationStatus.DISCOVERED,
    ApplicationStatus.REVIEW,
    ApplicationStatus.SHORTLISTED,
    ApplicationStatus.READY_TO_APPLY,
    ApplicationStatus.APPLIED,
    ApplicationStatus.RECRUITER_SCREEN,
    ApplicationStatus.OA,
    ApplicationStatus.INTERVIEW_1,
    ApplicationStatus.FINAL,
    ApplicationStatus.OFFER,
]

TERMINAL_STATUSES = {
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
    ApplicationStatus.ARCHIVED,
}


# ---------------------------------------------------------------------------
# People / networking
# ---------------------------------------------------------------------------
class ContactStatus(StrEnum):
    """Where a relationship stands. Not every person passes through every state.

    A contact who replies and is never asked for a referral is a normal, useful
    outcome, so the sequence is a vocabulary rather than a pipeline.
    """

    IDENTIFIED = "IDENTIFIED"
    #: A draft exists and has not been sent. The system never sends.
    OUTREACH_READY = "OUTREACH_READY"
    OUTREACH_SENT = "OUTREACH_SENT"
    REPLIED = "REPLIED"
    CONVERSATION = "CONVERSATION"
    REFERRAL_REQUESTED = "REFERRAL_REQUESTED"
    REFERRED = "REFERRED"
    DECLINED = "DECLINED"
    NO_RESPONSE = "NO_RESPONSE"
    #: Deliberately finished with — distinct from NO_RESPONSE, which is silence,
    #: and from DECLINED, which is their answer. This one is the user's decision.
    CLOSED = "CLOSED"


class ContactWarmth(StrEnum):
    WARM = "WARM"
    ALUMNI = "ALUMNI"
    SECOND_DEGREE = "SECOND_DEGREE"
    COLD = "COLD"
    UNKNOWN = "UNKNOWN"


class ContactType(StrEnum):
    BU_ALUM = "BU_ALUM"
    PROFESSOR = "PROFESSOR"
    FORMER_COLLEAGUE = "FORMER_COLLEAGUE"
    RECRUITER = "RECRUITER"
    HIRING_MANAGER = "HIRING_MANAGER"
    TEAM_MEMBER = "TEAM_MEMBER"
    FRIEND_CLASSMATE = "FRIEND_CLASSMATE"
    OTHER = "OTHER"


class FollowUpKind(StrEnum):
    NETWORKING_OUTREACH = "NETWORKING_OUTREACH"
    REFERRAL_REQUEST = "REFERRAL_REQUEST"
    RECRUITER_RESPONSE = "RECRUITER_RESPONSE"
    APPLICATION = "APPLICATION"
    INTERVIEW_THANK_YOU = "INTERVIEW_THANK_YOU"
    AUTHORIZATION_CPT = "AUTHORIZATION_CPT"
    DEADLINE = "DEADLINE"
    OTHER = "OTHER"


class FollowUpPriority(StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class ReferralState(StrEnum):
    """Where a referral stands for one job/contact pair."""

    NONE = "NONE"
    POTENTIAL = "POTENTIAL"
    REQUESTED = "REQUESTED"
    RECEIVED = "RECEIVED"
    SUBMITTED_WITH_REFERRAL = "SUBMITTED_WITH_REFERRAL"
    DECLINED = "DECLINED"
    NO_RESPONSE = "NO_RESPONSE"


class RecommendationKind(StrEnum):
    """Resume Tuner output categories. Kept explicitly separate so a gap can
    never be silently presented as something to write down."""

    EMPHASIZE = "EMPHASIZE"
    DE_EMPHASIZE = "DE_EMPHASIZE"
    SAFE_REWORD = "SAFE_REWORD"
    DO_NOT_ADD = "DO_NOT_ADD"
    GAP = "GAP"
    OWNERSHIP_GATED = "OWNERSHIP_GATED"


class ResumeBase(StrEnum):
    """Role-family resume bases."""

    DS_ML_AI = "DS_ML_AI"
    ANALYTICS_PRODUCT = "ANALYTICS_PRODUCT"
    DATA_ENGINEERING = "DATA_ENGINEERING"
    APPLIED_AI_SOFTWARE = "APPLIED_AI_SOFTWARE"
    OPTIONAL_SOFTWARE = "OPTIONAL_SOFTWARE"


# ---------------------------------------------------------------------------
# LLM / operations
# ---------------------------------------------------------------------------
class LLMTask(StrEnum):
    JOB_CLASSIFICATION = "JOB_CLASSIFICATION"
    JD_EXTRACTION = "JD_EXTRACTION"
    ELIGIBILITY_INTERPRETATION = "ELIGIBILITY_INTERPRETATION"
    EVIDENCE_MAPPING = "EVIDENCE_MAPPING"
    FIT_EXPLANATION = "FIT_EXPLANATION"
    RESUME_TAILORING = "RESUME_TAILORING"
    OUTREACH_DRAFTING = "OUTREACH_DRAFTING"
    COMPLEX_REVIEW = "COMPLEX_REVIEW"


class LLMTier(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    CHEAP = "CHEAP"
    STRONG = "STRONG"
    PREMIUM = "PREMIUM"


class AnalysisMode(StrEnum):
    SYNCHRONOUS = "SYNCHRONOUS"
    BATCH = "BATCH"


class WatchlistScope(StrEnum):
    FALL = "FALL"
    FULL_TIME = "FULL_TIME"
    BOTH = "BOTH"


class RegistrySource(StrEnum):
    """How a company entered the company registry."""

    #: ``data/config/companies.yaml`` — the curated seed. The app never edits it.
    CORE_SEED = "CORE_SEED"
    #: Monitored by a click from a job whose employer uses a supported ATS.
    DISCOVERED = "DISCOVERED"
    #: Added on the Sources page from a name and a careers URL.
    USER_ADDED = "USER_ADDED"
    #: Created while importing a pasted job, before anyone chose to monitor it.
    IMPORTED_FROM_JOB = "IMPORTED_FROM_JOB"
    #: A reviewed expansion batch (``scripts/expand_registry.py``), each entry
    #: with a verified careers source.
    CURATED_EXPANSION = "CURATED_EXPANSION"


class MonitoringStatus(StrEnum):
    """Whether structured collection polls a registry company.

    Only ACTIVE is polled on the normal sweep. EXPERIMENTAL is polled only when
    asked for by name — that is the canary gate a new provider passes before it
    joins the sweep.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EXPERIMENTAL = "EXPERIMENTAL"
    #: Relevant, but its careers source has no connector. Kept so coverage
    #: diagnostics can show the gap instead of hiding the employer.
    MANUAL_ONLY = "MANUAL_ONLY"
    #: Relevant, careers source not yet established. Never auto-enabled.
    VERIFY_LATER = "VERIFY_LATER"


class SourceVerification(StrEnum):
    """How sure the registry is about a company's careers source.

    Only CONFIRMED may be activated, and only after a human approves it.
    """

    CONFIRMED = "CONFIRMED"
    LIKELY_REVIEW = "LIKELY_REVIEW"
    UNKNOWN = "UNKNOWN"


class OpportunityType(StrEnum):
    CONFERENCE = "CONFERENCE"
    GRANT = "GRANT"
    SCHOLARSHIP = "SCHOLARSHIP"
    STUDENT_FUNDING = "STUDENT_FUNDING"
    HACKATHON = "HACKATHON"
    CAREER_FAIR = "CAREER_FAIR"
    FELLOWSHIP = "FELLOWSHIP"
    STUDENT_PROGRAM = "STUDENT_PROGRAM"
    COMPETITION = "COMPETITION"
    WORKSHOP = "WORKSHOP"
