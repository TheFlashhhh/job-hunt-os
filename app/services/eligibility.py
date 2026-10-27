"""Deterministic screening and the eligibility engine.

Two distinct stages:

* :func:`screen_job` — cheap deterministic pre-filter that runs before any LLM
  spend. It **records** a verdict; it never deletes a row.
* :func:`assess_eligibility` — non-authorization eligibility with reasons.

**Authorization is never computed here.** ``authorization_state`` is copied from
``candidate.yaml`` per lane. Only the user may set ``AUTHORIZED_CONFIRMED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import load_yaml_config
from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.enums import (
    AuthorizationState,
    EligibilityStatus,
    EmploymentType,
    ExperienceLevel,
    RemoteStatus,
    RolePriority,
    ScreenStatus,
    SearchLane,
    SponsorshipRoleSignal,
)
from app.services.classifiers import LocationInfo

# Statuses that must never be auto-assigned by the system.
USER_ONLY_AUTHORIZATION_STATES = {AuthorizationState.AUTHORIZED_CONFIRMED}


@dataclass
class ScreenResult:
    status: ScreenStatus
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == ScreenStatus.PASSED


@dataclass
class EligibilityResult:
    status: EligibilityStatus
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    authorization_state: AuthorizationState = AuthorizationState.AUTHORIZATION_REQUIRES_VERIFICATION
    authorization_note: str = ""
    requires_isso_verification: bool = True


def _rules() -> dict[str, Any]:
    return load_yaml_config("screening_rules.yaml").get("screening", {})


# ---------------------------------------------------------------------------
# Stage 1 — deterministic screen (runs before any LLM spend)
# ---------------------------------------------------------------------------


def screen_job(
    *,
    title: str,
    description: str,
    location: LocationInfo,
    lane: SearchLane,
    employment_type: EmploymentType,
    experience_level: ExperienceLevel,
    role_priority: RolePriority,
    remote_status: RemoteStatus,
    deadline: date | None = None,
    sponsorship_signal: SponsorshipRoleSignal = SponsorshipRoleSignal.UNKNOWN,
    today: date | None = None,
) -> ScreenResult:
    """Cheap deterministic screen. Records a verdict; never deletes."""
    today = today or date.today()
    rules = _rules()
    checks: dict[str, Any] = {}

    # -- location -------------------------------------------------------
    allow_unknown_location = bool(rules.get("allow_unknown_location", True))
    is_remote = remote_status == RemoteStatus.REMOTE
    checks["is_us"] = location.is_us
    checks["is_remote"] = is_remote
    if location.country == "NON_US" and not location.is_us:
        return ScreenResult(
            ScreenStatus.REJECTED,
            f"Location '{location.raw}' is outside the United States.",
            checks,
        )
    if not location.is_us and not is_remote and location.raw and not allow_unknown_location:
        return ScreenResult(
            ScreenStatus.FLAGGED, f"Could not confirm a US location from '{location.raw}'.", checks
        )

    # -- role relevance -------------------------------------------------
    checks["role_priority"] = role_priority.value
    if role_priority == RolePriority.UNRELATED:
        return ScreenResult(
            ScreenStatus.REJECTED, "Role family is unrelated to the target taxonomy.", checks
        )

    # -- seniority ------------------------------------------------------
    checks["experience_level"] = experience_level.value
    if experience_level in (ExperienceLevel.SENIOR, ExperienceLevel.STAFF_PLUS):
        return ScreenResult(
            ScreenStatus.REJECTED,
            f"Seniority {experience_level.value} is above the target bands.",
            checks,
        )

    # -- deadline -------------------------------------------------------
    if deadline is not None and deadline < today:
        checks["deadline"] = deadline.isoformat()
        return ScreenResult(
            ScreenStatus.ARCHIVED, f"Application deadline passed on {deadline.isoformat()}.", checks
        )

    # -- hard authorization blockers (facts stated by the employer) ------
    if sponsorship_signal == SponsorshipRoleSignal.CITIZENSHIP_REQUIRED:
        return ScreenResult(
            ScreenStatus.FLAGGED,
            "Posting states US citizenship is required. Retained for review, not deleted.",
            checks,
        )
    if sponsorship_signal == SponsorshipRoleSignal.CLEARANCE_REQUIRED:
        return ScreenResult(
            ScreenStatus.FLAGGED,
            "Posting states a security clearance is required. Retained for review.",
            checks,
        )

    # -- Fall lane geography --------------------------------------------
    if lane == SearchLane.FALL_2026_INTERNSHIP_COOP:
        compatible = location.is_greater_boston or is_remote or remote_status == RemoteStatus.HYBRID
        checks["fall_geography_compatible"] = compatible
        if not compatible and location.raw:
            return ScreenResult(
                ScreenStatus.FLAGGED,
                (
                    f"Fall role at '{location.raw}' is neither Greater Boston nor remote — "
                    f"would require relocation during the semester. Retained; override to keep."
                ),
                checks,
            )

    if lane == SearchLane.NEITHER:
        return ScreenResult(
            ScreenStatus.REJECTED, "Does not belong to either active search lane.", checks
        )

    return ScreenResult(ScreenStatus.PASSED, "Passed deterministic screening.", checks)


# ---------------------------------------------------------------------------
# Stage 2 — eligibility (non-authorization grounds only)
# ---------------------------------------------------------------------------


def assess_eligibility(
    *,
    lane: SearchLane,
    location: LocationInfo,
    remote_status: RemoteStatus,
    employment_type: EmploymentType,
    experience_level: ExperienceLevel,
    years_min: int | None,
    requires_enrollment: bool | None,
    weekly_hours: int | None,
    deadline: date | None,
    start_date: date | None,
    end_date: date | None,
    sponsorship_signal: SponsorshipRoleSignal,
    bank: EvidenceBankService | None = None,
    today: date | None = None,
) -> EligibilityResult:
    """Rule on eligibility from facts. Never judges work authorization."""
    bank = bank or get_evidence_bank()
    today = today or date.today()
    reasons: list[str] = []
    checks: dict[str, Any] = {}
    verdicts: list[EligibilityStatus] = []

    # -- authorization: copied from candidate.yaml, NEVER computed ------
    auth = bank.work_authorization or {}
    lane_key = (
        "fall_2026" if lane == SearchLane.FALL_2026_INTERNSHIP_COOP else "post_completion_2027"
    )
    lane_auth = auth.get(lane_key, {}) or {}
    raw_state = str(
        lane_auth.get("state", AuthorizationState.AUTHORIZATION_REQUIRES_VERIFICATION.value)
    )
    try:
        auth_state = AuthorizationState(raw_state)
    except ValueError:
        auth_state = AuthorizationState.AUTHORIZATION_REQUIRES_VERIFICATION
    if auth_state in USER_ONLY_AUTHORIZATION_STATES:
        # Defensive: the system must never surface a confirmed state it derived.
        auth_state = AuthorizationState.AUTHORIZATION_REQUIRES_VERIFICATION
    auth_note = str(lane_auth.get("planning_note", "")).strip()

    # -- location -------------------------------------------------------
    is_remote = remote_status == RemoteStatus.REMOTE
    checks["location_us_or_remote"] = location.is_us or is_remote
    if location.is_us or is_remote:
        verdicts.append(EligibilityStatus.ELIGIBLE)
    elif not location.raw:
        reasons.append("Location not stated in the posting.")
        verdicts.append(EligibilityStatus.UNCERTAIN)
    else:
        reasons.append(f"Location '{location.raw}' does not appear to be US-based or remote.")
        verdicts.append(EligibilityStatus.INELIGIBLE)

    # -- degree / graduation window -------------------------------------
    education = (bank._raw["candidate"].get("education") or [{}])[0]
    completion = str(education.get("academic_completion", "Fall 2026"))
    checks["academic_completion"] = completion
    if lane == SearchLane.FULL_TIME_2027:
        reasons.append(f"Degree completion {completion} aligns with a 2027 start.")
        verdicts.append(EligibilityStatus.ELIGIBLE)

    # -- experience band -------------------------------------------------
    checks["years_min_required"] = years_min
    if years_min is not None:
        if years_min <= 3:
            verdicts.append(EligibilityStatus.ELIGIBLE)
        elif years_min <= 5:
            reasons.append(
                f"Requests {years_min}+ years. Above the stated band, but responsibilities may "
                f"still align — flagged rather than excluded."
            )
            verdicts.append(EligibilityStatus.UNCERTAIN)
        else:
            reasons.append(f"Requests {years_min}+ years of experience.")
            verdicts.append(EligibilityStatus.INELIGIBLE)

    # -- Fall-specific schedule facts ------------------------------------
    if lane == SearchLane.FALL_2026_INTERNSHIP_COOP:
        checks["weekly_hours"] = weekly_hours
        checks["requires_enrollment"] = requires_enrollment
        if requires_enrollment:
            reasons.append("Requires current student enrollment — compatible with BU status.")
            verdicts.append(EligibilityStatus.ELIGIBLE)
        if weekly_hours is not None and weekly_hours > 25:
            reasons.append(
                f"States {weekly_hours} hours/week, which may not fit a part-time course load. "
                f"Verify with BU/ISSO."
            )
            verdicts.append(EligibilityStatus.UNCERTAIN)
        if end_date and end_date > date(2027, 1, 31):
            reasons.append(f"End date {end_date.isoformat()} extends beyond the Fall term window.")
            verdicts.append(EligibilityStatus.UNCERTAIN)

    # -- employer-stated authorization facts (facts, not judgements) -----
    checks["sponsorship_role_signal"] = sponsorship_signal.value
    if sponsorship_signal == SponsorshipRoleSignal.CITIZENSHIP_REQUIRED:
        reasons.append("Posting states US citizenship is required.")
        verdicts.append(EligibilityStatus.INELIGIBLE)
    elif sponsorship_signal == SponsorshipRoleSignal.CLEARANCE_REQUIRED:
        reasons.append("Posting states a security clearance is required.")
        verdicts.append(EligibilityStatus.INELIGIBLE)
    elif sponsorship_signal == SponsorshipRoleSignal.PERMANENT_AUTHORIZATION_REQUIRED:
        reasons.append(
            "Posting states work authorization without sponsorship is required. "
            "Whether this is satisfiable is a question for BU/ISSO, not for this system."
        )
        verdicts.append(EligibilityStatus.UNCERTAIN)
    elif sponsorship_signal in (
        SponsorshipRoleSignal.NO_FUTURE_SPONSORSHIP,
        SponsorshipRoleSignal.NO_SPONSORSHIP_THIS_ROLE,
    ):
        # These previously fell through the chain, so a posting that said
        # "no immigration sponsorship now or in the future" produced
        # "No disqualifying facts found in the posting" — actively misleading.
        # The fact is recorded and flagged; whether it is disqualifying is a
        # BU/ISSO question, and the answer differs by lane.
        if lane == SearchLane.FALL_2026_INTERNSHIP_COOP:
            reasons.append(
                "Posting states the employer does not sponsor. A Fall co-op is normally "
                "worked on CPT, which is student authorization rather than employer "
                "sponsorship — but confirm with BU/ISSO before applying."
            )
        else:
            reasons.append(
                "Posting states the employer does not sponsor now or in the future. For a "
                "2027 full-time role this is a serious constraint beyond OPT/STEM-OPT. "
                "Verify with BU/ISSO."
            )
        verdicts.append(EligibilityStatus.UNCERTAIN)

    # -- deadline --------------------------------------------------------
    if deadline is not None:
        checks["deadline"] = deadline.isoformat()
        if deadline < today:
            reasons.append(f"Deadline passed on {deadline.isoformat()}.")
            verdicts.append(EligibilityStatus.INELIGIBLE)

    status = _combine(verdicts)
    if not reasons:
        reasons.append("No disqualifying facts found in the posting.")

    return EligibilityResult(
        status=status,
        reasons=reasons,
        checks=checks,
        authorization_state=auth_state,
        authorization_note=auth_note
        or "Authorization must be verified with BU/ISSO for this specific role.",
        requires_isso_verification=True,
    )


def _combine(verdicts: list[EligibilityStatus]) -> EligibilityStatus:
    """INELIGIBLE dominates; UNCERTAIN is never silently upgraded."""
    if not verdicts:
        return EligibilityStatus.UNCERTAIN
    if EligibilityStatus.INELIGIBLE in verdicts:
        return EligibilityStatus.INELIGIBLE
    if EligibilityStatus.UNCERTAIN in verdicts:
        return EligibilityStatus.UNCERTAIN
    if all(v == EligibilityStatus.ELIGIBLE for v in verdicts):
        return (
            EligibilityStatus.ELIGIBLE if len(verdicts) >= 2 else EligibilityStatus.LIKELY_ELIGIBLE
        )
    return EligibilityStatus.LIKELY_ELIGIBLE
