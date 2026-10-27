"""EvidenceBankService — read-only access to the authoritative YAML.

``data/*.yaml`` is the canonical Career Evidence Bank. This service loads,
validates and indexes it. **It never writes.** The application database stores
Evidence IDs and the Evidence Bank version used, never duplicated evidence
content.

Gating rules enforced here (see ``context/EVIDENCE_BANK_SCHEMA.md``):

* ``project_status ∈ {PLANNED, IN_DEVELOPMENT}`` ⟹ not retrievable for resumes
* unresolved ``personal_ownership`` gates first-person verbs only, never
  project-level facts
* skills at ``PENDING_USER_CONFIRMATION`` / ``UNSUPPORTED_DO_NOT_USE``
  contribute zero to scoring
* ``resume_ready: true`` requires ``confidence != LOW``
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import DATA_DIR

#: Verbs that assert personal authorship. Gated by unresolved ownership.
FIRST_PERSON_OWNERSHIP_VERBS = frozenset(
    {
        "built",
        "implemented",
        "designed",
        "owned",
        "architected",
        "developed",
        "created",
        "engineered",
        "led",
    }
)

#: Ownership values that block first-person claim forms.
GATED_OWNERSHIP = frozenset(
    {"OWNERSHIP_REQUIRES_USER_REVIEW", "NOT_PRIMARY_OWNER", "TEAM_CONTRIBUTION", "UNKNOWN"}
)

#: Project statuses that can never supply resume evidence.
NON_RESUME_PROJECT_STATUSES = frozenset({"PLANNED", "IN_DEVELOPMENT"})

#: Skill levels excluded from fit scoring entirely.
ZERO_WEIGHT_SKILL_LEVELS = frozenset({"PENDING_USER_CONFIRMATION", "UNSUPPORTED_DO_NOT_USE"})


class EvidenceBankError(RuntimeError):
    """Raised when the Evidence Bank cannot be loaded or fails invariants."""


@dataclass(frozen=True)
class SkillRecord:
    skill_id: str
    name: str
    categories: tuple[str, ...]
    evidence_level: str
    evidence_ids: tuple[str, ...]
    confidence: str
    fit_weight: float
    resume_usable: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def normalized_name(self) -> str:
        return self.name.strip().lower()


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    category: str
    organization_or_project: str
    project_implemented: bool
    personal_ownership: str
    confidence: str
    resume_ready: bool
    first_person_claims_gated: bool
    technologies: tuple[str, ...]
    keywords_supported: tuple[str, ...]
    role_family_relevance: dict[str, str]
    safe_claim_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def ownership_gated(self) -> bool:
        return self.first_person_claims_gated or self.personal_ownership in GATED_OWNERSHIP


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    text: str
    evidence_ids: tuple[str, ...]
    role_families: tuple[str, ...]
    strength: str
    verb_form: str
    gated: bool
    project_level_alternative: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class ProhibitedClaim:
    claim_id: str
    subject: str
    rule: str
    reason: str
    still_permitted: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class EvidenceBankService:
    """Loads and indexes the authoritative Evidence Bank. Read-only."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or DATA_DIR
        self._raw: dict[str, dict[str, Any]] = {}
        self._load()
        self._index()
        self._check_invariants()

    # -- loading ---------------------------------------------------------
    def _read(self, filename: str) -> dict[str, Any]:
        path = self.data_dir / filename
        if not path.exists():
            raise EvidenceBankError(f"Missing Evidence Bank file: {path}")
        with path.open(encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
        if not isinstance(payload, dict):
            raise EvidenceBankError(f"{filename} did not parse to a mapping")
        return payload

    def _load(self) -> None:
        for name in (
            "candidate",
            "experiences",
            "projects",
            "evidence",
            "skills",
            "metrics",
            "claims",
            "gaps",
            "open_questions",
        ):
            self._raw[name] = self._read(f"{name}.yaml")
        self._raw["scoring_weights"] = self._read("config/scoring_weights.yaml")

    # -- indexing --------------------------------------------------------
    def _index(self) -> None:
        weights = self._raw["scoring_weights"].get("skill_evidence_level_weights", {})
        eligibility = self._raw["scoring_weights"].get("skill_resume_eligibility", {})

        self.evidence: dict[str, EvidenceRecord] = {}
        for item in self._raw["evidence"].get("evidence", []):
            rec = EvidenceRecord(
                evidence_id=item["evidence_id"],
                category=item.get("category", ""),
                organization_or_project=item.get("organization_or_project", ""),
                project_implemented=bool(item.get("project_implemented", False)),
                personal_ownership=item.get("personal_ownership", "UNKNOWN"),
                confidence=item.get("confidence", "LOW"),
                resume_ready=bool(item.get("resume_ready", False)),
                first_person_claims_gated=bool(item.get("first_person_claims_gated", False)),
                technologies=tuple(item.get("technologies") or ()),
                keywords_supported=tuple(item.get("keywords_supported") or ()),
                role_family_relevance=dict(item.get("role_family_relevance") or {}),
                safe_claim_ids=tuple(item.get("safe_claim_ids") or ()),
                metric_ids=tuple(item.get("metric_ids") or ()),
                raw=item,
            )
            self.evidence[rec.evidence_id] = rec

        self.skills: dict[str, SkillRecord] = {}
        for item in self._raw["skills"].get("skills", []):
            level = item.get("evidence_level", "PENDING_USER_CONFIRMATION")
            elig = eligibility.get(level, {}) or {}
            skill = SkillRecord(
                skill_id=item["skill_id"],
                name=item["name"],
                categories=tuple(item.get("categories") or ()),
                evidence_level=level,
                evidence_ids=tuple(item.get("evidence_ids") or ()),
                confidence=item.get("confidence", "LOW"),
                fit_weight=float(weights.get(level, 0.0)),
                resume_usable=bool(elig.get("resume_skills_line", False)),
                raw=item,
            )
            self.skills[skill.skill_id] = skill

        self.skills_by_name: dict[str, SkillRecord] = {
            s.normalized_name: s for s in self.skills.values()
        }

        # RETIRED claims stay in the Bank as history but are never usable
        # wording. They used to load into `safe_claims` with the live ones, and
        # nothing downstream read `status` - the resume pool, the recommendation
        # service and the validator's safe-claim match all saw SC-24 as usable.
        # It never reached a page only because it happened to score low.
        self.safe_claims: dict[str, ClaimRecord] = {}
        self.retired_claims: dict[str, dict[str, Any]] = {}
        for item in self._raw["claims"].get("safe_claims", []):
            if str(item.get("status") or "SAFE").upper() == "RETIRED":
                self.retired_claims[item["claim_id"]] = item
                continue
            claim = ClaimRecord(
                claim_id=item["claim_id"],
                text=item.get("text", ""),
                evidence_ids=tuple(item.get("evidence_ids") or ()),
                role_families=tuple(item.get("role_families") or ()),
                strength=item.get("strength", "MODEST"),
                verb_form=item.get("verb_form", "FIRST_PERSON"),
                gated=bool(item.get("gated", False)),
                project_level_alternative=item.get("project_level_alternative"),
                raw=item,
            )
            self.safe_claims[claim.claim_id] = claim

        self.prohibited_claims: dict[str, ProhibitedClaim] = {}
        for item in self._raw["claims"].get("prohibited_claims", []):
            prohibited = ProhibitedClaim(
                claim_id=item["claim_id"],
                subject=item.get("subject", ""),
                rule=item.get("rule", ""),
                reason=item.get("reason", ""),
                still_permitted=item.get("still_permitted"),
                raw=item,
            )
            self.prohibited_claims[prohibited.claim_id] = prohibited

        self.projects: dict[str, dict[str, Any]] = {
            p["project_id"]: p for p in self._raw["projects"].get("projects", [])
        }
        self.experiences: dict[str, dict[str, Any]] = {
            e["experience_id"]: e for e in self._raw["experiences"].get("experiences", [])
        }
        self.metrics: dict[str, dict[str, Any]] = {
            m["metric_id"]: m for m in self._raw["metrics"].get("metrics", [])
        }
        self.gaps: list[dict[str, Any]] = list(self._raw["gaps"].get("gaps", []))
        self.known_absences: list[dict[str, Any]] = list(
            self._raw["skills"].get("known_absences", [])
        )
        self.open_questions: list[dict[str, Any]] = list(
            self._raw["open_questions"].get("open_questions", [])
        )

        # Keyword → evidence index, used by the deterministic matcher.
        self._keyword_index: dict[str, set[str]] = {}
        for rec in self.evidence.values():
            for token in (*rec.technologies, *rec.keywords_supported):
                self._keyword_index.setdefault(str(token).strip().lower(), set()).add(
                    rec.evidence_id
                )

    def _check_invariants(self) -> None:
        """Fail loudly if the Bank violates a hard rule."""
        problems: list[str] = []

        for pid, project in self.projects.items():
            status = project.get("project_status")
            if status in NON_RESUME_PROJECT_STATUSES:
                if project.get("resume_ready"):
                    problems.append(f"{pid}: {status} project marked resume_ready")
                if project.get("resume_auto_select"):
                    problems.append(f"{pid}: {status} project marked resume_auto_select")

        for rec in self.evidence.values():
            if rec.resume_ready and rec.confidence == "LOW":
                problems.append(f"{rec.evidence_id}: resume_ready with LOW confidence")

        for claim in self.safe_claims.values():
            for eid in claim.evidence_ids:
                if eid not in self.evidence:
                    problems.append(f"{claim.claim_id}: references unknown evidence {eid}")

        if problems:
            raise EvidenceBankError("Evidence Bank invariant violations: " + "; ".join(problems))

    # -- public API ------------------------------------------------------
    @property
    def version(self) -> str:
        return str(self._raw["evidence"].get("evidence_bank_version", "unknown"))

    @property
    def candidate(self) -> dict[str, Any]:
        return self._raw["candidate"].get("candidate", {})

    @property
    def is_synthetic_demo(self) -> bool:
        """True only for the public repository's fictional demo bank."""
        return self._raw["candidate"].get("is_synthetic_demo") is True

    @property
    def education(self) -> list[dict[str, Any]]:
        return list(self._raw["candidate"].get("education", []))

    @property
    def work_authorization(self) -> dict[str, Any]:
        return self._raw["candidate"].get("work_authorization", {})

    @property
    def scoring_weights(self) -> dict[str, Any]:
        return self._raw["scoring_weights"]

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        return self.evidence.get(evidence_id)

    def get_skill_by_name(self, name: str) -> SkillRecord | None:
        return self.skills_by_name.get(name.strip().lower())

    def skill_weight(self, name: str) -> float:
        skill = self.get_skill_by_name(name)
        if skill is None or skill.evidence_level in ZERO_WEIGHT_SKILL_LEVELS:
            return 0.0
        return skill.fit_weight

    def find_evidence_by_keyword(self, keyword: str) -> list[EvidenceRecord]:
        """Exact then substring match over the technology/keyword index."""
        key = keyword.strip().lower()
        if not key:
            return []
        ids = set(self._keyword_index.get(key, set()))
        if not ids:
            for indexed, evidence_ids in self._keyword_index.items():
                if key in indexed or indexed in key:
                    ids |= evidence_ids
        return [self.evidence[i] for i in sorted(ids) if i in self.evidence]

    def evidence_for_role_family(
        self, role_family: str, *, min_relevance: str = "MEDIUM"
    ) -> list[EvidenceRecord]:
        order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}
        floor = order.get(min_relevance, 2)
        out = [
            rec
            for rec in self.evidence.values()
            if order.get(rec.role_family_relevance.get(role_family, "NONE"), 0) >= floor
        ]
        return sorted(
            out,
            key=lambda r: order.get(r.role_family_relevance.get(role_family, "NONE"), 0),
            reverse=True,
        )

    def resume_ready_evidence(self) -> list[EvidenceRecord]:
        """Evidence retrievable by the Resume Tuner. Enforces the project gate."""
        out = []
        for rec in self.evidence.values():
            if not rec.resume_ready:
                continue
            if self._project_blocks_resume(rec):
                continue
            out.append(rec)
        return out

    def _project_blocks_resume(self, rec: EvidenceRecord) -> bool:
        project_id = rec.raw.get("project_id")
        if not project_id:
            return False
        project = self.projects.get(project_id)
        if not project:
            return False
        return project.get("project_status") in NON_RESUME_PROJECT_STATUSES

    def claims_for_evidence(self, evidence_id: str) -> list[ClaimRecord]:
        return [c for c in self.safe_claims.values() if evidence_id in c.evidence_ids]

    def usable_claim_text(self, claim_id: str) -> tuple[str, bool]:
        """Return (text, was_downgraded).

        A gated first-person claim yields its ``project_level_alternative``
        instead. This is the ownership rule in its single enforcement point.

        A gated claim with NO alternative yields NOTHING. There is no wording
        for it that the ownership rule permits, so the only safe answer is
        silence — the caller must omit the line, exactly as
        ``ResumeBaseBuilder._claim_text`` already does.

        The earlier ``return claim.text`` fallback was an ownership leak, and it
        was invisible because it depended on the verb: SC-16 ("Integrated HUD
        AMI API data to build…") sits on evidence marked
        OWNERSHIP_REQUIRES_USER_REVIEW, and "Integrated" is not in
        ``FIRST_PERSON_OWNERSHIP_VERBS``, so the claim validator waved it
        through and it rendered on a real resume as a first-person claim.
        """
        claim = self.safe_claims.get(claim_id)
        if claim is None:
            return "", False
        if claim.gated:
            alternative = (claim.project_level_alternative or "").strip()
            return (alternative, True) if alternative else ("", False)
        return claim.text, False

    def summary(self) -> dict[str, Any]:
        return {
            "evidence_bank_version": self.version,
            "evidence_items": len(self.evidence),
            "skills": len(self.skills),
            "safe_claims": len(self.safe_claims),
            "prohibited_claims": len(self.prohibited_claims),
            "projects": len(self.projects),
            "experiences": len(self.experiences),
            "metrics": len(self.metrics),
            "gaps": len(self.gaps),
            "open_questions": len(self.open_questions),
        }


@functools.lru_cache(maxsize=1)
def get_evidence_bank() -> EvidenceBankService:
    """Process-wide cached Evidence Bank."""
    return EvidenceBankService()


def reset_evidence_bank_cache() -> None:
    get_evidence_bank.cache_clear()
