"""Audit every canonical safe claim against the X-Y-Z standard.

Reports, never rewrites. The point is to find the bullets that state a task
without stating why it mattered, and then to ask the only question that
matters: **does the Evidence Bank already hold the missing Y?** If it does, the
wording can be improved by a human edit to ``claims.yaml``. If it does not, the
bullet stays exactly as it is — a conservative true sentence beats an invented
outcome, every time.

Usage::

    ./.venv/Scripts/python.exe scripts/audit_bullet_quality.py
    ./.venv/Scripts/python.exe scripts/audit_bullet_quality.py --weak-only
    ./.venv/Scripts/python.exe scripts/audit_bullet_quality.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evidence.service import get_evidence_bank  # noqa: E402
from app.services.bullet_quality import get_bullet_quality_scorer  # noqa: E402

#: Audit verdicts. The first three come from the scorer; the fourth is this
#: script's own judgement about whether an improvement is even possible.
NEEDS_EVIDENCE = "NEEDS_EVIDENCE_BEFORE_IMPROVEMENT"


def _available_outcome(bank: Any, claim: Any) -> list[str]:
    """Y material the Bank already holds for this claim's evidence.

    A weak bullet backed by a recorded metric or a recorded outcome can be
    improved honestly. One backed by neither cannot.
    """
    found: list[str] = []
    for metric_id in claim.raw.get("metric_ids") or ():
        metric = bank.metrics.get(metric_id)
        if metric:
            allowed = (metric.get("allowed_formulations") or [""])[0]
            found.append(f"{metric_id}: {allowed}")
    for evidence_id in claim.evidence_ids:
        record = bank.evidence.get(evidence_id)
        if record is None:
            continue
        for metric_id in record.metric_ids:
            metric = bank.metrics.get(metric_id)
            if metric and not any(m.startswith(metric_id) for m in found):
                allowed = (metric.get("allowed_formulations") or [""])[0]
                found.append(f"{metric_id}: {allowed}")
        for outcome in record.raw.get("observed_outcomes") or ():
            found.append(f"{evidence_id}: {outcome}")
    return found


def audit() -> list[dict[str, Any]]:
    bank = get_evidence_bank()
    scorer = get_bullet_quality_scorer(bank)

    rows: list[dict[str, Any]] = []
    for claim in bank.safe_claims.values():
        text, downgraded = bank.usable_claim_text(claim.claim_id)
        rendered = text or claim.text
        quality = scorer.score(rendered, claim=claim)
        outcomes = _available_outcome(bank, claim)

        verdict = quality.verdict
        if quality.is_weak and not outcomes:
            verdict = NEEDS_EVIDENCE

        rows.append(
            {
                "claim_id": claim.claim_id,
                "verdict": verdict,
                "scorer_verdict": quality.verdict,
                "score": quality.score,
                "flags": quality.flags,
                "gated": claim.gated,
                "renders_as_project_level": downgraded,
                "strength": claim.strength,
                "role_families": list(claim.role_families),
                "text": rendered,
                "available_outcome_material": outcomes,
            }
        )
    rows.sort(key=lambda r: (r["score"], r["claim_id"]))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weak-only", action="store_true", help="only sub-acceptable bullets")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    rows = audit()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    shown = [r for r in rows if r["score"] < 0.9] if args.weak_only else rows
    for row in shown:
        flags = ",".join(row["flags"]) or "-"
        print(f"{row['claim_id']:8} {row['verdict']:34} {row['score']:.2f}  {flags}")
        print(f"         {row['text'][:150]}")
        if row["available_outcome_material"]:
            print(f"         available Y: {row['available_outcome_material'][0][:120]}")
        print()

    print("-" * 72)
    for verdict, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{count:4}  {verdict}")
    print(f"{len(rows):4}  TOTAL")

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
