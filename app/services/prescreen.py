"""Cheap deterministic prefilter — the expensive-analysis boundary.

Pilot 1 analyzed all 1,010 unique jobs even though ~95% were plainly
irrelevant (sales, support, non-US, senior). That was free while analysis is
deterministic, and would be ruinous the moment an LLM provider is enabled.

This service is that boundary, made explicit. It answers one question as
cheaply as possible:

    is this posting worth spending real analysis on?

**What it uses:** company, title, location, remote status, employment type,
seniority wording, student/intern signal, and obvious citizenship or clearance
language. All from already-normalized fields.

**What it must never use:** Evidence Bank matching, requirement extraction,
priority scoring, resume recommendations, or any LLM call. Those are the
expensive work this exists to gate.

**The asymmetry that matters.** A false IRRELEVANT is a missed job — invisible,
permanent, and the one failure this system cannot afford. A false PLAUSIBLE
costs a few milliseconds of deterministic work. So the classifier only rejects
on *strong, specific* deterministic evidence, and everything genuinely
ambiguous lands in UNCERTAIN, which stays reviewable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.models.enums import EmploymentType, ExperienceLevel, RolePriority
from app.services import classifiers as cls
from app.services.functional_classifier import (
    TITLE_CLEAR_IRRELEVANT,
    classify_function,
)
from app.services.internship_filter import classify_internship

#: Bumped when prefilter logic changes, so stored verdicts can be recomputed
#: without forcing unrelated expensive analysis.
#: 0.2.0 — function is read from the description before the title-pattern
#: fallback runs, so an unfamiliar title no longer rejects a relevant job.
#: 0.3.0 — confirmed internships and co-ops are rejected before analysis; the
#: search is full-time only (D-51).
PRESCREEN_VERSION = "0.3.0"


class PrescreenOutcome(str, Enum):
    PLAUSIBLE = "PREFILTER_PLAUSIBLE"
    UNCERTAIN = "PREFILTER_UNCERTAIN"
    IRRELEVANT = "PREFILTER_IRRELEVANT"


class PrescreenReason(str, Enum):
    UNRELATED_ROLE = "UNRELATED_ROLE"
    NON_US = "NON_US"
    SENIORITY_TOO_HIGH = "SENIORITY_TOO_HIGH"
    FALL_LOCATION_INCOMPATIBLE = "FALL_LOCATION_INCOMPATIBLE"
    EMPLOYMENT_TYPE_MISMATCH = "EMPLOYMENT_TYPE_MISMATCH"
    CITIZENSHIP_REQUIRED = "CITIZENSHIP_REQUIRED"
    CLEARANCE_REQUIRED = "CLEARANCE_REQUIRED"
    DEADLINE_PASSED = "DEADLINE_PASSED"
    STUDENT_ROLE_MISMATCH = "STUDENT_ROLE_MISMATCH"
    OTHER_DETERMINISTIC = "OTHER_DETERMINISTIC"
    #: Not a rejection — records why something was kept despite weak signals.
    RETAINED_FOR_REVIEW = "RETAINED_FOR_REVIEW"


@dataclass
class PrescreenResult:
    outcome: PrescreenOutcome
    reasons: list[PrescreenReason] = field(default_factory=list)
    detail: str = ""
    version: str = PRESCREEN_VERSION

    @property
    def needs_analysis(self) -> bool:
        """Only PLAUSIBLE earns full analysis. UNCERTAIN stays reviewable."""
        return self.outcome is PrescreenOutcome.PLAUSIBLE

    @property
    def reason_codes(self) -> list[str]:
        return [r.value for r in self.reasons]


#: Titles that are unambiguously outside every target family. Deliberately
#: specific: "manager" alone is not here, because "Data Science Manager" is a
#: real target and "analytics" appears in many hybrid titles.
_CLEARLY_UNRELATED = re.compile(
    r"\b(nurse|nursing|physician|dentist|pharmacist|therapist|veterinar\w+|"
    r"chef|cook|barista|server|bartender|housekeep\w+|janitor|custodian|"
    r"driver|courier|warehouse|forklift|welder|electrician|plumber|carpenter|"
    r"mechanic|technician,? (?:hvac|automotive)|"
    r"account executive|sales (?:representative|rep|manager|director|executive)|"
    r"business development (?:representative|manager)|sdr\b|bdr\b|"
    r"customer (?:success|support|service) (?:representative|associate|manager|specialist)|"
    r"recruiter|talent acquisition|hr business partner|payroll|"
    r"paralegal|attorney|counsel|"
    r"social media|copywriter|content writer|graphic designer|"
    r"real estate|leasing|retail associate|store manager|cashier|"
    r"security guard|dispatcher|flight attendant|pilot)\b",
    re.I,
)

#: Any of these makes a posting worth a closer look regardless of title shape.
_DATA_SIGNAL = re.compile(
    r"\b(data|analytic\w*|analyst|scientist|science|machine learning|\bml\b|\bai\b|"
    r"artificial intelligence|statistic\w*|quantitative|research|model\w*|"
    r"engineer|engineering|software|developer|python|sql|etl|pipeline|"
    r"business intelligence|\bbi\b|insight\w*|experimentation|forecast\w*|"
    r"decision scien\w*|informatics|bioinformatics)\b",
    re.I,
)

#: Student-shaped roles. The Fall lane depends on these surviving.
_STUDENT_SIGNAL = re.compile(
    r"\b(intern|internship|co-?op|student|new grad|university|early career|"
    r"apprentice|fall|spring|summer|semester|graduate program|campus)\b",
    re.I,
)

_SENIOR_TITLE = re.compile(
    r"\b(senior staff|staff|principal|distinguished|fellow|architect|"
    r"director|vice president|\bvp\b|\bsvp\b|\bevp\b|head of|chief|"
    r"\bcto\b|\bceo\b|\bcio\b|manager,? (?:senior|director))\b",
    re.I,
)


class PreScreenService:
    """Decides whether a posting earns expensive analysis. No I/O, no LLM."""

    def evaluate(
        self,
        *,
        title: str,
        description: str,
        company_name: str = "",
        location_raw: str | None = None,
    ) -> PrescreenResult:
        reasons: list[PrescreenReason] = []
        notes: list[str] = []

        title_text = title or ""
        # Only the head of the body is read. Full-document work is what this
        # service exists to defer.
        head = (description or "")[:2000]
        combined = f"{title_text}\n{head}"

        student = bool(_STUDENT_SIGNAL.search(title_text))
        # A data signal anywhere — title or body — is enough to keep a posting
        # alive. Checking only the title rejected "Business Systems Coordinator"
        # whose body was entirely about reporting and data workflows.
        data_in_title = bool(_DATA_SIGNAL.search(title_text))
        data_signal = data_in_title or bool(_DATA_SIGNAL.search(head))

        # -- hard deterministic blocks -----------------------------------
        sponsorship = cls.extract_sponsorship_signal(combined)
        if sponsorship.signal.value == "CITIZENSHIP_REQUIRED":
            reasons.append(PrescreenReason.CITIZENSHIP_REQUIRED)
        if sponsorship.signal.value == "CLEARANCE_REQUIRED":
            reasons.append(PrescreenReason.CLEARANCE_REQUIRED)
        if reasons:
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=reasons,
                detail=sponsorship.quote
                or "Posting states a citizenship or clearance requirement.",
            )

        # -- clearly unrelated occupation --------------------------------
        # For a clearly-unrelated occupation the TITLE must carry the data
        # signal — "Account Executive" whose body happens to say "data-driven"
        # is still a sales role.
        if _CLEARLY_UNRELATED.search(title_text) and not data_in_title:
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=[PrescreenReason.UNRELATED_ROLE],
                detail=f"Title is outside every target role family: {title_text[:80]!r}",
            )

        # -- location ------------------------------------------------------
        location = cls.parse_location(location_raw or "", head)
        remote = cls.classify_remote_status(title_text, location_raw or "", head)
        if location_raw and not location.is_us and remote.value != "REMOTE":
            # Only reject when the source actually told us where it is. A blank
            # location is unknown, not foreign.
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=[PrescreenReason.NON_US],
                detail=f"Location {location_raw!r} is not US-based and the role is not remote.",
            )

        # -- seniority -----------------------------------------------------
        employment = cls.classify_employment_type(title_text, head)
        experience = cls.classify_experience_level(title_text, head, employment)
        senior_title = bool(_SENIOR_TITLE.search(title_text))
        too_senior = experience in (ExperienceLevel.SENIOR, ExperienceLevel.STAFF_PLUS)

        if (senior_title or too_senior) and not student:
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=[PrescreenReason.SENIORITY_TOO_HIGH],
                detail=f"Seniority {experience.value} is well beyond early career.",
            )

        # -- student roles -------------------------------------------------
        # Job Hunt OS is full-time-only as of 2026-09-08 (D-51). A confirmed
        # internship or co-op is rejected here, before any expensive analysis:
        # it is cheap to detect and there is no path that would ever want it.
        #
        # Only a CONFIRMED verdict rejects. An AMBIGUOUS one — an employment
        # type that says INTERNSHIP with nothing in the title or body agreeing —
        # stays reviewable, because that field has been wrong about real
        # full-time new-grad postings.
        student_role = classify_internship(
            title=title_text, description=description, employment_type=None
        )
        if student_role.is_internship:
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=[PrescreenReason.EMPLOYMENT_TYPE_MISMATCH],
                detail=f"Internship or co-op; the search is full-time only. {student_role.reason}"[:500],
            )

        # -- function, from the description --------------------------------
        # The old order asked `classify_role_family` — a list of TITLE regexes —
        # and rejected anything it did not recognise. That put 95% of a
        # 10,000-job corpus into `UNRELATED`, including postings whose bodies
        # were entirely SQL, Python and experimentation. Function is now decided
        # by the description, and the title only closes the door when it names a
        # different occupation outright.
        functional = classify_function(title_text, description or "")
        if functional.title_triage == TITLE_CLEAR_IRRELEVANT:
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=[PrescreenReason.UNRELATED_ROLE],
                detail=f"Title names a different occupation: {title_text[:80]!r}",
            )
        if functional.is_functional_match:
            return PrescreenResult(
                outcome=PrescreenOutcome.PLAUSIBLE,
                detail=(
                    f"Functional match: {functional.primary_function}. "
                    + (functional.reasons[0] if functional.reasons else "")
                )[:500],
            )

        _, role_priority = cls.classify_role_family(title_text, head)
        if role_priority is RolePriority.UNRELATED and not data_signal and not student:
            return PrescreenResult(
                outcome=PrescreenOutcome.IRRELEVANT,
                reasons=[PrescreenReason.UNRELATED_ROLE],
                detail=(
                    "No target function in the description, no target role family, "
                    "and no data signal."
                ),
            )

        # -- student-shaped roles ------------------------------------------
        # This used to be the Fall safety net: a student-shaped posting with any
        # technical signal was PLAUSIBLE, because missing a Fall co-op was the
        # expensive error. The Fall lane was retired on 2026-08-24, so a
        # student-shaped posting is no longer a target. It is kept REVIEWABLE
        # rather than rejected — an "Analyst, University Graduate" full-time
        # posting is student-shaped wording on a real job. D-46.
        if student and (data_signal or _DATA_SIGNAL.search(head)):
            notes.append(
                "Student-shaped wording; the Fall internship lane is archived, so this "
                "is retained for review rather than targeted."
            )

        lane = cls.classify_search_lane(title_text, head, employment, experience)

        # -- plausible ------------------------------------------------------
        if role_priority in (RolePriority.PRIMARY, RolePriority.ADJACENT):
            return PrescreenResult(
                outcome=PrescreenOutcome.PLAUSIBLE,
                detail=f"Target role family, {role_priority.value}, lane {lane.lane.value}.",
            )

        # -- everything else stays reviewable --------------------------------
        # Low-priority families, blank locations, odd employment types and
        # unconventional titles land here rather than being deleted. An
        # unconventional 2027 title, or a role asking 2-3 years, is exactly the
        # case the deeper fit engine is meant to judge.
        if lane.needs_llm:
            notes.append("Lane classification is ambiguous.")
        if employment is EmploymentType.UNKNOWN:
            notes.append("Employment type not stated.")
        if not location_raw:
            notes.append("No location given by the source.")
        return PrescreenResult(
            outcome=PrescreenOutcome.UNCERTAIN,
            reasons=[PrescreenReason.RETAINED_FOR_REVIEW],
            detail=" ".join(notes) or f"Role priority {role_priority.value}; retained for review.",
        )


def should_run_semantic_analysis(
    *,
    prescreen: PrescreenResult,
    llm_available: bool,
    analysis_is_stale: bool = False,
    content_changed: bool = False,
    budget_permits: bool = True,
) -> bool:
    """The future LLM gate. Testable today without an API key.

    Deliberately conservative: semantic analysis is spent on postings that
    already passed the cheap filter, or on ambiguous ones a cheap classifier
    could not resolve. It is never spent on a posting the prefilter rejected.
    """
    if not llm_available or not budget_permits:
        return False
    if prescreen.outcome is PrescreenOutcome.IRRELEVANT:
        return False
    if prescreen.outcome is PrescreenOutcome.UNCERTAIN:
        # Worth resolving semantically — that is what UNCERTAIN means.
        return True
    return analysis_is_stale or content_changed or True


def rescreen_existing(session: object, *, only_missing: bool = True) -> dict[str, int]:
    """Apply the prefilter to jobs already stored, WITHOUT re-analyzing them.

    Prefilter logic changes far more often than analysis logic, and re-screening
    must not drag expensive analysis along with it. This walks stored jobs,
    records a current verdict, and touches nothing else — no requirement
    extraction, no evidence matching, no scoring, no LLM.

    Existing analyses are deliberately left in place: a job that was analyzed
    under the old pipeline keeps its analysis even if it now screens as
    IRRELEVANT, because deleting work already paid for gains nothing.
    """
    from sqlalchemy import select

    from app.models.ingestion import Job

    service = PreScreenService()
    counts: dict[str, int] = {}

    stmt = select(Job)
    if only_missing:
        stmt = stmt.where(
            (Job.prescreen_outcome.is_(None)) | (Job.prescreen_version != PRESCREEN_VERSION)
        )

    for job in session.execute(stmt).scalars():  # type: ignore[attr-defined]
        result = service.evaluate(
            title=job.title,
            description=job.description or "",
            company_name=job.company_name,
            location_raw=job.location_raw,
        )
        job.prescreen_outcome = result.outcome.value
        job.prescreen_reasons = result.reason_codes
        job.prescreen_detail = result.detail[:2000]
        job.prescreen_version = result.version
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1

    return counts
