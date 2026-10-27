"""Derived ``evidence_digest`` generator.

**Derived only. Never authoritative. Never hand-edited.** Regenerated whenever
``evidence_bank_version`` changes. Every entry carries its source Evidence IDs
so the full record can always be retrieved from ``data/*.yaml``.

Purpose: prompt efficiency, caching, retrieval efficiency, lower repeated token
usage. It is a *projection* of the Bank, not a replacement for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import GENERATED_DIR
from app.evidence.service import EvidenceBankService, get_evidence_bank

DIGEST_FORMAT_VERSION = "1"
DEFAULT_DIGEST_PATH = GENERATED_DIR / "evidence_digest.json"


def build_digest(bank: EvidenceBankService | None = None) -> dict[str, Any]:
    """Build the compact projection used as a cached prompt prefix."""
    bank = bank or get_evidence_bank()

    evidence_entries = []
    for rec in sorted(bank.evidence.values(), key=lambda r: r.evidence_id):
        raw = rec.raw
        evidence_entries.append(
            {
                "id": rec.evidence_id,
                "org": rec.organization_or_project,
                "category": rec.category,
                "summary": _summarize_actions(raw.get("actions")),
                "tech": list(rec.technologies),
                "keywords": list(rec.keywords_supported),
                "ownership": rec.personal_ownership,
                "ownership_gated": rec.ownership_gated,
                "project_implemented": rec.project_implemented,
                "confidence": rec.confidence,
                "resume_ready": rec.resume_ready,
                "role_relevance": {
                    k: v for k, v in rec.role_family_relevance.items() if v in {"HIGH", "MEDIUM"}
                },
                "safe_claims": list(rec.safe_claim_ids),
                "metrics": list(rec.metric_ids),
                "limitations": _first_n(raw.get("limitations"), 2),
                "not_supported": _first_n(raw.get("not_supported"), 3),
            }
        )

    skill_entries = [
        {
            "id": s.skill_id,
            "name": s.name,
            "level": s.evidence_level,
            "weight": s.fit_weight,
            "resume_usable": s.resume_usable,
            "evidence": list(s.evidence_ids),
        }
        for s in sorted(bank.skills.values(), key=lambda s: s.skill_id)
    ]

    claim_entries = [
        {
            "id": c.claim_id,
            "text": c.text,
            "evidence": list(c.evidence_ids),
            "families": list(c.role_families),
            "strength": c.strength,
            "gated": c.gated,
            "project_level_alternative": c.project_level_alternative,
        }
        for c in sorted(bank.safe_claims.values(), key=lambda c: c.claim_id)
    ]

    prohibited_entries = [
        {
            "id": p.claim_id,
            "subject": p.subject or p.raw.get("text", ""),
            "rule": p.rule or p.raw.get("correct_boundary", ""),
        }
        for p in sorted(bank.prohibited_claims.values(), key=lambda p: p.claim_id)
    ]

    project_entries = [
        {
            "id": pid,
            "name": p.get("name") or p.get("project_name", pid),
            "status": p.get("project_status"),
            "resume_ready": bool(p.get("resume_ready")),
            "resume_auto_select": bool(p.get("resume_auto_select", p.get("resume_ready"))),
            "tech": _first_n(p.get("technologies"), 12),
        }
        for pid, p in sorted(bank.projects.items())
    ]

    metric_entries = [
        {
            "id": mid,
            "value": m.get("value"),
            "unit": m.get("unit"),
            "status": m.get("status"),
            "allowed": _first_n(m.get("allowed_formulations"), 1),
            "mandatory_caveat": m.get("mandatory_caveat"),
        }
        for mid, m in sorted(bank.metrics.items())
    ]

    return {
        "digest_format_version": DIGEST_FORMAT_VERSION,
        "evidence_bank_version": bank.version,
        "authoritative_source": "data/*.yaml",
        "derived": True,
        "warning": (
            "DERIVED ARTIFACT. Not authoritative. Never hand-edit. "
            "Regenerate with `jobhunt evidence digest` when the Evidence Bank changes."
        ),
        "counts": bank.summary(),
        "candidate": {
            "name": bank.candidate.get("name"),
            "location": bank.candidate.get("location"),
        },
        "work_authorization": bank.work_authorization,
        "evidence": evidence_entries,
        "skills": skill_entries,
        "safe_claims": claim_entries,
        "prohibited_claims": prohibited_entries,
        "projects": project_entries,
        "metrics": metric_entries,
        "gaps": [
            {
                "id": g.get("gap_id"),
                "gap": g.get("gap"),
                "impact": g.get("impact"),
            }
            for g in bank.gaps
        ],
        "known_absences": [
            a.get("name") if isinstance(a, dict) else a for a in bank.known_absences
        ],
    }


def write_digest(path: Path | None = None, bank: EvidenceBankService | None = None) -> Path:
    """Write the digest to disk and return the path."""
    target = path or DEFAULT_DIGEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = build_digest(bank)
    target.write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def load_digest(path: Path | None = None) -> dict[str, Any]:
    """Load the digest, regenerating it if missing or stale."""
    target = path or DEFAULT_DIGEST_PATH
    bank = get_evidence_bank()
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if data.get("evidence_bank_version") == bank.version:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    write_digest(target, bank)
    return json.loads(target.read_text(encoding="utf-8"))


def _summarize_actions(actions: Any, limit: int = 240) -> str:
    if not actions:
        return ""
    text = actions if isinstance(actions, str) else " ".join(str(a) for a in actions)
    text = " ".join(text.split())
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _first_n(value: Any, n: int) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in list(value)[:n]]
