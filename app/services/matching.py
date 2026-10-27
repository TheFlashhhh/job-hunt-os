"""Evidence Bank matching and priority scoring.

Requirement extraction lives in :mod:`app.services.jd_parser`; it is re-exported
here so existing callers keep working.

The matcher is deterministic: it maps requirement text onto Evidence IDs via the
Bank's technology/keyword index and skill taxonomy. Transferable experience is
never silently converted into direct experience — a transferable-only match is
reported as ``PARTIALLY_SUPPORTED`` and flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.enums import (
    EligibilityStatus,
    RemoteStatus,
    RequirementKind,
    RoleFamily,
    RolePriority,
    SearchLane,
    SponsorshipRoleSignal,
    SupportStatus,
)
from app.services.classifiers import LocationInfo
from app.services.jd_parser import (
    ParsedRequirement,
)

# ---------------------------------------------------------------------------
# Evidence matching
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    support_status: SupportStatus
    evidence_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    safe_claim_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    explanation: str = ""
    transferable_only: bool = False
    ownership_gated: bool = False


class EvidenceMatcher:
    """Maps a requirement onto Evidence IDs. Deterministic and conservative."""

    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        self.bank = bank or get_evidence_bank()

    def match(self, requirement: ParsedRequirement) -> MatchResult:
        evidence_hits: dict[str, int] = {}
        skill_hits: dict[str, float] = {}
        zero_weight_skills: list[str] = []
        non_resume_skills: list[str] = []
        direct_skills: list[str] = []

        for keyword in requirement.keywords:
            skill = self.bank.get_skill_by_name(keyword)
            if skill is not None:
                weight = self.bank.skill_weight(skill.name)
                if weight <= 0.0:
                    zero_weight_skills.append(skill.name)
                else:
                    if not skill.resume_usable:
                        non_resume_skills.append(skill.name)
                    elif skill.evidence_level == "WORK_PROJECT_VERIFIED":
                        direct_skills.append(skill.name)
                    skill_hits[skill.skill_id] = max(skill_hits.get(skill.skill_id, 0.0), weight)
                    for eid in skill.evidence_ids:
                        evidence_hits[eid] = evidence_hits.get(eid, 0) + 1

            for rec in self.bank.find_evidence_by_keyword(keyword):
                evidence_hits[rec.evidence_id] = evidence_hits.get(rec.evidence_id, 0) + 1

        # A requirement whose named technology is unusable (PENDING_USER_CONFIRMATION,
        # UNSUPPORTED_DO_NOT_USE, LEARNING_NOT_RESUME_READY) is UNSUPPORTED, even when
        # unrelated keywords in the same sentence happen to hit the evidence index.
        # This is what stops "production RAG pipelines" earning credit from "pipelines".
        blocking = zero_weight_skills + non_resume_skills
        if blocking and not direct_skills:
            names = ", ".join(sorted(set(blocking)))
            return MatchResult(
                support_status=SupportStatus.UNSUPPORTED,
                skill_ids=sorted(skill_hits),
                confidence=0.0,
                explanation=(
                    f"Requires {names}, which has no work- or project-verified evidence. "
                    f"Other words in this requirement overlap the Evidence Bank, but that "
                    f"overlap is not evidence for {names}."
                ),
            )

        if not evidence_hits and not skill_hits:
            explanation = "No evidence in the Bank matches this requirement."
            if zero_weight_skills:
                explanation += (
                    f" Mentions {', '.join(sorted(set(zero_weight_skills)))}, which carries no "
                    f"usable evidence level."
                )
            return MatchResult(
                support_status=SupportStatus.UNSUPPORTED,
                confidence=0.0,
                explanation=explanation,
            )

        ranked = sorted(evidence_hits.items(), key=lambda kv: kv[1], reverse=True)[:6]
        evidence_ids = [eid for eid, _ in ranked]
        records = [
            record
            for record in (self.bank.get_evidence(eid) for eid in evidence_ids)
            if record is not None
        ]

        ownership_gated = any(r.ownership_gated for r in records)
        resume_ready_hits = [r for r in records if r.resume_ready]

        skill_strength = max(skill_hits.values(), default=0.0)
        coverage = len(evidence_hits) / max(len(requirement.keywords), 1)
        confidence = min(1.0, 0.35 * skill_strength + 0.65 * min(coverage * 2.0, 1.0))

        safe_claim_ids: list[str] = []
        for rec in records[:3]:
            safe_claim_ids.extend(rec.safe_claim_ids)
        safe_claim_ids = sorted(set(safe_claim_ids))[:6]

        # Transferable-only: evidence exists but none of it is direct.
        transferable_only = bool(records) and not resume_ready_hits

        if skill_strength >= 1.0 and resume_ready_hits and confidence >= 0.5:
            status = SupportStatus.SUPPORTED
        elif records or skill_hits:
            status = SupportStatus.PARTIALLY_SUPPORTED
        else:
            status = SupportStatus.UNKNOWN

        parts: list[str] = []
        if resume_ready_hits:
            parts.append(
                "Direct evidence: " + ", ".join(r.evidence_id for r in resume_ready_hits[:4])
            )
        if transferable_only:
            parts.append(
                "Only transferable/adjacent evidence — not direct experience for this requirement."
            )
        if zero_weight_skills:
            parts.append(
                "Mentions "
                + ", ".join(sorted(set(zero_weight_skills)))
                + " which has no usable evidence level."
            )
        if ownership_gated:
            parts.append(
                "Some matched evidence has unresolved personal ownership — project-level "
                "wording only until confirmed."
            )

        return MatchResult(
            support_status=status,
            evidence_ids=evidence_ids,
            skill_ids=sorted(skill_hits),
            safe_claim_ids=safe_claim_ids,
            confidence=round(confidence, 3),
            explanation=" ".join(parts) or "Partial keyword overlap with the Evidence Bank.",
            transferable_only=transferable_only,
            ownership_gated=ownership_gated,
        )


# ---------------------------------------------------------------------------
# Priority scoring — explainable, never a hiring probability
# ---------------------------------------------------------------------------


@dataclass
class PriorityBreakdown:
    total: float
    band: str
    dimensions: dict[str, float]
    max_dimensions: dict[str, float]
    explanation: str
    why_apply: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    strongest_evidence: list[str] = field(default_factory=list)


class PriorityScorer:
    """Computes the explainable 100-point application priority."""

    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        self.bank = bank or get_evidence_bank()
        self.weights = self.bank.scoring_weights.get("application_priority_weights", {})

    def score(
        self,
        *,
        lane: SearchLane,
        eligibility: EligibilityStatus,
        matches: list[tuple[ParsedRequirement, MatchResult]],
        role_priority: RolePriority,
        role_family: RoleFamily,
        location: LocationInfo,
        remote_status: RemoteStatus,
        experience_matches: bool,
        posted_or_first_seen: datetime | date | None,
        sponsorship_signal: SponsorshipRoleSignal,
        weekly_hours: int | None = None,
        contacts_at_company: int = 0,
        today: date | None = None,
    ) -> PriorityBreakdown:
        today = today or date.today()
        lane_key = lane.value if lane.value in self.weights else SearchLane.FULL_TIME_2027.value
        max_dims: dict[str, float] = {
            k: float(v)
            for k, v in (self.weights.get(lane_key) or {}).items()
            if k not in {"total", "note"}
        }
        dims: dict[str, float] = {}
        why: list[str] = []
        risks: list[str] = []
        missing: list[str] = []

        # 1. Eligibility / authorization compatibility
        cap = max_dims.get("eligibility_authorization_compatibility", 20.0)
        elig_fraction = {
            EligibilityStatus.ELIGIBLE: 1.0,
            EligibilityStatus.LIKELY_ELIGIBLE: 0.8,
            EligibilityStatus.UNCERTAIN: 0.45,
            EligibilityStatus.INELIGIBLE: 0.0,
        }[eligibility]
        dims["eligibility_authorization_compatibility"] = round(cap * elig_fraction, 2)
        if eligibility == EligibilityStatus.INELIGIBLE:
            risks.append("Eligibility assessed as INELIGIBLE — see reasons on the job detail.")
        elif eligibility == EligibilityStatus.UNCERTAIN:
            risks.append("Eligibility is UNCERTAIN — verify before investing time.")

        # 2. Core requirement fit
        cap = max_dims.get("core_requirement_fit", 25.0)
        must = [(r, m) for r, m in matches if r.kind == RequirementKind.MUST_HAVE]
        pool = must or matches
        if pool:
            weights = {
                SupportStatus.SUPPORTED: 1.0,
                SupportStatus.PARTIALLY_SUPPORTED: 0.5,
                SupportStatus.UNKNOWN: 0.2,
                SupportStatus.UNSUPPORTED: 0.0,
            }
            fraction = sum(weights[m.support_status] for _, m in pool) / len(pool)
            supported = sum(1 for _, m in pool if m.support_status == SupportStatus.SUPPORTED)
            why.append(
                f"{supported}/{len(pool)} core requirements have direct supporting evidence."
            )
            missing = [
                r.text[:140] for r, m in pool if m.support_status == SupportStatus.UNSUPPORTED
            ][:6]
        else:
            fraction = 0.3
            risks.append("No structured requirements could be extracted from this posting.")
        dims["core_requirement_fit"] = round(cap * fraction, 2)

        # 3. Evidence strength
        cap = max_dims.get("evidence_strength", 15.0)
        strong_ids: list[str] = []
        for _, m in matches:
            if m.support_status == SupportStatus.SUPPORTED:
                strong_ids.extend(m.evidence_ids[:2])
        unique_strong = sorted(set(strong_ids))
        strength_fraction = min(1.0, len(unique_strong) / 8.0)
        dims["evidence_strength"] = round(cap * strength_fraction, 2)

        # 4. Role preference
        cap = max_dims.get("role_preference", 10.0)
        role_fraction = {
            RolePriority.PRIMARY: 1.0,
            RolePriority.ADJACENT: 0.65,
            RolePriority.LOW_PRIORITY: 0.3,
            RolePriority.UNRELATED: 0.0,
        }[role_priority]
        dims["role_preference"] = round(cap * role_fraction, 2)
        if role_priority == RolePriority.PRIMARY:
            why.append(f"{role_family.value} is a primary target role family.")

        # 5. Experience-level fit
        cap = max_dims.get("experience_level_fit", 10.0)
        dims["experience_level_fit"] = round(cap * (1.0 if experience_matches else 0.4), 2)
        if not experience_matches:
            risks.append("Requested experience band sits above the target range.")

        # 6. Recency
        cap = max_dims.get("recency", 5.0)
        dims["recency"] = round(cap * _recency_fraction(posted_or_first_seen, today), 2)

        # 7. Location / remote fit
        cap = max_dims.get("location_remote_fit", 5.0)
        loc_fraction = _location_fraction(lane, location, remote_status)
        dims["location_remote_fit"] = round(cap * loc_fraction, 2)
        if lane == SearchLane.FALL_2026_INTERNSHIP_COOP and loc_fraction < 0.5:
            risks.append("Location is not Greater Boston or remote — hard during the semester.")

        # 8. Networking opportunity
        cap = max_dims.get("networking_opportunity", 5.0)
        dims["networking_opportunity"] = round(
            cap * min(1.0, contacts_at_company / 3.0) if contacts_at_company else 0.0, 2
        )

        # 9. Lane-specific final dimension
        if "future_sponsorship_willingness" in max_dims:
            cap = max_dims["future_sponsorship_willingness"]
            fraction, note = _sponsorship_fraction(sponsorship_signal)
            dims["future_sponsorship_willingness"] = round(cap * fraction, 2)
            if note:
                (why if fraction >= 0.6 else risks).append(note)
        if "schedule_cpt_plausibility" in max_dims:
            cap = max_dims["schedule_cpt_plausibility"]
            fraction, note = _schedule_fraction(weekly_hours, remote_status, location)
            dims["schedule_cpt_plausibility"] = round(cap * fraction, 2)
            if note:
                (why if fraction >= 0.6 else risks).append(note)

        total = round(sum(dims.values()), 2)
        band = "HIGH" if total >= 70 else "MEDIUM" if total >= 45 else "LOW"

        return PriorityBreakdown(
            total=total,
            band=band,
            dimensions=dims,
            max_dimensions=max_dims,
            explanation=(
                f"{total:.0f}/100 on the {lane_key} profile. This is an explainable priority "
                f"score, not a probability of being hired."
            ),
            why_apply=why,
            risks=risks,
            missing=missing,
            strongest_evidence=unique_strong[:8],
        )


def _recency_fraction(when: datetime | date | None, today: date) -> float:
    if when is None:
        return 0.5
    day = when.date() if isinstance(when, datetime) else when
    age = (today - day).days
    if age <= 3:
        return 1.0
    if age <= 7:
        return 0.85
    if age <= 14:
        return 0.65
    if age <= 30:
        return 0.4
    return 0.15


def _location_fraction(
    lane: SearchLane, location: LocationInfo, remote_status: RemoteStatus
) -> float:
    if lane == SearchLane.FALL_2026_INTERNSHIP_COOP:
        if remote_status == RemoteStatus.REMOTE and location.is_us:
            return 1.0
        if location.is_greater_boston:
            return 1.0
        if remote_status == RemoteStatus.HYBRID and location.is_greater_boston:
            return 0.9
        if remote_status == RemoteStatus.HYBRID:
            return 0.4
        return 0.15
    # 2027 full-time: preferred metros score highest, remote and other US still count.
    if location.is_greater_boston:
        return 1.0
    if remote_status == RemoteStatus.REMOTE and location.is_us:
        return 0.95
    if location.state in {"NY", "CA"}:
        return 0.9
    if location.is_us:
        return 0.7
    return 0.2


def _sponsorship_fraction(signal: SponsorshipRoleSignal) -> tuple[float, str]:
    match signal:
        case SponsorshipRoleSignal.SPONSORSHIP_CONFIRMED:
            return 1.0, "Posting states visa sponsorship is available."
        case SponsorshipRoleSignal.SPONSORSHIP_LIKELY:
            return 0.8, "Sponsorship appears likely based on the posting."
        case SponsorshipRoleSignal.OPT_STEM_FRIENDLY_OR_EARLY_CAREER_FRIENDLY:
            return 0.7, "Posting references OPT/CPT/F-1 — early-career friendly wording."
        case SponsorshipRoleSignal.UNKNOWN:
            return 0.5, ""
        case SponsorshipRoleSignal.NO_SPONSORSHIP_THIS_ROLE:
            return 0.15, "Posting states no sponsorship for this role."
        case SponsorshipRoleSignal.NO_FUTURE_SPONSORSHIP:
            return 0.0, "Posting states no sponsorship now or in the future — long-term tradeoff."
        case SponsorshipRoleSignal.PERMANENT_AUTHORIZATION_REQUIRED:
            return 0.1, "Posting requires work authorization without sponsorship."
        case _:
            return 0.0, "Posting states citizenship or clearance is required."


def _schedule_fraction(
    weekly_hours: int | None, remote_status: RemoteStatus, location: LocationInfo
) -> tuple[float, str]:
    """Schedule plausibility for the Fall lane. Never an authorization judgement."""
    score = 0.5
    note = ""
    if weekly_hours is not None:
        if weekly_hours <= 20:
            score, note = 1.0, f"{weekly_hours} hrs/week fits a reduced course load."
        elif weekly_hours <= 30:
            score, note = 0.6, f"{weekly_hours} hrs/week is demanding alongside coursework."
        else:
            score, note = 0.2, f"{weekly_hours} hrs/week is unlikely to fit the semester."
    if remote_status == RemoteStatus.REMOTE:
        score = min(1.0, score + 0.2)
        note = note or "Remote role — schedule-compatible with BU coursework."
    elif location.is_greater_boston:
        score = min(1.0, score + 0.1)
        note = note or "Greater Boston location — commutable during the semester."
    return score, note
