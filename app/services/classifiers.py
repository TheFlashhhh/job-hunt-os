"""Deterministic classifiers: lane, role family, experience level, location,
employment type, remote status, sponsorship wording and authorization facts.

Deterministic rules run first. An LLM is consulted only where genuine ambiguity
remains, and never for authorization judgements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import load_yaml_config
from app.models.enums import (
    EmploymentType,
    ExperienceLevel,
    RemoteStatus,
    RoleFamily,
    RolePriority,
    SearchLane,
    SponsorshipRoleSignal,
)

# ---------------------------------------------------------------------------
# Employment type / remote status
# ---------------------------------------------------------------------------

_INTERN_RE = re.compile(r"\b(intern|internship|co-?op|placement student)\b", re.I)
_COOP_RE = re.compile(r"\bco-?op\b", re.I)
_PART_TIME_RE = re.compile(r"\bpart[- ]?time\b", re.I)
_CONTRACT_RE = re.compile(r"\b(contract|contractor|temporary|temp|freelance)\b", re.I)
_FULL_TIME_RE = re.compile(r"\bfull[- ]?time\b", re.I)


def classify_employment_type(title: str, description: str) -> EmploymentType:
    blob = f"{title}\n{description[:4000]}"
    if _COOP_RE.search(title) or _COOP_RE.search(blob):
        return EmploymentType.CO_OP
    if _INTERN_RE.search(title) or _INTERN_RE.search(blob):
        return EmploymentType.INTERNSHIP
    if _PART_TIME_RE.search(blob):
        return EmploymentType.PART_TIME
    if _CONTRACT_RE.search(blob):
        return EmploymentType.CONTRACT
    if _FULL_TIME_RE.search(blob):
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN


_REMOTE_RE = re.compile(r"\b(fully remote|100% remote|remote[- ]first|work from home|wfh)\b", re.I)
_REMOTE_WORD_RE = re.compile(r"\bremote\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on-?site|in-?office|in-?person)\b", re.I)


def classify_remote_status(title: str, location: str, description: str) -> RemoteStatus:
    blob = f"{title}\n{location}\n{description[:4000]}"
    if _HYBRID_RE.search(blob):
        return RemoteStatus.HYBRID
    if _REMOTE_RE.search(blob):
        return RemoteStatus.REMOTE
    if _ONSITE_RE.search(blob):
        return RemoteStatus.ONSITE
    if _REMOTE_WORD_RE.search(f"{title}\n{location}"):
        return RemoteStatus.REMOTE
    return RemoteStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

_US_STATE_ABBR = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}
# Country words match case-insensitively; state abbreviations must stay
# case-SENSITIVE or common words ("or", "in", "me", "ok", "hi") false-positive.
_US_COUNTRY_RE = re.compile(r"\b(united states|u\.s\.a?\.?|u\.s\b|usa|us)\b", re.I)
_US_STATE_RE = re.compile(r"\b(" + "|".join(sorted(_US_STATE_ABBR)) + r")\b")

#: Full state names, matched case-insensitively. Abbreviations alone were not
#: enough: "Foxboro, Massachusetts" was classified as non-US, which flipped a
#: real Greater-Boston Fall role to INELIGIBLE for the wrong reason. Employers
#: write the state out at least as often as they abbreviate it.
_US_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|"
    "oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
    "texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    "district of columbia"
)
_US_STATE_NAME_RE = re.compile(r"\b(" + _US_STATE_NAMES + r")\b", re.I)
_NON_US_MARKERS = re.compile(
    r"\b(united kingdom|london|england|canada|toronto|vancouver|india|bangalore|bengaluru|"
    r"hyderabad|germany|berlin|munich|france|paris|netherlands|amsterdam|ireland|dublin|"
    r"singapore|australia|sydney|japan|tokyo|china|shanghai|israel|tel aviv|spain|madrid|"
    r"poland|warsaw|brazil|sao paulo|mexico city|switzerland|zurich|sweden|stockholm)\b",
    re.I,
)

#: Greater Boston towns treated as HIGH geography for the Fall lane.
GREATER_BOSTON = {
    "boston",
    "cambridge",
    "somerville",
    "brookline",
    "newton",
    "watertown",
    "medford",
    "malden",
    "everett",
    "chelsea",
    "quincy",
    "waltham",
    "burlington",
    "woburn",
    "lexington",
    "arlington",
    "belmont",
    "needham",
    "dedham",
    "braintree",
    "revere",
    "allston",
    "brighton",
    "charlestown",
    "back bay",
    "seaport",
    "kendall square",
    "greater boston",
}


@dataclass
class LocationInfo:
    raw: str
    normalized: str
    country: str | None
    state: str | None
    city: str | None
    is_us: bool
    is_greater_boston: bool


def parse_location(location_raw: str, description: str = "") -> LocationInfo:
    raw = (location_raw or "").strip()
    blob = f"{raw} {description[:1500]}"
    lowered = raw.lower()

    is_greater_boston = any(town in lowered for town in GREATER_BOSTON)
    if not is_greater_boston and raw:
        is_greater_boston = bool(re.search(r"\bma\b|massachusetts", lowered)) and any(
            town in blob.lower() for town in GREATER_BOSTON
        )

    non_us = bool(_NON_US_MARKERS.search(raw))
    us_hit = (
        bool(_US_COUNTRY_RE.search(raw))
        or bool(_US_STATE_RE.search(raw))
        or bool(_US_STATE_NAME_RE.search(raw))
        or is_greater_boston
    )
    is_us = us_hit and not non_us
    if not raw:
        is_us = False

    state = None
    state_match = _US_STATE_RE.search(raw)
    if state_match:
        state = state_match.group(1)
    elif re.search(r"massachusetts", lowered):
        state = "MA"

    city = None
    for town in GREATER_BOSTON:
        if town in lowered:
            city = town.title()
            break
    if city is None and raw:
        head = raw.split(",")[0].strip()
        if head and len(head) < 60 and not _REMOTE_WORD_RE.search(head):
            city = head

    country = "US" if is_us else ("NON_US" if non_us else None)
    normalized = re.sub(r"\s+", " ", lowered).strip()

    return LocationInfo(
        raw=raw,
        normalized=normalized,
        country=country,
        state=state,
        city=city,
        is_us=is_us,
        is_greater_boston=is_greater_boston,
    )


# ---------------------------------------------------------------------------
# Experience level
# ---------------------------------------------------------------------------

_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|director|vp|vice president|head of|manager|"
    r"architect|distinguished|fellow)\b",
    re.I,
)
_NEW_GRAD_RE = re.compile(
    r"\b(new ?grad(uate)?|university grad(uate)?|early career|entry[- ]level|campus hire|"
    r"graduate program|rotational program)\b",
    re.I,
)
_YOE_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*years?", re.I)


def extract_years_experience(description: str) -> tuple[int | None, int | None]:
    """Best-effort minimum/maximum years of experience from the JD."""
    window = description[:8000]
    best: tuple[int | None, int | None] = (None, None)
    for match in _YOE_RE.finditer(window):
        context = window[max(0, match.start() - 60) : match.end() + 40].lower()
        if "experience" not in context:
            continue
        lo = int(match.group(1))
        hi = int(match.group(2)) if match.group(2) else None
        if lo > 25:
            continue
        if best[0] is None or lo < best[0]:
            best = (lo, hi)
    return best


def classify_experience_level(
    title: str, description: str, employment_type: EmploymentType
) -> ExperienceLevel:
    if employment_type in (EmploymentType.INTERNSHIP, EmploymentType.CO_OP):
        return ExperienceLevel.INTERNSHIP
    if _NEW_GRAD_RE.search(f"{title}\n{description[:4000]}"):
        return ExperienceLevel.NEW_GRAD
    if _SENIOR_RE.search(title):
        return (
            ExperienceLevel.STAFF_PLUS
            if re.search(
                r"\b(staff|principal|director|vp|head of|distinguished|fellow)\b", title, re.I
            )
            else ExperienceLevel.SENIOR
        )
    lo, _hi = extract_years_experience(description)
    if lo is None:
        return ExperienceLevel.UNKNOWN
    if lo <= 1:
        return ExperienceLevel.ENTRY_LEVEL
    if lo <= 3:
        return ExperienceLevel.EARLY_CAREER
    if lo <= 6:
        return ExperienceLevel.MID_LEVEL
    return ExperienceLevel.SENIOR


# ---------------------------------------------------------------------------
# Role family
# ---------------------------------------------------------------------------

ROLE_PATTERNS: list[tuple[RoleFamily, RolePriority, re.Pattern[str]]] = [
    (
        RoleFamily.DATA_SCIENTIST,
        RolePriority.PRIMARY,
        re.compile(r"\b(data scien(ce|tist)|applied scientist|research scientist, data)\b", re.I),
    ),
    (
        RoleFamily.ML_ENGINEER,
        RolePriority.PRIMARY,
        re.compile(r"\b(machine learning engineer|ml engineer|mle|applied ml)\b", re.I),
    ),
    (
        RoleFamily.AI_ENGINEER,
        RolePriority.PRIMARY,
        re.compile(r"\b(ai engineer|artificial intelligence engineer|genai engineer)\b", re.I),
    ),
    (
        RoleFamily.APPLIED_AI,
        RolePriority.PRIMARY,
        re.compile(r"\b(applied ai|ai solutions|forward deployed (ai )?engineer)\b", re.I),
    ),
    (
        RoleFamily.PRODUCT_ANALYTICS,
        RolePriority.PRIMARY,
        re.compile(r"\b(product analyst|product analytics|growth analyst)\b", re.I),
    ),
    (
        RoleFamily.DATA_ANALYST,
        RolePriority.PRIMARY,
        re.compile(
            r"\b(data analyst|business analyst, data|reporting analyst|insights analyst)\b", re.I
        ),
    ),
    (
        RoleFamily.DATA_ENGINEERING,
        RolePriority.ADJACENT,
        re.compile(r"\b(data engineer|data platform engineer|data infrastructure)\b", re.I),
    ),
    (
        RoleFamily.ANALYTICS_ENGINEERING,
        RolePriority.ADJACENT,
        re.compile(r"\b(analytics engineer|bi engineer|business intelligence engineer)\b", re.I),
    ),
    (
        RoleFamily.AI_SOFTWARE_ENGINEER,
        RolePriority.ADJACENT,
        re.compile(r"\b(ai software engineer|ml platform engineer|mlops engineer)\b", re.I),
    ),
    (
        RoleFamily.DECISION_SCIENCE,
        RolePriority.ADJACENT,
        re.compile(r"\b(decision scien(ce|tist)|quantitative analyst|quant analyst)\b", re.I),
    ),
    (
        RoleFamily.TECHNICAL_PRODUCT,
        RolePriority.ADJACENT,
        re.compile(
            r"\b(technical product manager|product manager, (data|ai|ml)|data product manager)\b",
            re.I,
        ),
    ),
    (
        RoleFamily.SOFTWARE_ENGINEERING,
        RolePriority.LOW_PRIORITY,
        re.compile(
            r"\b(software engineer|software developer|backend engineer|full[- ]stack engineer|sde)\b",
            re.I,
        ),
    ),
]

#: Titles that are never a target regardless of other signals.
HARD_EXCLUDE_RE = re.compile(
    r"\b(nurse|physician|therapist|driver|truck|welder|electrician|plumber|barista|cashier|"
    r"server|bartender|retail associate|sales representative|account executive|"
    r"insurance agent|real estate|recruiter|teacher|tutor|paralegal|attorney|"
    r"security guard|janitor|warehouse|forklift|mechanic|technician, hvac)\b",
    re.I,
)


def classify_role_family(title: str, description: str) -> tuple[RoleFamily, RolePriority]:
    if HARD_EXCLUDE_RE.search(title):
        return RoleFamily.UNRELATED, RolePriority.UNRELATED

    for family, priority, pattern in ROLE_PATTERNS:
        if pattern.search(title):
            return family, priority

    # Title was inconclusive — fall back to the description's opening section.
    head = description[:2500]
    for family, priority, pattern in ROLE_PATTERNS:
        if pattern.search(head):
            demoted = (
                RolePriority.ADJACENT
                if priority == RolePriority.PRIMARY
                else RolePriority.LOW_PRIORITY
            )
            return family, demoted

    if re.search(r"\b(analytics|machine learning|data|ai)\b", title, re.I):
        return RoleFamily.OTHER_ADJACENT, RolePriority.LOW_PRIORITY
    return RoleFamily.UNRELATED, RolePriority.UNRELATED


# ---------------------------------------------------------------------------
# Search lane
# ---------------------------------------------------------------------------

_FALL_TERM_RE = re.compile(
    r"\b(fall (?:20)?26|fall 2026|autumn 2026|september[- ]december|sep[- ]dec|"
    r"academic year|semester (?:internship|co-?op)|fall (?:internship|co-?op))\b",
    re.I,
)
_SUMMER_RE = re.compile(r"\bsummer (?:20)?2[5-9]\b", re.I)
_GRAD_2027_RE = re.compile(r"\b(20)?27\b")


@dataclass
class LaneResult:
    lane: SearchLane
    reasons: list[str] = field(default_factory=list)
    needs_llm: bool = False


def classify_search_lane(
    title: str,
    description: str,
    employment_type: EmploymentType,
    experience_level: ExperienceLevel,
) -> LaneResult:
    """Deterministic lane assignment. Ambiguity is reported, not guessed."""
    reasons: list[str] = []
    blob = f"{title}\n{description[:6000]}"

    is_intern = employment_type in (EmploymentType.INTERNSHIP, EmploymentType.CO_OP)
    fall_signal = bool(_FALL_TERM_RE.search(blob))
    summer_signal = bool(_SUMMER_RE.search(blob))

    # Seniority outranks an unstated employment type: a staff-level posting is
    # out of band for both lanes regardless of whether it says "full-time".
    if not is_intern and experience_level in (ExperienceLevel.SENIOR, ExperienceLevel.STAFF_PLUS):
        reasons.append(
            f"{experience_level.value} seniority is outside both target bands "
            f"(Fall internship / 2027 early-career)."
        )
        return LaneResult(SearchLane.NEITHER, reasons)

    if is_intern and fall_signal:
        reasons.append("Internship/co-op with an explicit Fall/Sep-Dec/academic-year term.")
        return LaneResult(SearchLane.FALL_2026_INTERNSHIP_COOP, reasons)

    if is_intern and summer_signal and not fall_signal:
        reasons.append("Internship explicitly scoped to a Summer term.")
        return LaneResult(SearchLane.NEITHER, reasons)

    if is_intern:
        reasons.append("Internship/co-op with no explicit term — Fall compatibility unverified.")
        return LaneResult(SearchLane.FALL_2026_INTERNSHIP_COOP, reasons, needs_llm=True)

    if employment_type == EmploymentType.PART_TIME:
        reasons.append("Part-time role — potentially compatible with a reduced course load.")
        return LaneResult(SearchLane.FALL_2026_INTERNSHIP_COOP, reasons, needs_llm=True)

    if employment_type == EmploymentType.FULL_TIME:
        if experience_level in (
            ExperienceLevel.NEW_GRAD,
            ExperienceLevel.ENTRY_LEVEL,
            ExperienceLevel.EARLY_CAREER,
        ):
            reasons.append(f"Full-time {experience_level.value} role.")
            return LaneResult(SearchLane.FULL_TIME_2027, reasons)
        if experience_level in (ExperienceLevel.SENIOR, ExperienceLevel.STAFF_PLUS):
            reasons.append(f"Full-time role at {experience_level.value} — outside target bands.")
            return LaneResult(SearchLane.NEITHER, reasons)
        reasons.append("Full-time role with an unclear experience band.")
        return LaneResult(SearchLane.FULL_TIME_2027, reasons, needs_llm=True)

    if _GRAD_2027_RE.search(blob) and not is_intern:
        reasons.append("Mentions a 2027 start.")
        return LaneResult(SearchLane.FULL_TIME_2027, reasons, needs_llm=True)

    reasons.append("Employment type could not be determined from the text.")
    return LaneResult(SearchLane.BOTH_OR_AMBIGUOUS, reasons, needs_llm=True)


# ---------------------------------------------------------------------------
# Sponsorship and authorization wording — FACTS ONLY, never a legal judgement
# ---------------------------------------------------------------------------

_SPONSORSHIP_PATTERNS: list[tuple[SponsorshipRoleSignal, re.Pattern[str]]] = [
    (
        SponsorshipRoleSignal.CITIZENSHIP_REQUIRED,
        re.compile(
            r"\b(must be a? ?u\.?s\.? citizen|us citizenship (is )?required|"
            r"citizenship requirement|u\.?s\.? citizens only)\b",
            re.I,
        ),
    ),
    (
        SponsorshipRoleSignal.CLEARANCE_REQUIRED,
        re.compile(
            r"\b(security clearance|ts/sci|top secret|public trust|active clearance)\b", re.I
        ),
    ),
    # "unable to sponsor", "unable to provide visa sponsorship", "cannot offer
    # sponsorship", "do not sponsor" … followed by a future-scope qualifier.
    (
        SponsorshipRoleSignal.NO_FUTURE_SPONSORSHIP,
        re.compile(
            r"\b(?:not (?:be )?able to|unable to|cannot|can not|can't|will not|won't|"
            r"do(?:es)? not|unwilling to)\s+"
            r"(?:\w+\s+){0,3}?sponsor(?:ship)?\b"
            r"[^.]{0,100}\b(?:now or in the future|at this time or in the future|"
            r"or in the future|in the future)\b",
            re.I,
        ),
    ),
    (
        SponsorshipRoleSignal.NO_SPONSORSHIP_THIS_ROLE,
        re.compile(
            r"\b(?:not (?:be )?able to|unable to|cannot|can not|can't|will not|won't|"
            r"do(?:es)? not|unwilling to)\s+"
            r"(?:\w+\s+){0,3}?sponsor(?:ship)?\b"
            r"|\bno (?:visa )?sponsorship (?:is )?(?:available|offered|provided)\b"
            r"|\bsponsorship (?:is )?not (?:available|offered|provided)\b",
            re.I,
        ),
    ),
    (
        SponsorshipRoleSignal.PERMANENT_AUTHORIZATION_REQUIRED,
        re.compile(
            r"\b(authorized to work[^.]{0,60}without (?:sponsorship|visa sponsorship)|"
            r"permanent work authorization|without the need for sponsorship|"
            r"not require sponsorship)\b",
            re.I,
        ),
    ),
    (
        SponsorshipRoleSignal.SPONSORSHIP_CONFIRMED,
        re.compile(
            r"\b(we sponsor|will sponsor|sponsorship (?:is )?available|"
            r"visa sponsorship (?:is )?(?:offered|available|provided)|"
            r"we (?:do )?provide (?:visa )?sponsorship)\b",
            re.I,
        ),
    ),
    (
        SponsorshipRoleSignal.OPT_STEM_FRIENDLY_OR_EARLY_CAREER_FRIENDLY,
        re.compile(r"\b(opt|cpt|stem opt|f-1|f1 visa)\b", re.I),
    ),
]


@dataclass
class SponsorshipExtraction:
    signal: SponsorshipRoleSignal
    quote: str | None
    all_matches: list[str] = field(default_factory=list)


def extract_sponsorship_signal(description: str) -> SponsorshipExtraction:
    """Extract role-specific sponsorship wording with the verbatim quote."""
    matches: list[str] = []
    found: SponsorshipExtraction | None = None
    for signal, pattern in _SPONSORSHIP_PATTERNS:
        match = pattern.search(description)
        if match:
            quote = _sentence_around(description, match.start())
            matches.append(f"{signal.value}: {quote}")
            if found is None:
                found = SponsorshipExtraction(signal=signal, quote=quote)
    if found is None:
        return SponsorshipExtraction(SponsorshipRoleSignal.UNKNOWN, None, matches)
    found.all_matches = matches
    return found


_AUTH_WORDING_RE = re.compile(
    r"[^.]*\b(work authorization|authorized to work|sponsorship|visa|citizen|clearance|"
    r"employment eligibility|right to work)\b[^.]*\.",
    re.I,
)


def extract_authorization_wording(description: str, *, limit: int = 4) -> str | None:
    """Return the verbatim authorization-related sentences. Facts only."""
    hits = [m.group(0).strip() for m in _AUTH_WORDING_RE.finditer(description)]
    if not hits:
        return None
    seen: list[str] = []
    for hit in hits:
        cleaned = re.sub(r"\s+", " ", hit)
        if cleaned not in seen:
            seen.append(cleaned)
    return " ".join(seen[:limit])[:2000]


def _sentence_around(text: str, index: int, window: int = 220) -> str:
    start = max(0, text.rfind(".", 0, index) + 1)
    end = text.find(".", index)
    end = len(text) if end == -1 else end + 1
    if end - start > window * 2:
        start, end = max(0, index - window), min(len(text), index + window)
    return re.sub(r"\s+", " ", text[start:end]).strip()


# ---------------------------------------------------------------------------
# Fall-lane schedule facts
# ---------------------------------------------------------------------------

#: Matches "20 hours per week", "20-25 hours/week" and the hyphenated
#: adjectival form "40-hour-per-week", which real postings use constantly and
#: which the original pattern silently missed.
_HOURS_RE = re.compile(
    r"(\d{1,2})\s*(?:-|to|–)?\s*(\d{1,2})?\s*[-\s]*hours?\s*[-\s]*(?:per|/|a)\s*[-\s]*week",
    re.I,
)
_ENROLLMENT_RE = re.compile(
    r"\b(currently enrolled|must be enrolled|actively enrolled|enrolled (?:in|at) an? "
    r"(?:accredited )?(?:university|college|degree)|current student|pursuing a (?:bachelor|master|phd))\b",
    re.I,
)
_CREDIT_RE = re.compile(
    r"\b(academic credit|for credit|course credit|receive credit|credit-bearing)\b", re.I
)


@dataclass
class ScheduleFacts:
    weekly_hours: int | None = None
    requires_enrollment: bool | None = None
    academic_credit_required: bool | None = None
    start_date: date | None = None
    end_date: date | None = None


def extract_schedule_facts(description: str) -> ScheduleFacts:
    """Extract Fall-relevant scheduling facts. No legal conclusions."""
    facts = ScheduleFacts()

    hours = _HOURS_RE.search(description)
    if hours:
        lo = int(hours.group(1))
        hi = int(hours.group(2)) if hours.group(2) else None
        facts.weekly_hours = hi or lo

    if _ENROLLMENT_RE.search(description):
        facts.requires_enrollment = True
    if _CREDIT_RE.search(description):
        facts.academic_credit_required = True

    return facts


# ---------------------------------------------------------------------------
# Search profiles (config-driven keyword sets)
# ---------------------------------------------------------------------------


def load_search_profiles() -> dict[str, dict[str, Any]]:
    cfg = load_yaml_config("search_profiles.yaml")
    profiles = cfg.get("search_profiles", []) if cfg else []
    return {p["profile_id"]: p for p in profiles if "profile_id" in p}
