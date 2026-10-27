"""Verify a batch of candidate employers and, when asked, add them to the registry.

    python scripts/expand_registry.py scripts/expansion/batch_2026_10.yaml            # verify, report
    python scripts/expand_registry.py scripts/expansion/batch_2026_10.yaml --apply --limit 50

The batch file is a reviewed INPUT, read only by this script. It is not a
config the application loads: once a company is verified and applied, it lives
in the company registry (the database), like any other company added since the
seed.

For every candidate, nothing about its careers source is assumed:

* A candidate names its board when it is known (``workday:`` a career-site
  URL, ``board:`` any board URL). Otherwise the script tries the candidate's
  name as a board token on Greenhouse, Ashby and Lever — the three providers
  whose tokens are derived from the company name — and nothing else.
* A board only counts if it ANSWERS and it is demonstrably this employer's:
  Greenhouse returns the board's own name; on the other providers the
  postings themselves must name the company. A board that answers but cannot
  be tied to the employer is ``VERIFY_LATER``, never monitored.
* ``careers:`` (a careers page) is checked with the same detector the Sources
  page uses. A page that loads and links no supported board is
  ``VERIFIED_UNSUPPORTED`` and is kept ``MANUAL_ONLY`` so coverage shows the gap.

Statuses written to the report:

  VERIFIED_MONITORED     board answered and belongs to the employer → ACTIVE
  VERIFIED_UNSUPPORTED   careers page checked; no supported board → MANUAL_ONLY
  VERIFY_LATER           a board answered but could not be tied to the employer,
                         or nothing could be checked → VERIFY_LATER
  ALREADY_IN_REGISTRY    the seed or the registry already covers this board
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.connectors.http import ConnectorHTTPError, HttpClient  # noqa: E402
from app.connectors.workday import parse_board_identifier  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.services import monitored_companies  # noqa: E402
from app.services.source_detection import board_from_url, detect_source  # noqa: E402
from app.services.text import normalize_company  # noqa: E402

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board"
LEVER = "https://api.lever.co/v0/postings"


@dataclass
class Verdict:
    name: str
    status: str
    provider: str | None = None
    identifier: str | None = None
    postings: int | None = None
    evidence: str = ""
    careers_url: str | None = None
    category: str | None = None
    region: str | None = None
    tried: list[str] = field(default_factory=list)


def _names(name: str, aliases: list[str]) -> list[str]:
    out = []
    for value in [name, *aliases]:
        norm = normalize_company(value)
        if norm and norm not in out:
            out.append(norm)
    return out


def _mentions(text: str, names: list[str]) -> bool:
    lowered = (text or "").lower()
    for name in names:
        if len(name) < 4:
            # A three-letter name appears by accident everywhere; it must match
            # as a whole word.
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                return True
        elif name in lowered or name.replace(" ", "") in lowered.replace(" ", ""):
            return True
    return False


def _slugs(name: str, explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    words = base.split()
    out = ["".join(words), "-".join(words)]
    return [s for i, s in enumerate(out) if s and s not in out[:i]]


class Verifier:
    def __init__(self) -> None:
        self.http = HttpClient(min_interval=0.35, max_retries=1)

    def close(self) -> None:
        self.http.close()

    # -- providers ---------------------------------------------------------
    def greenhouse(self, token: str, names: list[str]) -> tuple[bool, int | None, str] | None:
        try:
            board = self.http.get_json(f"{GREENHOUSE}/{token}")
        except ConnectorHTTPError:
            return None
        board_name = str((board or {}).get("name") or "")
        try:
            jobs = self.http.get_json(f"{GREENHOUSE}/{token}/jobs")
            count = len(jobs.get("jobs") or []) if isinstance(jobs, dict) else None
        except ConnectorHTTPError:
            count = None
        owned = bool(board_name) and _mentions(normalize_company(board_name), names)
        return owned, count, f"Greenhouse board name {board_name!r}"

    def ashby(self, slug: str, names: list[str]) -> tuple[bool, int | None, str] | None:
        try:
            payload = self.http.get_json(f"{ASHBY}/{slug}")
        except ConnectorHTTPError:
            return None
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        bodies = " ".join(str(j.get("descriptionPlain") or "") for j in jobs[:8])
        return _mentions(bodies, names), len(jobs), f"{len(jobs)} postings; name in bodies"

    def lever(self, slug: str, names: list[str]) -> tuple[bool, int | None, str] | None:
        try:
            payload = self.http.get_json(f"{LEVER}/{slug}", params={"mode": "json"})
        except ConnectorHTTPError:
            return None
        if not isinstance(payload, list):
            return None
        bodies = " ".join(
            str(p.get("descriptionPlain") or "") + str(p.get("additionalPlain") or "")
            for p in payload[:8]
        )
        return _mentions(bodies, names), len(payload), f"{len(payload)} postings; name in bodies"

    def workday(self, identifier: str, names: list[str]) -> tuple[bool, int | None, str] | None:
        board = parse_board_identifier(identifier)
        if board is None:
            return None
        try:
            first = self.http.post_json(
                f"{board.api_base}/jobs",
                body={"appliedFacets": {}, "limit": 3, "offset": 0, "searchText": ""},
            )
        except ConnectorHTTPError:
            return None
        postings = first.get("jobPostings") or [] if isinstance(first, dict) else []
        total = first.get("total") if isinstance(first, dict) else None
        text = ""
        for posting in postings[:2]:
            try:
                detail = self.http.get_json(f"{board.api_base}{posting.get('externalPath')}")
            except ConnectorHTTPError:
                continue
            info = detail.get("jobPostingInfo") if isinstance(detail, dict) else {}
            text += " " + str((info or {}).get("jobDescription") or "")
        owned = _mentions(text, names) or _mentions(board.tenant.replace("-", " "), names)
        return owned, total if isinstance(total, int) else None, f"{total} listed; name in bodies or tenant"

    # -- one candidate -----------------------------------------------------
    def verify(self, candidate: dict[str, Any]) -> Verdict:
        name = str(candidate["name"])
        names = _names(name, list(candidate.get("aliases") or []))
        verdict = Verdict(
            name=name,
            status="VERIFY_LATER",
            careers_url=candidate.get("careers"),
            category=candidate.get("category"),
            region=candidate.get("region"),
        )

        probes: list[tuple[str, str]] = []
        if candidate.get("workday"):
            parsed = board_from_url(str(candidate["workday"]))
            if parsed:
                probes.append(parsed)
        if candidate.get("board"):
            parsed = board_from_url(str(candidate["board"]))
            if parsed:
                probes.append(parsed)
        if not probes and not candidate.get("careers_only"):
            for slug in _slugs(name, list(candidate.get("slugs") or [])):
                for provider in candidate.get("try") or ("GREENHOUSE", "ASHBY", "LEVER"):
                    probes.append((provider, slug))

        found_unowned: tuple[str, str, str] | None = None
        for provider, identifier in probes:
            verdict.tried.append(f"{provider}:{identifier}")
            check = {
                "GREENHOUSE": self.greenhouse,
                "ASHBY": self.ashby,
                "LEVER": self.lever,
                "WORKDAY": self.workday,
            }.get(provider)
            if check is None:
                continue
            result = check(identifier, names)
            if result is None:
                continue
            owned, count, evidence = result
            if owned:
                verdict.status = "VERIFIED_MONITORED"
                verdict.provider, verdict.identifier = provider, identifier
                verdict.postings, verdict.evidence = count, evidence
                return verdict
            found_unowned = found_unowned or (provider, identifier, evidence)

        if found_unowned:
            verdict.provider, verdict.identifier = found_unowned[0], found_unowned[1]
            verdict.evidence = f"answered, but not tied to {name}: {found_unowned[2]}"
            return verdict

        if candidate.get("careers"):
            detection = detect_source(str(candidate["careers"]), company_name=name)
            if detection.confidence == "UNKNOWN" and detection.provider is None and "not currently supported" in detection.detail:
                verdict.status = "VERIFIED_UNSUPPORTED"
                verdict.evidence = "careers page read; it links no supported board"
            else:
                verdict.evidence = f"careers page: {detection.confidence} — {detection.detail[:140]}"
        else:
            verdict.evidence = "no board answered for the tried tokens"
        return verdict


def _already(session: Any, verdict: Verdict) -> str | None:
    names = set(_names(verdict.name, []))
    for entry in monitored_companies.registry_entries(session):
        if verdict.identifier and monitored_companies.board_key(
            entry.ats_type, entry.ats_identifier
        ) == monitored_companies.board_key(verdict.provider or "", verdict.identifier):
            return entry.company_name
        entry_names = {normalize_company(entry.company_name)}
        if names & entry_names and (entry.collectable or not verdict.identifier):
            return entry.company_name
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("batch", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write verified results to the registry")
    parser.add_argument("--limit", type=int, help="Apply at most this many NEW monitored companies")
    parser.add_argument("--report", type=Path, help="Write the verification report (JSON) here")
    parser.add_argument("--from-report", type=Path, help="Apply a saved report instead of re-verifying")
    args = parser.parse_args(argv)

    batch = yaml.safe_load(args.batch.read_text(encoding="utf-8")) or {}
    candidates = list(batch.get("candidates") or [])

    if args.from_report:
        verdicts = [Verdict(**v) for v in json.loads(args.from_report.read_text(encoding="utf-8"))]
    else:
        verifier = Verifier()
        verdicts = []
        started = time.monotonic()
        try:
            for i, candidate in enumerate(candidates, 1):
                verdict = verifier.verify(candidate)
                verdicts.append(verdict)
                print(
                    f"[{i:3}/{len(candidates)}] {verdict.status:20} {verdict.name:32} "
                    f"{verdict.provider or '':15} {verdict.identifier or ''}",
                    flush=True,
                )
        finally:
            verifier.close()
        print(f"\nVerified {len(verdicts)} candidates in {time.monotonic() - started:.0f}s.")
    if args.report:
        args.report.write_text(json.dumps([asdict(v) for v in verdicts], indent=1), encoding="utf-8")

    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
    print("Results:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    if not args.apply:
        print("Nothing written. Re-run with --apply to add verified companies to the registry.")
        return 0

    holds = {str(c["name"]): str(c["review_hold"]) for c in candidates if c.get("review_hold")}
    for verdict in verdicts:
        if verdict.name in holds and verdict.status == "VERIFIED_MONITORED":
            # Verified automatically, then held back by a person who looked at
            # the board: the automatic check proved the board answers and names
            # the company, not that it is the company meant.
            verdict.status = "VERIFY_LATER"
            verdict.evidence = f"held at review: {holds[verdict.name]} ({verdict.evidence})"

    added = kept = skipped = 0
    with session_scope() as session:
        for verdict in verdicts:
            existing = _already(session, verdict)
            if existing:
                skipped += 1
                continue
            note = f"Expansion batch {args.batch.stem}: {verdict.evidence}"[:900]
            if verdict.status == "VERIFIED_MONITORED":
                if args.limit is not None and added >= args.limit:
                    continue
                assert verdict.provider and verdict.identifier
                outcome = monitored_companies.monitor(
                    session,
                    company_name=verdict.name,
                    ats_type=verdict.provider,
                    ats_identifier=verdict.identifier,
                    careers_url=verdict.careers_url,
                    registry_source="CURATED_EXPANSION",
                    notes=note,
                )
                added += int(outcome.ok)
            else:
                outcome = monitored_companies.record_unsupported(
                    session,
                    company_name=verdict.name,
                    careers_url=verdict.careers_url,
                    ats_type=verdict.provider,
                    monitoring_status=(
                        "MANUAL_ONLY" if verdict.status == "VERIFIED_UNSUPPORTED" else "VERIFY_LATER"
                    ),
                    source_verification="LIKELY_REVIEW" if verdict.provider else "UNKNOWN",
                    registry_source="CURATED_EXPANSION",
                    notes=note,
                )
                kept += int(outcome.ok)
    print(
        f"Applied: {added} monitored, {kept} kept as gaps (manual-only / verify later), "
        f"{skipped} already in the registry."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
