#!/usr/bin/env python3
"""Validate the Career Evidence Bank in data/*.yaml.

This validator protects the Evidence Bank's integrity guarantees. It is deliberately
dependency-light (PyYAML only) and does not import or scaffold any Job Hunt OS code.

Usage:
    python scripts/validate_evidence_bank.py
    python scripts/validate_evidence_bank.py --data-dir path/to/data

Exit codes:
    0  all checks passed (warnings may still be printed)
    1  one or more checks failed
    2  could not load the Evidence Bank at all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    print("ERROR: PyYAML is required.  pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from exc


# --------------------------------------------------------------------------- enums

PROVENANCE = {
    "DIRECT_USER_CLARIFICATION",
    "REPO_OR_PROJECT_ARTIFACT",
    "MASTER_BANK",
    "JOB_HUNT_OS_CONTEXT",
    "OLDER_RESUME",
    "CLAUDE_MEMORY",
}

CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}

OWNERSHIP = {
    "PRIMARY_OWNER",
    "SUBSTANTIAL_CONTRIBUTOR",
    "TEAM_CONTRIBUTION",
    "SUPPORTING_CONTRIBUTOR",
    "NOT_PRIMARY_OWNER",
    "OWNERSHIP_REQUIRES_USER_REVIEW",
    "UNKNOWN",
}

UNRESOLVED_OWNERSHIP = {"OWNERSHIP_REQUIRES_USER_REVIEW", "UNKNOWN"}

METRIC_STATUS = {
    "VERIFIED",
    "APPROXIMATE",
    "DERIVED",
    "OBSERVATIONAL",
    "CAUSALITY_NOT_ESTABLISHED",
    "UNVERIFIED",
    # The candidate remembers the figure; the measurement it came from is gone.
    # Added 2026-09-24 for the OpenText Entity Framework figures (M-074..M-076).
    "USER_RECALLED_APPROXIMATE",
}

SKILL_LEVEL = {
    "WORK_PROJECT_VERIFIED",
    "COURSEWORK_VERIFIED",
    "USER_CONFIRMED_PRACTICE",
    "SELF_REPORTED_LIMITED",
    "LEARNING_NOT_RESUME_READY",
    "PENDING_USER_CONFIRMATION",
    "UNSUPPORTED_DO_NOT_USE",
}

SKILL_LEVELS_NOT_RESUME_USABLE = {
    "LEARNING_NOT_RESUME_READY",
    "PENDING_USER_CONFIRMATION",
    "UNSUPPORTED_DO_NOT_USE",
}

PROJECT_STATUS = {
    "PLANNED",
    "IN_DEVELOPMENT",
    "WORKING_MVP",
    "DEPLOYED",
    "COMPLETE",
    "DELIVERED",
    "EVALUATED",
    "LEGACY_CANDIDATE",
    "LEGACY",
    "ARCHIVED",
}

NEVER_RESUME_READY_STATUS = {"PLANNED", "IN_DEVELOPMENT"}

UNCERTAINTY_CLASS = {
    "BLOCKING_FOR_ALL_USE",
    "BLOCKING_ONLY_FOR_SPECIFIC_CLAIM",
    "NON_BLOCKING_UNKNOWN",
    "OPTIONAL_ENRICHMENT",
}

FIRST_PERSON_VERBS = {
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

# Metrics whose caveat may never be dropped, and the field that must be present.
# M-068 added 2026-09-16: 90.8% accuracy on a ~9.7% positive class is a truthful
# number that misleads on its own, so the 8.2% recall travels with it by rule.
# M-074/M-075 added 2026-09-24: recalled OpenText figures scoped to "targeted
# workflows" - without that scope they read as the whole application.
CAVEAT_REQUIRED_METRICS = {
    "M-013": "mandatory_caveat",
    "M-068": "mandatory_caveat",
    "M-074": "mandatory_caveat",
    "M-075": "mandatory_caveat",
    # Job Hunt OS (D-72): probes are lock probes, not writes; the payload cut
    # was measured on a 57-board sweep.
    "M-084": "mandatory_caveat",
    "M-085": "mandatory_caveat",
    "M-087": "mandatory_caveat",
}

# Claims that must carry an un-strippable caveat.
CAVEAT_REQUIRED_CLAIMS = {
    "SC-11": "mandatory_caveat",
    "SC-64": "mandatory_caveat",
    "SC-67": "mandatory_caveat",
    "SC-77": "mandatory_caveat",
    "SC-78": "mandatory_caveat",
}

# How often the Resume Tuner should reach for a project, relative to the rest.
# A USER PREFERENCE, not an evidence judgement: it changes ranking only, and a
# LOWER project keeps every claim, metric and evidence item it had. The
# multipliers these labels map to live in data/config/role_targeting.yaml.
RESUME_SELECTION_PRIORITY = {"HIGHER", "NORMAL", "LOWER"}


# --------------------------------------------------------------------------- result


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checks_run = 0
        self.checks_passed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks_run += 1
        if ok:
            self.checks_passed += 1
        else:
            self.errors.append(f"{name}: {detail}")

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def load(path: Path, report: Report) -> Any:
    if not path.exists():
        report.error(f"MISSING FILE: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        report.error(f"YAML PARSE ERROR in {path.name}: {exc}")
        return None


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# --------------------------------------------------------------------------- checks


def validate(data_dir: Path) -> Report:
    r = Report()

    candidate = load(data_dir / "candidate.yaml", r)
    experiences_doc = load(data_dir / "experiences.yaml", r)
    projects_doc = load(data_dir / "projects.yaml", r)
    evidence_doc = load(data_dir / "evidence.yaml", r)
    skills_doc = load(data_dir / "skills.yaml", r)
    metrics_doc = load(data_dir / "metrics.yaml", r)
    claims_doc = load(data_dir / "claims.yaml", r)
    gaps_doc = load(data_dir / "gaps.yaml", r)
    questions_doc = load(data_dir / "open_questions.yaml", r)

    if r.errors:
        return r  # cannot proceed past parse/load failures

    experiences = experiences_doc.get("experiences", [])
    projects = projects_doc.get("projects", [])
    evidence = evidence_doc.get("evidence", [])
    skills = skills_doc.get("skills", [])
    metrics = metrics_doc.get("metrics", [])
    safe_claims = claims_doc.get("safe_claims", [])
    prohibited = claims_doc.get("prohibited_claims", [])
    gaps = gaps_doc.get("gaps", [])
    open_qs = questions_doc.get("open_questions", [])
    resolved_qs = questions_doc.get("resolved_questions", [])

    exp_ids = {e["experience_id"] for e in experiences}
    prj_ids = {p["project_id"] for p in projects}
    edu_ids = {e["education_id"] for e in candidate.get("education", [])}
    ev_ids = {e["evidence_id"] for e in evidence}
    m_ids = {m["metric_id"] for m in metrics}
    sk_ids = {s["skill_id"] for s in skills}
    gap_ids = {g["gap_id"] for g in gaps}
    q_ids = {q["question_id"] for q in open_qs} | {q["question_id"] for q in resolved_qs}
    safe_ids = {c["claim_id"] for c in safe_claims}
    dnc_ids = {c["claim_id"] for c in prohibited}

    # ---- 1. unique IDs ---------------------------------------------------
    for label, items, key in (
        ("evidence", evidence, "evidence_id"),
        ("metrics", metrics, "metric_id"),
        ("skills", skills, "skill_id"),
        ("experiences", experiences, "experience_id"),
        ("projects", projects, "project_id"),
        ("gaps", gaps, "gap_id"),
    ):
        seen: set[str] = set()
        dupes: set[str] = set()
        for item in items:
            v = item.get(key)
            if v in seen:
                dupes.add(v)
            seen.add(v)
        r.check(f"unique-{label}-ids", not dupes, f"duplicates: {sorted(dupes)}")

    all_claim_ids = [c["claim_id"] for c in safe_claims] + [c["claim_id"] for c in prohibited]
    dupe_claims = {c for c in all_claim_ids if all_claim_ids.count(c) > 1}
    r.check("unique-claim-ids", not dupe_claims, f"duplicates: {sorted(dupe_claims)}")

    # ---- 2. evidence integrity -------------------------------------------
    for e in evidence:
        eid = e.get("evidence_id", "<missing>")

        parents = [e.get("experience_id"), e.get("project_id"), e.get("education_id")]
        if not any(parents):
            r.error(f"evidence-parent: {eid} references no experience, project or education")
        if e.get("experience_id") and e["experience_id"] not in exp_ids:
            r.error(f"evidence-parent: {eid} -> unknown experience {e['experience_id']}")
        if e.get("project_id") and e["project_id"] not in prj_ids:
            r.error(f"evidence-parent: {eid} -> unknown project {e['project_id']}")
        if e.get("education_id") and e["education_id"] not in edu_ids:
            r.error(f"evidence-parent: {eid} -> unknown education {e['education_id']}")

        for mid in as_list(e.get("metric_ids")):
            if mid not in m_ids:
                r.error(f"evidence-metric-ref: {eid} -> unknown metric {mid}")
        for cid in as_list(e.get("safe_claim_ids")):
            if cid not in safe_ids:
                r.error(f"evidence-claim-ref: {eid} -> unknown safe claim {cid}")
        for cid in as_list(e.get("prohibited_claim_ids")):
            if cid not in dnc_ids:
                r.error(f"evidence-dnc-ref: {eid} -> unknown prohibited claim {cid}")
        for gid in as_list(e.get("related_gap_ids")):
            if gid not in gap_ids:
                r.error(f"evidence-gap-ref: {eid} -> unknown gap {gid}")
        for qid in as_list(e.get("open_question_ids")):
            if qid not in q_ids:
                r.error(f"evidence-question-ref: {eid} -> unknown question {qid}")

        own = e.get("personal_ownership")
        if own is not None and own not in OWNERSHIP:
            r.error(f"evidence-ownership-enum: {eid} -> invalid '{own}'")

        conf = e.get("confidence")
        if conf is not None and conf not in CONFIDENCE:
            r.error(f"evidence-confidence-enum: {eid} -> invalid '{conf}'")

        for p in as_list(e.get("provenance")):
            if p not in PROVENANCE:
                r.error(f"evidence-provenance-enum: {eid} -> invalid '{p}'")

        uc = e.get("uncertainty_class")
        if uc is not None and uc not in UNCERTAINTY_CLASS:
            r.error(f"evidence-uncertainty-enum: {eid} -> invalid '{uc}'")

        # LOW confidence may not be resume_ready
        if e.get("resume_ready") and conf == "LOW":
            r.error(f"low-confidence-resume-ready: {eid} is resume_ready with confidence LOW")

        # CLAUDE_MEMORY may never be the sole provenance of resume-usable evidence
        prov = set(as_list(e.get("provenance")))
        if e.get("resume_ready") and prov == {"CLAUDE_MEMORY"}:
            r.error(f"memory-sole-provenance: {eid} is resume_ready with only CLAUDE_MEMORY")

    r.check("evidence-referential-integrity", True)

    # ---- 3. planned projects can never be resume-ready --------------------
    for p in projects:
        pid = p.get("project_id")
        status = p.get("project_status")
        if status not in PROJECT_STATUS:
            r.error(f"project-status-enum: {pid} -> invalid '{status}'")
        if status in NEVER_RESUME_READY_STATUS:
            if p.get("resume_ready"):
                r.error(f"planned-resume-ready: {pid} status={status} but resume_ready is true")
            if p.get("resume_auto_select"):
                r.error(
                    f"planned-auto-select: {pid} status={status} but resume_auto_select is true"
                )
            if as_list(p.get("evidence_ids")):
                r.error(f"planned-has-evidence: {pid} status={status} but has evidence_ids")
        for eid in as_list(p.get("evidence_ids")):
            if eid not in ev_ids:
                r.error(f"project-evidence-ref: {pid} -> unknown evidence {eid}")

        priority = p.get("resume_selection_priority")
        if priority is not None and priority not in RESUME_SELECTION_PRIORITY:
            r.error(f"project-selection-priority-enum: {pid} -> invalid '{priority}'")
        # A preference is not a gate. LOWER must never be used as a soft delete:
        # if a project is not resume-usable, that is resume_ready's job to say.
        if priority == "LOWER" and not p.get("resume_ready"):
            r.warn(
                f"selection-priority-on-unusable-project: {pid} is LOWER priority but "
                f"is not resume_ready - the priority field has nothing to rank"
            )
    r.check("planned-projects-not-resume-ready", True)
    r.check("project-selection-priority-enum", True)

    # evidence must not belong to a PLANNED/IN_DEVELOPMENT project
    prj_status = {p["project_id"]: p.get("project_status") for p in projects}
    for e in evidence:
        pid = e.get("project_id")
        if pid and prj_status.get(pid) in NEVER_RESUME_READY_STATUS:
            r.error(
                f"evidence-on-planned-project: {e['evidence_id']} belongs to {pid} ({prj_status[pid]})"
            )
    r.check("no-evidence-on-planned-projects", True)

    # ---- 4. experiences ---------------------------------------------------
    for x in experiences:
        xid = x.get("experience_id")
        for eid in as_list(x.get("evidence_ids")):
            if eid not in ev_ids:
                r.error(f"experience-evidence-ref: {xid} -> unknown evidence {eid}")
        own = x.get("personal_ownership")
        if own is not None and own not in OWNERSHIP:
            r.error(f"experience-ownership-enum: {xid} -> invalid '{own}'")
    r.check("experience-referential-integrity", True)

    # ---- 5. safe claims ---------------------------------------------------
    ev_ownership = {e["evidence_id"]: e.get("personal_ownership") for e in evidence}

    for c in safe_claims:
        cid = c["claim_id"]
        status = c.get("status")

        claim_priority = c.get("resume_selection_priority")
        if claim_priority is not None and claim_priority not in RESUME_SELECTION_PRIORITY:
            r.error(f"claim-selection-priority-enum: {cid} -> invalid '{claim_priority}'")

        if status == "RETIRED":
            rep = c.get("replacement_claim_id")
            if rep and rep not in safe_ids:
                r.error(f"retired-replacement-ref: {cid} -> unknown replacement {rep}")
            continue

        refs = as_list(c.get("evidence_ids"))
        if not refs:
            r.error(f"claim-no-evidence: {cid} references no evidence")
        for eid in refs:
            if eid not in ev_ids:
                r.error(f"claim-evidence-ref: {cid} -> unknown evidence {eid}")
        for mid in as_list(c.get("metric_ids")):
            if mid not in m_ids:
                r.error(f"claim-metric-ref: {cid} -> unknown metric {mid}")
        for qid in as_list(c.get("gate_question_ids")):
            if qid not in q_ids:
                r.error(f"claim-question-ref: {cid} -> unknown question {qid}")

        # an unsupported claim can never be marked safe
        if cid in dnc_ids:
            r.error(f"claim-in-both-banks: {cid} appears in safe_claims and prohibited_claims")

        # unresolved ownership must not yield an ungated first-person claim
        if c.get("verb_form") == "FIRST_PERSON" and not c.get("gated"):
            for eid in refs:
                if ev_ownership.get(eid) == "OWNERSHIP_REQUIRES_USER_REVIEW":
                    r.error(
                        f"ungated-first-person: {cid} uses FIRST_PERSON verb form on {eid} "
                        f"whose personal_ownership is OWNERSHIP_REQUIRES_USER_REVIEW"
                    )

        # a gated claim should offer a project-level alternative or an explicit reason
        if c.get("gated") and not (c.get("project_level_alternative") or c.get("gate_reason")):
            r.warn(f"gated-claim-no-alternative: {cid} is gated with no alternative or reason")

        # leading verb sanity: first word of a FIRST_PERSON claim should be an
        # ownership verb, and a PROJECT_LEVEL claim should not lead with one
        text = (c.get("text") or "").strip()
        first_word = text.split(" ")[0].lower().rstrip(",;:") if text else ""
        stem = first_word[:-2] if first_word.endswith("ed") else first_word
        looks_first_person = (
            any(
                first_word == v + "" or first_word == v[:-1] + "ed" or first_word == v
                for v in FIRST_PERSON_VERBS
            )
            or first_word in FIRST_PERSON_VERBS
        )
        if c.get("verb_form") == "PROJECT_LEVEL" and looks_first_person and not c.get("gated"):
            r.warn(
                f"verb-form-mismatch: {cid} is PROJECT_LEVEL but opens with "
                f"ownership verb '{first_word}'"
            )
        del stem

    r.check("safe-claim-integrity", True)

    # ---- 6. caveats retained ---------------------------------------------
    # A bank may name its own caveat-bound records (a different person's bank,
    # or the public demo one). Without that declaration the lists above apply,
    # so a missing M-074 in this bank is still an error rather than a pass.
    required_metrics = metrics_doc.get("caveat_required_metrics") or CAVEAT_REQUIRED_METRICS
    required_claims = claims_doc.get("caveat_required_claims") or CAVEAT_REQUIRED_CLAIMS
    metric_by_id = {m["metric_id"]: m for m in metrics}
    for mid, field in required_metrics.items():
        m = metric_by_id.get(mid)
        if m is None:
            r.error(f"caveat-metric-missing: {mid} not found")
            continue
        if not m.get(field):
            r.error(f"caveat-stripped: metric {mid} is missing required field '{field}'")
        if m.get("caveat_strippable") is not False:
            r.error(f"caveat-strippable: metric {mid} must set caveat_strippable: false")

    claim_by_id = {c["claim_id"]: c for c in safe_claims}
    for cid, field in required_claims.items():
        c = claim_by_id.get(cid)
        if c is None:
            r.error(f"caveat-claim-missing: {cid} not found")
            continue
        if not c.get(field):
            r.error(f"caveat-stripped: claim {cid} is missing required field '{field}'")
        if c.get("caveat_strippable") is not False:
            r.error(f"caveat-strippable: claim {cid} must set caveat_strippable: false")
    r.check("mandatory-caveats-retained", True)

    # ---- 7. metrics -------------------------------------------------------
    for m in metrics:
        mid = m.get("metric_id")
        for s in as_list(m.get("status")):
            if s not in METRIC_STATUS:
                r.error(f"metric-status-enum: {mid} -> invalid '{s}'")
        if not as_list(m.get("status")):
            r.error(f"metric-status-missing: {mid} has no status")
        for p in as_list(m.get("provenance")):
            if p not in PROVENANCE:
                r.error(f"metric-provenance-enum: {mid} -> invalid '{p}'")
        if "allowed_formulations" not in m:
            r.error(f"metric-no-allowed-formulations: {mid} must declare allowed_formulations")
        for eid in as_list(m.get("evidence_ids")):
            if eid not in ev_ids:
                r.error(f"metric-evidence-ref: {mid} -> unknown evidence {eid}")
        # APPROXIMATE metrics must carry a precision rule
        if "APPROXIMATE" in as_list(m.get("status")) and not m.get("precision_rule"):
            r.warn(f"approximate-no-precision-rule: {mid} is APPROXIMATE without a precision_rule")
        # A recalled figure is an approximation whose "~" the validator enforces.
        if "USER_RECALLED_APPROXIMATE" in as_list(m.get("status")):
            if "APPROXIMATE" not in as_list(m.get("status")):
                r.error(f"recalled-not-approximate: {mid} is USER_RECALLED_APPROXIMATE but not APPROXIMATE")
            if m.get("approximation_qualifier_required") is not True:
                r.error(
                    f"recalled-qualifier-strippable: {mid} must set approximation_qualifier_required: true"
                )
            if "REPO_OR_PROJECT_ARTIFACT" in as_list(m.get("provenance")):
                r.error(f"recalled-claims-artifact: {mid} is recalled; it cannot also be artifact-sourced")
    r.check("metric-integrity", True)

    # Forbidden-metric narrowing by name must point at real, caveated metrics.
    for entry in metrics_doc.get("forbidden_metrics", []) or []:
        if not isinstance(entry, dict):
            continue
        for pmid in as_list(entry.get("permitted_metric_ids")):
            pm = next((m for m in metrics if m.get("metric_id") == pmid), None)
            if pm is None:
                r.error(f"forbidden-exemption-ref: {entry.get('category')} -> unknown metric {pmid}")
            elif pm.get("caveat_strippable") is not False or not pm.get("mandatory_caveat"):
                r.error(
                    f"forbidden-exemption-uncaveated: {pmid} is exempt from {entry.get('category')} "
                    "without a non-strippable caveat"
                )
    r.check("forbidden-metric-exemptions", True)

    # ---- 8. skills --------------------------------------------------------
    for s in skills:
        sid = s.get("skill_id")
        lvl = s.get("evidence_level")
        if lvl not in SKILL_LEVEL:
            r.error(f"skill-level-enum: {sid} ({s.get('name')}) -> invalid '{lvl}'")
        for p in as_list(s.get("provenance")):
            if p not in PROVENANCE:
                r.error(f"skill-provenance-enum: {sid} -> invalid '{p}'")
        for eid in as_list(s.get("evidence_ids")):
            if eid not in ev_ids:
                r.error(f"skill-evidence-ref: {sid} -> unknown evidence {eid}")
        for gid in as_list(s.get("related_gap_ids")):
            if gid not in gap_ids:
                r.error(f"skill-gap-ref: {sid} -> unknown gap {gid}")
        # WORK_PROJECT_VERIFIED requires at least one backing evidence item
        if lvl == "WORK_PROJECT_VERIFIED" and not as_list(s.get("evidence_ids")):
            r.error(
                f"skill-unbacked: {sid} ({s.get('name')}) is WORK_PROJECT_VERIFIED with no evidence_ids"
            )
        # PENDING / LEARNING must be excluded from resumes
        if lvl in {"PENDING_USER_CONFIRMATION"} and not s.get("excluded_from_resumes"):
            r.error(
                f"pending-skill-not-excluded: {sid} ({s.get('name')}) must set excluded_from_resumes: true"
            )
        if lvl == "LEARNING_NOT_RESUME_READY" and not s.get("excluded_from_resumes"):
            r.error(
                f"learning-skill-not-excluded: {sid} ({s.get('name')}) must set excluded_from_resumes: true"
            )
    r.check("skill-integrity", True)

    # ---- 9. prohibited claims --------------------------------------------
    for c in prohibited:
        cid = c["claim_id"]
        if c.get("status") != "PROHIBITED":
            r.error(f"dnc-status: {cid} must have status PROHIBITED")
        if not (c.get("text") or c.get("subject")):
            r.error(f"dnc-no-subject: {cid} has neither text nor subject")
        for eid in as_list(c.get("evidence_ids")):
            if eid not in ev_ids:
                r.error(f"dnc-evidence-ref: {cid} -> unknown evidence {eid}")
        for sid in as_list(c.get("skill_ids")):
            if sid not in sk_ids:
                r.error(f"dnc-skill-ref: {cid} -> unknown skill {sid}")
        for mid in as_list(c.get("metric_ids")):
            if mid not in m_ids:
                r.error(f"dnc-metric-ref: {cid} -> unknown metric {mid}")
        for xid in as_list(c.get("experience_ids")):
            if xid not in exp_ids:
                r.error(f"dnc-experience-ref: {cid} -> unknown experience {xid}")
        for gid in as_list(c.get("related_gap_ids")):
            if gid not in gap_ids:
                r.error(f"dnc-gap-ref: {cid} -> unknown gap {gid}")
    r.check("prohibited-claim-integrity", True)

    # ---- 10. gaps and questions ------------------------------------------
    for g in gaps:
        gid = g.get("gap_id")
        for eid in as_list(g.get("related_evidence_ids")):
            if eid not in ev_ids:
                r.error(f"gap-evidence-ref: {gid} -> unknown evidence {eid}")
        for sid in as_list(g.get("related_skill_ids")):
            if sid not in sk_ids:
                r.error(f"gap-skill-ref: {gid} -> unknown skill {sid}")
        for qid in as_list(g.get("open_question_ids")):
            if qid not in q_ids:
                r.error(f"gap-question-ref: {gid} -> unknown question {qid}")

    for q in open_qs:
        qid = q.get("question_id")
        uc = q.get("uncertainty_class")
        if uc not in UNCERTAINTY_CLASS:
            r.error(f"question-uncertainty-enum: {qid} -> invalid '{uc}'")
        if uc == "BLOCKING_FOR_ALL_USE" and q.get("blocks_finalization") is not True:
            r.error(
                f"question-blocking-mismatch: {qid} is BLOCKING_FOR_ALL_USE but blocks_finalization is not true"
            )
        for cid in as_list(q.get("gates_claim_ids")):
            if cid not in safe_ids:
                r.error(f"question-claim-ref: {qid} -> unknown claim {cid}")
    r.check("gap-and-question-integrity", True)

    # ---- 11. work authorization safety -----------------------------------
    wa = candidate.get("work_authorization", {})
    if wa.get("authorized_confirmed") is not False:
        r.error("work-auth: authorized_confirmed must be false unless the user sets it")
    if wa.get("system_may_infer_eligibility") is not False:
        r.error("work-auth: system_may_infer_eligibility must be false")
    if wa.get("sponsorship", {}).get("is_hard_requirement") is not False:
        r.error("work-auth: sponsorship.is_hard_requirement must be false")
    r.check("work-authorization-safety", True)

    # ---- 12. version consistency -----------------------------------------
    version = candidate.get("evidence_bank_version")
    for name, doc in (
        ("experiences.yaml", experiences_doc),
        ("projects.yaml", projects_doc),
        ("evidence.yaml", evidence_doc),
        ("skills.yaml", skills_doc),
        ("metrics.yaml", metrics_doc),
        ("claims.yaml", claims_doc),
        ("gaps.yaml", gaps_doc),
        ("open_questions.yaml", questions_doc),
    ):
        if doc.get("evidence_bank_version") != version:
            r.error(
                f"version-mismatch: {name} has {doc.get('evidence_bank_version')!r}, expected {version!r}"
            )
    r.check("version-consistency", True)

    # ---- 13. blocking questions ------------------------------------------
    blocking = [
        q["question_id"] for q in open_qs if q.get("uncertainty_class") == "BLOCKING_FOR_ALL_USE"
    ]
    r.check("no-blocking-for-all-use-questions", not blocking, f"blocking: {blocking}")

    return r


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the Career Evidence Bank.")
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="Directory containing the Evidence Bank YAML files.",
    )
    args = ap.parse_args()

    print(f"Validating Evidence Bank in: {args.data_dir}")
    print("-" * 68)

    report = validate(args.data_dir)

    if report.warnings:
        print(f"\nWARNINGS ({len(report.warnings)}):")
        for w in report.warnings:
            print(f"  ! {w}")

    if report.errors:
        print(f"\nFAILED ({len(report.errors)} error(s)):")
        for e in report.errors:
            print(f"  x {e}")
        print("-" * 68)
        print(f"RESULT: FAIL  ({report.checks_passed}/{report.checks_run} check groups passed)")
        return 1

    print(
        f"\nRESULT: PASS  ({report.checks_passed}/{report.checks_run} check groups passed, "
        f"{len(report.warnings)} warning(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
