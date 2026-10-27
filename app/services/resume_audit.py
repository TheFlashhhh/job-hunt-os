"""ResumeTargetingAudit — internal QA for a tailored resume.

This is a diagnostic, not a grade. It answers questions an engineer can act on:
did anything unsupported reach the page, how much of the document is generic,
which JD requirements are still unanswered, and how much material the Evidence
Bank is holding back.

**Deliberately not an "ATS score".** No number here predicts what a recruiter or
an applicant tracking system will do, and none is presented as if it did. ATS
scores as sold by resume tools are invented — publishing one inside a system
whose entire premise is evidence discipline would be the same failure the
Evidence Bank exists to prevent.

The one check that matters most is ``unsupported_keyword_leakage``. Targeting
introduced a ranking layer between the JD and the document; this is the assertion
that the ranking layer cannot smuggle anything past the Bank. It must be zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.claim_validator import SafeClaimValidator, get_claim_validator
from app.evidence.service import (
    FIRST_PERSON_OWNERSHIP_VERBS,
    EvidenceBankService,
    get_evidence_bank,
)
from app.services.role_targeting import _STOPWORDS, RoleTargetProfile


@dataclass
class AuditFinding:
    code: str
    severity: str  # INFO | WARN | BLOCK
    message: str
    detail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class TargetingAuditResult:
    """Measured properties of one tailored resume. Internal QA only."""

    role_archetype: str
    resume_base: str

    jd_requirement_coverage: float = 0.0
    supported_keyword_coverage: float = 0.0
    unsupported_keyword_leakage: int = 0
    primary_competency_coverage: float = 0.0
    role_family_alignment: float = 0.0
    ownership_risk_count: int = 0
    duplicate_claim_count: int = 0
    generic_content_ratio: float = 0.0
    project_relevance: float = 0.0
    skills_relevance: float = 0.0

    total_bullets: int = 0
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when nothing at BLOCK severity was found."""
        return not any(f.severity == "BLOCK" for f in self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "role_archetype": self.role_archetype,
            "resume_base": self.resume_base,
            "jd_requirement_coverage": round(self.jd_requirement_coverage, 3),
            "supported_keyword_coverage": round(self.supported_keyword_coverage, 3),
            "unsupported_keyword_leakage": self.unsupported_keyword_leakage,
            "primary_competency_coverage": round(self.primary_competency_coverage, 3),
            "role_family_alignment": round(self.role_family_alignment, 3),
            "ownership_risk_count": self.ownership_risk_count,
            "duplicate_claim_count": self.duplicate_claim_count,
            "generic_content_ratio": round(self.generic_content_ratio, 3),
            "project_relevance": round(self.project_relevance, 3),
            "skills_relevance": round(self.skills_relevance, 3),
            "total_bullets": self.total_bullets,
            "clean": self.clean,
            "findings": [f.as_dict() for f in self.findings],
            "note": (
                "Internal QA. These are measured properties of the document, not a "
                "prediction of how any recruiter or applicant tracking system will "
                "score it."
            ),
        }


