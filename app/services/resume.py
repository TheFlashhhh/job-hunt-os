"""Resume Tuner — the recommendation layer.

This module answers one question: *given this job and this base resume, what
should change?* It does not write a resume. It produces a diff of advice, and
every substantive line of that advice carries the Evidence IDs it rests on.

The design constraint is the whole point of the product. A tailoring tool that
optimises for apparent fit will, given a JD mentioning RAG, suggest mentioning
RAG. This one cannot: a JD keyword with no supporting evidence is emitted as a
``DO_NOT_ADD`` recommendation — surfaced *as a warning*, never as text to use.
The six categories are kept strictly separate so a gap can never be quietly
rendered as a suggestion:

``EMPHASIZE``        evidence you have that this JD wants — lead with it
``DE_EMPHASIZE``     evidence that is real but irrelevant here — spend the space elsewhere
``SAFE_REWORD``      wording validated against the Evidence Bank
``DO_NOT_ADD``       JD language with no support — must not appear on the resume
``GAP``              a requirement with nothing behind it, stated plainly
``OWNERSHIP_GATED``  true at project level, not claimable in the first person yet

Every ``EMPHASIZE`` and ``SAFE_REWORD`` line is passed through
:class:`SafeClaimValidator` before it is returned. Anything the validator
blocks is downgraded, never emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.claim_validator import SafeClaimValidator, get_claim_validator
from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.analysis import EvidenceMatch, JobRequirement
from app.models.enums import RecommendationKind, RoleFamily, SupportStatus
from app.models.ingestion import Job
from app.models.misc import ResumeFamily, ResumeRecommendation, ResumeVersion

# ---------------------------------------------------------------------------
# Role-family resume bases
# ---------------------------------------------------------------------------


class ResumeBaseSpec(TypedDict):
    name: str
    description: str
    target_role_families: list[str]


#: The five bases, mapped onto the role families the classifier already emits.
#: Seeded from the Evidence Bank — no resume content is invented here, and the
#: description says what the base is *for*, not what it claims.
RESUME_BASES: list[ResumeBaseSpec] = [
    {
        "name": "DS_ML_AI",
        "description": "Data Science / ML / AI roles. Leads with modelling and analysis evidence.",
        "target_role_families": [
            RoleFamily.DATA_SCIENTIST.value,
            RoleFamily.ML_ENGINEER.value,
            RoleFamily.AI_ENGINEER.value,
            RoleFamily.APPLIED_AI.value,
        ],
    },
    {
        "name": "ANALYTICS_PRODUCT",
        "description": "Product analytics and decision science. Leads with metrics and stakeholder work.",
        "target_role_families": [
            RoleFamily.PRODUCT_ANALYTICS.value,
            RoleFamily.DATA_ANALYST.value,
            RoleFamily.DECISION_SCIENCE.value,
            RoleFamily.TECHNICAL_PRODUCT.value,
        ],
    },
    {
        "name": "DATA_ENGINEERING",
        "description": "Data and analytics engineering. Leads with pipeline, streaming and warehouse evidence.",
        "target_role_families": [
            RoleFamily.DATA_ENGINEERING.value,
            RoleFamily.ANALYTICS_ENGINEERING.value,
        ],
    },
    {
        "name": "APPLIED_AI_SOFTWARE",
        "description": "AI-adjacent software engineering. Leads with systems and backend evidence.",
        "target_role_families": [
            RoleFamily.AI_SOFTWARE_ENGINEER.value,
            RoleFamily.APPLIED_AI.value,
        ],
    },
    {
        "name": "OPTIONAL_SOFTWARE",
        "description": "General software engineering. Held in reserve; used only when a role genuinely fits.",
        "target_role_families": [
            RoleFamily.SOFTWARE_ENGINEERING.value,
        ],
    },
]

_BASE_BY_ROLE_FAMILY: dict[str, str] = {
    role_family: base["name"]
    for base in RESUME_BASES
    for role_family in base["target_role_families"]
}


def seed_resume_bases(session: Session) -> int:
    """Create the five role-family bases. Idempotent."""
    created = 0
    for spec in RESUME_BASES:
        existing = session.execute(
            select(ResumeFamily).where(ResumeFamily.name == spec["name"])
        ).scalar_one_or_none()
        if existing is not None:
            existing.description = spec["description"]
            existing.target_role_families = list(spec["target_role_families"])
            continue
        session.add(
            ResumeFamily(
                name=spec["name"],
                description=spec["description"],
                target_role_families=list(spec["target_role_families"]),
                active=True,
            )
        )
        created += 1
    session.flush()
    return created


def base_for_role_family(role_family: str | None) -> str:
    return _BASE_BY_ROLE_FAMILY.get(role_family or "", "DS_ML_AI")


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@dataclass
class Recommendation:
    kind: RecommendationKind
    body: str
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)
    safe_claim_ids: list[str] = field(default_factory=list)
    requirement_text: str | None = None
    verdict: str | None = None
    rank: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "body": self.body,
            "rationale": self.rationale,
            "evidence_ids": self.evidence_ids,
            "safe_claim_ids": self.safe_claim_ids,
            "requirement_text": self.requirement_text,
            "verdict": self.verdict,
            "rank": self.rank,
        }


@dataclass
class RecommendationSet:
    job_id: str
    resume_base: str
    evidence_bank_version: str
    recommendations: list[Recommendation] = field(default_factory=list)

    def of_kind(self, kind: RecommendationKind) -> list[Recommendation]:
        return [r for r in self.recommendations if r.kind == kind]

    @property
    def counts(self) -> dict[str, int]:
        return {
            kind.value: len(self.of_kind(kind)) for kind in RecommendationKind if self.of_kind(kind)
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "resume_base": self.resume_base,
            "evidence_bank_version": self.evidence_bank_version,
            "counts": self.counts,
            "recommendations": [r.as_dict() for r in self.recommendations],
        }


class ResumeRecommendationService:
    """Builds an evidence-traceable tailoring diff for one job."""

    def __init__(
        self,
        session: Session,
        bank: EvidenceBankService | None = None,
        validator: SafeClaimValidator | None = None,
    ) -> None:
        self.session = session
        self.bank = bank or get_evidence_bank()
        self.validator = validator or get_claim_validator()

    # -- build -----------------------------------------------------------
    def build(self, job: Job, *, resume_base: str | None = None) -> RecommendationSet:
        base = resume_base or base_for_role_family(job.role_family)
        result = RecommendationSet(
            job_id=job.job_id,
            resume_base=base,
            evidence_bank_version=self.bank.version,
        )

        requirements = self._requirements(job)
        emphasized: set[str] = set()

        for requirement, match in requirements:
            kind = self._classify(requirement, match)
            if kind is None:
                continue
            recommendation = self._build_for(requirement, match, kind)
            if recommendation is not None:
                result.recommendations.append(recommendation)
                emphasized.update(recommendation.evidence_ids)

        result.recommendations.extend(self._de_emphasize(job, base, emphasized))
        result.recommendations.extend(self._skills_to_emphasize(requirements))
        result.recommendations = _dedupe(result.recommendations)

        for index, recommendation in enumerate(
            sorted(result.recommendations, key=_recommendation_order)
        ):
            recommendation.rank = index
        result.recommendations.sort(key=lambda r: r.rank)
        return result

    def _requirements(self, job: Job) -> list[tuple[JobRequirement, EvidenceMatch | None]]:
        analysis = job.latest_analysis
        if analysis is None:
            return []
        rows = (
            self.session.execute(
                select(JobRequirement)
                .where(JobRequirement.analysis_id == analysis.analysis_id)
                .order_by(JobRequirement.order_index)
            )
            .scalars()
            .all()
        )
        out: list[tuple[JobRequirement, EvidenceMatch | None]] = []
        for requirement in rows:
            match = requirement.matches[0] if requirement.matches else None
            out.append((requirement, match))
        return out

    def _classify(
        self, requirement: JobRequirement, match: EvidenceMatch | None
    ) -> RecommendationKind | None:
        if match is None:
            return RecommendationKind.GAP
        status = SupportStatus(match.support_status)
        if status == SupportStatus.SUPPORTED:
            if match.ownership_gated:
                return RecommendationKind.OWNERSHIP_GATED
            return RecommendationKind.EMPHASIZE
        if status == SupportStatus.PARTIALLY_SUPPORTED:
            if match.ownership_gated:
                return RecommendationKind.OWNERSHIP_GATED
            return RecommendationKind.SAFE_REWORD
        if status == SupportStatus.UNSUPPORTED:
            # A named-but-unsupported technology is the dangerous case: the JD
            # is asking for a keyword the resume must not acquire.
            return RecommendationKind.DO_NOT_ADD
        return RecommendationKind.GAP

    def _build_for(
        self,
        requirement: JobRequirement,
        match: EvidenceMatch | None,
        kind: RecommendationKind,
    ) -> Recommendation | None:
        evidence_ids = list(match.evidence_ids or []) if match else []
        claim_ids = list(match.safe_claim_ids or []) if match else []

        if kind == RecommendationKind.DO_NOT_ADD:
            return Recommendation(
                kind=kind,
                body=self._do_not_add_body(requirement, match),
                rationale=(match.explanation if match else "")
                or "No Evidence Bank support for this requirement.",
                requirement_text=requirement.text,
                verdict=SupportStatus.UNSUPPORTED.value,
            )

        if kind == RecommendationKind.GAP:
            return Recommendation(
                kind=kind,
                body=f"No evidence covers: {requirement.text}",
                rationale=(
                    "Nothing in the Evidence Bank maps to this requirement. Leave it "
                    "unaddressed on the resume rather than approximating it."
                ),
                requirement_text=requirement.text,
                verdict=SupportStatus.UNKNOWN.value,
            )

        text, claims_used, downgraded = self._claim_text(claim_ids)
        if not text:
            text = self._evidence_summary(evidence_ids)
        if not text:
            return None

        # The gate applies to the wording actually chosen. If an ungated claim
        # was available, this is ordinary EMPHASIZE advice, not a caveat.
        if kind == RecommendationKind.OWNERSHIP_GATED and not downgraded and claims_used:
            kind = RecommendationKind.EMPHASIZE

        check = self.validator.validate(text)
        if check.blocked:
            # The validator wins. A blocked line becomes a warning, never advice.
            return Recommendation(
                kind=RecommendationKind.DO_NOT_ADD,
                body=f"Do not write: {text}",
                rationale="; ".join(check.reasons) or "Blocked by the claim validator.",
                evidence_ids=evidence_ids,
                requirement_text=requirement.text,
                verdict=check.verdict,
            )

        body = check.suggested_text or text
        if kind == RecommendationKind.OWNERSHIP_GATED:
            rationale = (
                "Supported at project level, but personal ownership is unresolved. Use "
                "project-level wording until you confirm your own role."
            )
        elif kind == RecommendationKind.SAFE_REWORD:
            rationale = (
                match.explanation if match else ""
            ) or "Partial support — this wording stays inside what the evidence shows."
        else:
            rationale = (
                match.explanation if match else ""
            ) or "Directly supported by work evidence."

        return Recommendation(
            kind=kind,
            body=body,
            rationale=rationale,
            evidence_ids=evidence_ids,
            safe_claim_ids=claims_used,
            requirement_text=requirement.text,
            verdict=check.verdict,
        )

    def _do_not_add_body(self, requirement: JobRequirement, match: EvidenceMatch | None) -> str:
        unsupported = self._unsupported_terms(requirement, match)
        if unsupported:
            terms = ", ".join(unsupported)
            return (
                f"Do not add {terms} to the resume for this job, even though the posting "
                f"asks for it."
            )
        return f"Do not claim this requirement: {requirement.text}"

    def _unsupported_terms(
        self, requirement: JobRequirement, match: EvidenceMatch | None
    ) -> list[str]:
        """JD keywords that name a skill the Bank cannot support."""
        out: list[str] = []
        for keyword in requirement.keywords or []:
            skill = self.bank.get_skill_by_name(str(keyword))
            if skill is None:
                continue
            unsupported = self.bank.skill_weight(skill.name) <= 0.0 or not skill.resume_usable
            if unsupported and skill.name not in out:
                out.append(skill.name)
        return out

    def _claim_text(self, claim_ids: list[str]) -> tuple[str, list[str], bool]:
        """Pick the strongest safe claim, preferring one that is not gated.

        Returns (text, claim_ids_used, was_downgraded). An ungated claim is
        preferred because "I built X" is more useful on a resume than "the
        project included X" — but only when the Bank actually permits it.
        """
        fallback: tuple[str, str] | None = None
        for claim_id in claim_ids:
            text, downgraded = self.bank.usable_claim_text(claim_id)
            if not text:
                continue
            if not downgraded:
                return text, [claim_id], False
            if fallback is None:
                fallback = (text, claim_id)
        if fallback is not None:
            return fallback[0], [fallback[1]], True
        return "", [], False

    def _evidence_summary(self, evidence_ids: list[str]) -> str:
        for evidence_id in evidence_ids:
            record = self.bank.get_evidence(evidence_id)
            if record is None or not record.resume_ready:
                continue
            for claim_id in record.safe_claim_ids:
                text, _ = self.bank.usable_claim_text(claim_id)
                if text:
                    return text
        return ""

    def _de_emphasize(self, job: Job, base: str, emphasized: set[str]) -> list[Recommendation]:
        """Resume-ready evidence that is irrelevant to this specific role."""
        role_family = job.role_family or RoleFamily.DATA_SCIENTIST.value
        relevant = {
            rec.evidence_id
            for rec in self.bank.evidence_for_role_family(role_family, min_relevance="MEDIUM")
        }
        out: list[Recommendation] = []
        for record in self.bank.resume_ready_evidence():
            if record.evidence_id in emphasized or record.evidence_id in relevant:
                continue
            out.append(
                Recommendation(
                    kind=RecommendationKind.DE_EMPHASIZE,
                    body=f"Shorten or drop: {_describe(record)}",
                    rationale=(
                        f"Low relevance to {role_family}. The space is better spent on "
                        f"evidence this posting actually asks for."
                    ),
                    evidence_ids=[record.evidence_id],
                )
            )
        return out[:6]

    def _skills_to_emphasize(
        self, requirements: list[tuple[JobRequirement, EvidenceMatch | None]]
    ) -> list[Recommendation]:
        """Skills the JD wants that carry real weight — the skills line."""
        wanted: dict[str, float] = {}
        for requirement, _ in requirements:
            for keyword in requirement.keywords or []:
                skill = self.bank.get_skill_by_name(str(keyword))
                if skill is None or not skill.resume_usable:
                    continue
                weight = self.bank.skill_weight(skill.name)
                if weight >= 0.5:
                    wanted[skill.name] = max(wanted.get(skill.name, 0.0), weight)
        if not wanted:
            return []
        ordered = sorted(wanted.items(), key=lambda kv: (-kv[1], kv[0]))
        names = ", ".join(name for name, _ in ordered[:10])
        return [
            Recommendation(
                kind=RecommendationKind.EMPHASIZE,
                body=f"Lead the skills line with: {names}",
                rationale=(
                    "These appear in the posting and carry coursework-verified or stronger "
                    "evidence."
                ),
            )
        ]

    # -- persistence -----------------------------------------------------
    def stored_resume_base(self, job_id: str) -> str | None:
        """The base resume the stored recommendations were built against.

        Read back from the persisted rows rather than recomputed, so the UI
        cannot show "not chosen yet" next to a set of proposals that plainly
        came from a chosen base.
        """
        row = self.session.execute(
            select(ResumeRecommendation)
            .where(
                ResumeRecommendation.job_id == job_id,
                ResumeRecommendation.resume_family_id.is_not(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        family = self.session.get(ResumeFamily, row.resume_family_id)
        return family.name if family else None

    def save(self, result: RecommendationSet) -> list[ResumeRecommendation]:
        """Replace stored recommendations, CARRYING USER DECISIONS FORWARD.

        A refresh rebuilds the proposal set from the current Evidence Bank and
        JD. It must not erase what the user already decided: re-running
        preparation silently wiped seven accept decisions during the first real
        browser test, and the only visible symptom was a counter resetting.

        Decisions are matched on (kind, body) — the same proposed line under
        the same recommendation type is the same decision. A line whose text
        changed is genuinely a new proposal and correctly returns to undecided.
        """
        existing = (
            self.session.execute(
                select(ResumeRecommendation).where(ResumeRecommendation.job_id == result.job_id)
            )
            .scalars()
            .all()
        )
        prior_decisions = {
            (row.kind, row.body): row.accepted for row in existing if row.accepted is not None
        }
        for row in existing:
            self.session.delete(row)
        self.session.flush()

        family = self.session.execute(
            select(ResumeFamily).where(ResumeFamily.name == result.resume_base)
        ).scalar_one_or_none()

        rows = [
            ResumeRecommendation(
                job_id=result.job_id,
                resume_family_id=family.resume_family_id if family else None,
                kind=rec.kind.value,
                rank=rec.rank,
                requirement_text=rec.requirement_text,
                body=rec.body,
                rationale=rec.rationale,
                evidence_ids=rec.evidence_ids or None,
                safe_claim_ids=rec.safe_claim_ids or None,
                verdict=rec.verdict,
            )
            for rec in result.recommendations
        ]
        for row in rows:
            carried = prior_decisions.get((row.kind, row.body))
            if carried is not None:
                row.accepted = carried

        self.session.add_all(rows)
        self.session.flush()
        return rows

    def stored(self, job_id: str) -> list[ResumeRecommendation]:
        return list(
            self.session.execute(
                select(ResumeRecommendation)
                .where(ResumeRecommendation.job_id == job_id)
                .order_by(ResumeRecommendation.rank)
            )
            .scalars()
            .all()
        )

    def record_version(
        self, job: Job, *, base: str, label: str, result: RecommendationSet
    ) -> ResumeVersion | None:
        """Note that a tailored resume was produced for this job."""
        family = self.session.execute(
            select(ResumeFamily).where(ResumeFamily.name == base)
        ).scalar_one_or_none()
        if family is None:
            return None
        evidence_ids: list[str] = []
        claim_ids: list[str] = []
        for rec in result.recommendations:
            evidence_ids.extend(rec.evidence_ids)
            claim_ids.extend(rec.safe_claim_ids)
        version = ResumeVersion(
            resume_family_id=family.resume_family_id,
            label=label,
            job_id=job.job_id,
            evidence_ids_used=sorted(set(evidence_ids)) or None,
            safe_claim_ids_used=sorted(set(claim_ids)) or None,
            evidence_bank_version=self.bank.version,
        )
        self.session.add(version)
        self.session.flush()
        return version


def _describe(record: object) -> str:
    """A human-readable one-liner for an evidence record.

    Recommendations that name only an ID ("drop EV-OT-01") are unusable — the
    reader has to go and look it up before they can act.
    """
    raw = getattr(record, "raw", {}) or {}
    org = getattr(record, "organization_or_project", "") or raw.get("organization_or_project", "")
    role = raw.get("role") or raw.get("title")
    actions = raw.get("actions") or []
    detail = actions[0] if actions else raw.get("context", "")
    head = " — ".join(part for part in (org, role) if part)
    evidence_id = getattr(record, "evidence_id", "")
    body = f"{head}: {detail}" if detail else head
    return f"{body} ({evidence_id})" if body else evidence_id


def _dedupe(recommendations: list[Recommendation]) -> list[Recommendation]:
    """Collapse recommendations whose advice reads identically.

    Several requirements can resolve to the same underlying claim; repeating
    that claim once per requirement is noise, so the copies are merged and
    their evidence and requirement references combined.
    """
    merged: dict[tuple[str, str], Recommendation] = {}
    for rec in recommendations:
        key = (rec.kind.value, rec.body.strip().lower())
        existing = merged.get(key)
        if existing is None:
            merged[key] = rec
            continue
        for evidence_id in rec.evidence_ids:
            if evidence_id not in existing.evidence_ids:
                existing.evidence_ids.append(evidence_id)
        for claim_id in rec.safe_claim_ids:
            if claim_id not in existing.safe_claim_ids:
                existing.safe_claim_ids.append(claim_id)
        if rec.requirement_text and rec.requirement_text != existing.requirement_text:
            existing.requirement_text = (
                f"{existing.requirement_text}; {rec.requirement_text}"
                if existing.requirement_text
                else rec.requirement_text
            )
    return list(merged.values())


_KIND_ORDER = {
    RecommendationKind.EMPHASIZE: 0,
    RecommendationKind.SAFE_REWORD: 1,
    RecommendationKind.OWNERSHIP_GATED: 2,
    RecommendationKind.DO_NOT_ADD: 3,
    RecommendationKind.GAP: 4,
    RecommendationKind.DE_EMPHASIZE: 5,
}


def _recommendation_order(rec: Recommendation) -> tuple[int, str]:
    return (_KIND_ORDER.get(rec.kind, 9), rec.body)
