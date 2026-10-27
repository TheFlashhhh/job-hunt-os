"""Cheap, read-only ATS verification for watchlist candidates.

Authorized for the watchlist-validation step only. This script:

* issues ONE list request per (company, ATS) candidate token
* ingests nothing — no Job rows, no analysis, no database writes at all
* records company, connector, endpoint category, status, token, verified_at

It exists so board tokens are *verified* rather than guessed. A wrong token
silently collects nothing, which is worse than a loud failure.

    python scripts/verify_ats.py candidates.json
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.connectors import get_connector  # noqa: E402
from app.connectors.base import SourceConfig  # noqa: E402
from app.connectors.http import HttpClient  # noqa: E402

#: Spacing between requests. Deliberately generous: this is a courtesy scan
#: across many unrelated employers, not a collection run.
INTERVAL_SECONDS = 1.0


def verify(company: str, ats_type: str, token: str, client: HttpClient) -> dict:
    connector = get_connector(ats_type, client=client)
    config = SourceConfig(
        company_name=company,
        ats_type=ats_type,
        ats_identifier=token,
        is_fixture=False,
        active=False,
    )
    health = connector.health_check(config)
    return {
        "company": company,
        "connector": connector.connector_id,
        "endpoint_category": connector.capability.value,
        "board_token": token,
        "reachable": health.reachable,
        "capability_failure": health.capability_failure,
        "status": health.status,
        "postings_visible": health.postings_visible,
        "detail": health.detail[:160],
        "verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "candidates.json"
    candidates = json.loads(path.read_text(encoding="utf-8"))

    results: list[dict] = []
    client = HttpClient(timeout=12.0, max_retries=1, min_interval=INTERVAL_SECONDS)
    try:
        for entry in candidates:
            company = entry["company"]
            for ats_type, token in entry["try"]:
                row = verify(company, ats_type, token, client)
                results.append(row)
                mark = "OK " if row["reachable"] else "-- "
                count = row["postings_visible"]
                print(
                    f"{mark}{company:28} {ats_type:16} {token:24} "
                    f"{row['status']:22} {count if count is not None else ''}"
                )
                if row["reachable"]:
                    break  # first working token wins; stop probing this company
                time.sleep(0.2)
    finally:
        client.close()

    # Accumulate across batches: a later run must not erase earlier evidence.
    out = REPO_ROOT / "context" / "ats_verification.json"
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    merged = {(r["company"], r["board_token"], r["connector"]): r for r in existing}
    for row in results:
        merged[(row["company"], row["board_token"], row["connector"])] = row
    out.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")
    reachable = [r for r in results if r["reachable"]]
    print(f"\n{len(reachable)}/{len({r['company'] for r in results})} companies verified")
    print(f"total requests: {client.stats.requests}  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