class ResumeTargetingAudit:
    """Measures a built resume against the profile it was targeted at."""

    def __init__(
        self,
        bank: EvidenceBankService | None = None,
        validator: SafeClaimValidator | None = None,
    ) -> None:
        self.bank = bank or get_evidence_bank()
        self.validator = validator or get_claim_validator()

    def run(self, base: Any, profile: RoleTargetProfile) -> TargetingAuditResult:
        bullets = [b for entry in base.entries for b in entry.bullets]
        result = TargetingAuditResult(
            role_archetype=profile.role_archetype,
            resume_base=base.base_name,
            total_bullets=len(bullets),
        )
        if not bullets:
            result.findings.append(
                AuditFinding("EMPTY_RESUME", "BLOCK", "The document has no bullets at all.")
            )
            return result

        # Skill NAMES, not ``group.render()``. A group label is furniture, and
        # counting it made "Tools & Platforms" contribute the word "tools" to
        # requirement coverage — the untargeted base then scored higher than
        # the targeted one purely on the strength of its section headings.
        page_text = " ".join(
            [
                base.summary,
                *(b.text for b in bullets),
                *(name for g in base.skill_groups for name in g.skills),
            ]
        )

        self._coverage(result, base, profile, bullets, page_text)
        self._safety(result, base, bullets)
        self._quality(result, base, profile, bullets)
        self._held_back(result, profile)
        return result

    # -- coverage --------------------------------------------------------
    def _coverage(
        self,
        result: TargetingAuditResult,
        base: Any,
        profile: RoleTargetProfile,
        bullets: list[Any],
        page_text: str,
    ) -> None:
        page_tokens = _tokens(page_text)

        # Per REQUIREMENT, not per token. Token overlap counted "and", "one" and
        # "tools" as coverage, which made a longer document score higher than a
        # better-matched one.
        required = [r for r in profile.required_capabilities if _content(r, profile)]
        if required:
            covered = sum(1 for r in required if _answers(r, page_tokens, profile))
            result.jd_requirement_coverage = round(covered / len(required), 3)

        # Only JD terms the Bank ACTUALLY supports count towards coverage.
        # Measuring against every term the posting names would score a resume
        # down for correctly refusing to claim Tableau.
        supportable = {
            term
            for term in profile.tooling_terms
            if (skill := self.bank.get_skill_by_name(term)) is not None and skill.resume_usable
        }
        if supportable:
            present = {t for t in supportable if t in page_tokens}
            result.supported_keyword_coverage = round(len(present) / len(supportable), 3)

        competencies = profile.primary_competencies
        if competencies:
            hit = sum(1 for c in competencies if _tokens(c) & page_tokens)
            result.primary_competency_coverage = round(hit / len(competencies), 3)

        # The archetype's PRIMARY families, not all of them. Every family list
        # ends with DATA_SCIENTIST as a fallback, so matching against the whole
        # list scored every document 1.0 and discriminated nothing.
        primary = set(profile.claim_role_families[:3])
        if primary:
            aligned = sum(1 for b in bullets if self._families(b) & primary)
            result.role_family_alignment = round(aligned / len(bullets), 3)

    def _families(self, bullet: Any) -> set[str]:
        """The role families the Evidence Bank assigns to this bullet's claim."""
        claim = self.bank.safe_claims.get(bullet.claim_id)
        return set(claim.role_families) if claim else set()

    # -- safety ----------------------------------------------------------
    def _safety(self, result: TargetingAuditResult, base: Any, bullets: list[Any]) -> None:
        """The assertions that make targeting safe to have added at all."""
        leaked: list[str] = []
        for group in base.skill_groups:
            for name in group.skills:
                skill = self.bank.get_skill_by_name(name)
                if skill is None or not skill.resume_usable:
                    leaked.append(name)
        result.unsupported_keyword_leakage = len(leaked)
        if leaked:
            result.findings.append(
                AuditFinding(
                    "UNSUPPORTED_SKILL_ON_PAGE",
                    "BLOCK",
                    "A skill the Evidence Bank does not clear reached the skills line.",
                    leaked,
                )
            )

        # Verbatim check: every bullet must still be Bank text. A ranking layer
        # that quietly rewrote a claim would pass every other test here.
        rewritten: list[str] = []
        for bullet in bullets:
            claim = self.bank.safe_claims.get(bullet.claim_id)
            if claim is None:
                continue
            permitted = {claim.text.strip(), (claim.project_level_alternative or "").strip()}
            if bullet.text.strip() not in permitted:
                rewritten.append(f"{bullet.claim_id}: {bullet.text[:80]}")
        if rewritten:
            result.findings.append(
                AuditFinding(
                    "BULLET_NOT_VERBATIM",
                    "BLOCK",
                    "A bullet does not match its safe claim's text.",
                    rewritten,
                )
            )

        # For a gated claim the ONLY permitted wording is its authored
        # project-level alternative. Checked as an exact-text invariant rather
        # than by scanning for first-person verbs, because the verb list is
        # necessarily incomplete: SC-16 leaked onto a real resume as "Integrated
        # HUD AMI API data to build…", and "Integrated" is not on the list.
        risky: list[str] = []
        for bullet in bullets:
            claim = self.bank.safe_claims.get(bullet.claim_id)
            if claim is None or not claim.gated:
                continue
            alternative = (claim.project_level_alternative or "").strip()
            if not alternative or bullet.text.strip() != alternative:
                verb = next(
                    (
                        v
                        for v in FIRST_PERSON_OWNERSHIP_VERBS
                        if re.search(rf"(?<![\w-]){v}(?![\w-])", f" {bullet.text.lower()} ")
                    ),
                    "no project-level alternative exists",
                )
                risky.append(f"{bullet.claim_id}: {verb}")
        result.ownership_risk_count = len(risky)
        if risky:
            result.findings.append(
                AuditFinding(
                    "OWNERSHIP_OVERCLAIM",
                    "BLOCK",
                    "A gated claim rendered with a first-person ownership verb.",
                    risky,
                )
            )

        seen: dict[str, int] = {}
        for bullet in bullets:
            key = bullet.text.strip().lower()
            seen[key] = seen.get(key, 0) + 1
        duplicates = [text for text, count in seen.items() if count > 1]
        result.duplicate_claim_count = len(duplicates)
        if duplicates:
            result.findings.append(
                AuditFinding(
                    "DUPLICATE_BULLET",
                    "WARN",
                    "The same line appears more than once.",
                    [d[:80] for d in duplicates],
                )
            )

    # -- quality ---------------------------------------------------------
    def _quality(
        self,
        result: TargetingAuditResult,
        base: Any,
        profile: RoleTargetProfile,
        bullets: list[Any],
    ) -> None:
        # A line is "generic" when it is doing nothing for THIS application:
        # its evidence is not of a kind this role type wants, AND it does not
        # speak to any stated requirement.
        #
        # Both halves are needed. Evidence kind alone misses a bullet that
        # matches the posting's own words from an off-target project; lexical
        # overlap alone punishes exactly the right bullet for saying "Kafka
        # ingestion" where the posting said "ETL/ELT pipelines". Optimising
        # against lexical overlap by itself is how a tailoring tool turns into
        # a keyword stuffer.
        primary = set(profile.claim_role_families[:3])
        generic = [
            b
            for b in bullets
            if not (self._families(b) & primary) and not (_tokens(b.text) & profile.required_tokens)
        ]
        result.generic_content_ratio = round(len(generic) / len(bullets), 3)
        if result.generic_content_ratio > 0.5:
            result.findings.append(
                AuditFinding(
                    "MOSTLY_GENERIC",
                    "WARN",
                    "Over half the document does not touch a stated requirement. The "
                    "posting may be too far from the Evidence Bank to tailor towards.",
                    [b.text[:80] for b in generic[:5]],
                )
            )

        # Evidence-based, and independent of whether the build was targeted, so
        # the number means the same thing on both sides of a comparison.
        projects = [e for e in base.entries if e.kind != "EXPERIENCE"]
        project_bullets = [b for e in projects for b in e.bullets]
        if project_bullets:
            relevant = sum(1 for b in project_bullets if self._families(b) & primary)
            result.project_relevance = round(relevant / len(project_bullets), 3)

        if base.skill_groups:
            lead = base.skill_groups[0].skills + (
                base.skill_groups[1].skills if len(base.skill_groups) > 1 else []
            )
            if lead:
                hits = sum(1 for name in lead if _tokens(name) & profile.jd_tokens)
                result.skills_relevance = round(hits / len(lead), 3)

    # -- held back -------------------------------------------------------
    def _held_back(self, result: TargetingAuditResult, profile: RoleTargetProfile) -> None:
        """Safe claims the Bank will not let this resume use, and why.

        Not a defect in the resume — a report on the Evidence Bank. A gated
        claim with no ``project_level_alternative`` has no wording the ownership
        rule permits, so it is dropped silently. That is correct behaviour and
        invisible behaviour at the same time, and for this Bank it is currently
        removing some of the strongest analytics-engineering material there is.
        """
        blocked: list[str] = []
        for claim in self.bank.safe_claims.values():
            if not claim.gated or (claim.project_level_alternative or "").strip():
                continue
            if not (set(claim.role_families) & set(profile.claim_role_families)):
                continue
            blocked.append(f"{claim.claim_id}: {claim.text[:90]}")
        if blocked:
            result.findings.append(
                AuditFinding(
                    "OWNERSHIP_GATED_NO_ALTERNATIVE",
                    "INFO",
                    (
                        f"{len(blocked)} safe claim(s) relevant to this role cannot be used: "
                        f"personal ownership is unresolved and no project_level_alternative "
                        f"is authored in data/claims.yaml. Resolving ownership, or writing "
                        f"an alternative, would put them back in play."
                    ),
                    sorted(blocked)[:12],
                )
            )


def _tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9+#]+", (text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def _content(requirement: str, profile: RoleTargetProfile) -> set[str]:
    """The requirement's own distinctive words, filler removed."""
    return _tokens(requirement) - {"experience", "strong", "proficiency", "familiarity"}


def _answers(requirement: str, page_tokens: set[str], profile: RoleTargetProfile) -> bool:
    """Does the document say anything that speaks to this requirement?

    Two distinctive words, or a quarter of them — deliberately generous, because
    the alternative reading (an exact phrase match) would report a resume as
    uncovering a requirement it answers in the employer's own terms with
    different wording.
    """
    tokens = _content(requirement, profile)
    if not tokens:
        return False
    hits = len(tokens & page_tokens)
    return hits >= 2 or hits / len(tokens) >= 0.25
