"""Command-line entry point.

Designed for Windows Task Scheduler: every command is idempotent, resumable and
catch-up safe. The FastAPI server does not need to be running.

    python -m app.cli evidence validate
    python -m app.cli evidence digest
    python -m app.cli db upgrade
    python -m app.cli import file path/to/jobs.json
    python -m app.cli analyze --all
    python -m app.cli report today
    python -m app.cli demo seed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import REPO_ROOT, get_settings
from app.db import session_scope
from app.evidence.digest import write_digest
from app.evidence.service import EvidenceBankError, get_evidence_bank
from app.models.enums import SearchLane
from app.models.ingestion import Job


def _print_header(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def cmd_evidence_validate(_args: argparse.Namespace) -> int:
    try:
        bank = get_evidence_bank()
    except EvidenceBankError as exc:
        print(f"FAIL: {exc}")
        return 1
    _print_header("Evidence Bank")
    for key, value in bank.summary().items():
        print(f"  {key:26} {value}")
    print("\nRESULT: PASS")
    return 0


def cmd_evidence_digest(_args: argparse.Namespace) -> int:
    path = write_digest()
    size_kb = path.stat().st_size / 1024
    print(f"Wrote {path} ({size_kb:.1f} KB)")
    print("This artifact is DERIVED. data/*.yaml remains authoritative.")
    return 0


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


def cmd_db_upgrade(_args: argparse.Namespace) -> int:
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    print("Database upgraded to head.")
    return 0


def cmd_db_status(_args: argparse.Namespace) -> int:
    settings = get_settings()
    print(f"Database: {settings.jobhunt_db_url}")
    with session_scope() as session:
        total = session.execute(select(Job)).scalars().all()
        real = [j for j in total if not j.is_demo]
        print(f"Jobs: {len(real)} real, {len(total) - len(real)} demo")
        for lane in SearchLane:
            count = sum(1 for j in real if j.search_lane == lane.value)
            if count:
                print(f"  {lane.value:32} {count}")
    return 0


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def cmd_import_file(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"No such file: {path}")
        return 1
    payload = path.read_text(encoding="utf-8")

    from app.services.import_service import ManualImportService

    with session_scope() as session:
        service = ManualImportService(session)
        result = (
            service.import_csv(payload)
            if path.suffix.lower() == ".csv"
            else service.import_json(payload)
        )
        print(
            f"Imported: {result.created} created, {result.updated} updated, "
            f"{result.unchanged} unchanged."
        )
        for error in result.errors:
            print(f"  ERROR {error}")
    return 0 if not result.errors else 2


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def cmd_analyze(args: argparse.Namespace) -> int:
    from app.services.pipeline import JobPipeline

    with session_scope() as session:
        stmt = select(Job).where(Job.is_demo.is_(False))
        if args.lane:
            stmt = stmt.where(Job.search_lane == args.lane)
        jobs = list(session.execute(stmt).scalars().all())

        pipeline = JobPipeline(session)
        analyzed = skipped = 0
        for job in jobs:
            if not args.force and job.last_analyzed_hash == job.normalized_content_hash:
                skipped += 1
                continue
            pipeline.analyze(job)
            analyzed += 1

        print(f"Analyzed {analyzed}, skipped {skipped} unchanged (no LLM spend on those).")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def cmd_report_today(_args: argparse.Namespace) -> int:
    from app.services.dashboard import DashboardService

    with session_scope() as session:
        service = DashboardService(session)
        kpis = service.kpis()
        panel = service.today_panel()

        _print_header("KPIs")
        for key, value in kpis.as_dict().items():
            print(f"  {key:24} {value}")

        _print_header(f"Today — {panel.total_actions} action(s)")
        for label, items in [
            ("Due today", panel.due_today),
            ("Closing soon", panel.due_soon),
            ("Hidden fits to review", panel.hidden_fits_to_review),
            ("High priority, unapplied", panel.high_priority_unapplied),
            ("Authorization questions", panel.authorization_questions),
        ]:
            print(f"\n{label} ({len(items)})")
            for job in items[:8]:
                deadline = f" [due {job.deadline}]" if job.deadline else ""
                print(f"  - {job.title} @ {job.company_name}{deadline}")
            if not items:
                print("  (none)")
    return 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


def cmd_demo_seed(_args: argparse.Namespace) -> int:
    from app.services.demo import seed_demo_data

    with session_scope() as session:
        count = seed_demo_data(session)
    print(f"Seeded {count} DEMO jobs (is_demo=True — excluded from all funnel analytics).")
    return 0


def cmd_demo_bootstrap(_args: argparse.Namespace) -> int:
    """Migrate, then fill an EMPTY database from the synthetic demo bank."""
    from app.evidence.service import get_evidence_bank
    from app.services.demo_bootstrap import DemoBootstrapRefused, bootstrap_demo

    # Checked before the migration, so a real install's database is not even
    # opened for writing.
    if not get_evidence_bank().is_synthetic_demo:
        print("Refused: data/candidate.yaml is not the synthetic demo bank.")
        return 1
    cmd_db_upgrade(_args)
    try:
        with session_scope() as session:
            result = bootstrap_demo(session)
    except DemoBootstrapRefused as exc:
        print(f"Refused: {exc}")
        return 1
    print(
        f"Demo ready: {result.jobs} synthetic jobs, {result.applications} applications, "
        f"{result.contacts} contact. Start the backend and frontend to browse it."
    )
    return 0


def cmd_demo_clear(_args: argparse.Namespace) -> int:
    from app.services.demo import clear_demo_data

    with session_scope() as session:
        count = clear_demo_data(session)
    print(f"Removed {count} demo jobs.")
    return 0


# ---------------------------------------------------------------------------
# purge — targeted deletion, dry-run by default
# ---------------------------------------------------------------------------


def cmd_job_purge(args: argparse.Namespace) -> int:
    """Delete one job and its dependents. Requires --yes to actually delete."""
    from app.services.purge import plan_purge, purge_job

    with session_scope() as session:
        plan = plan_purge(session, args.job_id)
        if not plan.ok:
            print(f"Nothing to do: {plan.blocked_reason}")
            return 1

        _print_header("PURGE PLAN")
        print(plan.describe())

        if not args.yes:
            print("\nDry run. Nothing was deleted. Re-run with --yes to execute.")
            return 0

        purge_job(session, args.job_id)

    print(f"\nDeleted {plan.total_rows} rows.")
    return 0


# ---------------------------------------------------------------------------
# schedule — the local, honest answer to "when does this run?"
# ---------------------------------------------------------------------------


def schedule_plan() -> dict[str, Any]:
    """What a Windows Task Scheduler entry for collection would look like.

    Returned as data so the browser can show the same thing the terminal does.
    Building the plan registers nothing.
    """
    from app.config import load_yaml_config

    policy = load_yaml_config("collection_policy.yaml")
    task = policy.get("windows_task") or {}
    cadence = (policy.get("cadences") or {}).get("STRUCTURED_ATS_REFRESH") or {}
    interval = float(cadence.get("interval_hours") or 6)
    task_name = str(task.get("task_name") or "JobHuntOS-StructuredCollection")

    # The task points at a wrapper script rather than at python directly.
    # schtasks takes ONE quoted /TR value, so an inner quoted interpreter path
    # would nest quotes and fail; the wrapper also derives the repository
    # location from its own path instead of baking this machine's into a task.
    script = REPO_ROOT / "scripts" / "run_collection.cmd"
    command = str(script)
    register = (
        f'schtasks /Create /TN "{task_name}" '
        f'/TR "\\"{script}\\"" '
        f"/SC HOURLY /MO {int(interval)} "
        f'/ST {task.get("start_time", "07:00")} /F'
    )

    return {
        "task_name": task_name,
        "interval_hours": interval,
        "command": command,
        "register_command": register,
        "notes": [
            "Job Hunt OS is a local application. Nothing collects while this machine "
            "is off, and no in-process scheduler can change that — which is why there "
            "is not one.",
            "Task Scheduler catches up on the next wake-up. A repeat poll of an "
            "unchanged board writes nothing, so a missed window costs nothing.",
            "Structured ATS collection is free. Broad discovery is a separate, paused, "
            "potentially paid channel and is not part of this task.",
            "Run Collection Now in the browser stays available either way.",
        ],
    }


def cmd_schedule(args: argparse.Namespace) -> int:
    plan = schedule_plan()
    _print_header("LOCAL COLLECTION SCHEDULE")
    print(f"  Task name       {plan['task_name']}")
    print(f"  Every           {plan['interval_hours']} hours")
    print(f"  Runs            {plan['command']}")
    for note in plan["notes"]:
        print(f"    - {note}")

    print("")
    print("  Register it with:")
    print("")
    print(f"    {plan['register_command']}")
    print("")

    if not args.register:
        print("  Nothing was registered. Re-run with --register to create the task,")
        print("  or copy the command above and run it yourself.")
        return 0

    import platform
    import subprocess

    if platform.system() != "Windows":
        print("  --register only works on Windows. The command above is the whole task.")
        return 1

    answer = input(f"  Create scheduled task {plan['task_name']!r} now? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("  Not registered.")
        return 0

    completed = subprocess.run(  # noqa: S602 — user-confirmed, command printed above
        str(plan["register_command"]), shell=True, check=False
    )
    if completed.returncode == 0:
        print(f"  Registered {plan['task_name']}.")
        return 0
    print(f"  schtasks exited {completed.returncode}. Nothing was scheduled.")
    return 1


# ---------------------------------------------------------------------------
# reclassify — bring stored jobs up to the current functional classifier
# ---------------------------------------------------------------------------


def cmd_reclassify(args: argparse.Namespace) -> int:
    """Recompute what stored jobs ARE, without touching what was DONE about them.

    Writes the functional columns only. Applications, resume versions, cover
    letters, contacts, referrals, CRM history, raw source records and
    provenance are all untouched — a reclassification changes the system's
    opinion of a job, never the user's record of it.

    Audit first. `scripts/audit_functional_classifier.py` reports what this
    would change without writing anything, and that is the intended order.
    """
    from app.services.functional_classifier import FUNCTIONAL_CLASSIFIER_VERSION
    from app.services.functional_screening import reclassify

    _print_header(f"Functional reclassification — classifier {FUNCTIONAL_CLASSIFIER_VERSION}")
    if args.only_stale:
        print("  Only jobs not already on this classifier version.")
    else:
        print("  Every non-demo job, including ones already on this version.")

    with session_scope() as session:
        counts = reclassify(
            session, only_stale=args.only_stale, limit=args.limit or None
        )

    for key, value in counts.items():
        print(f"  {key:22} {value}")
    print("\n  Applications, resumes, cover letters and CRM state were not touched.")
    return 0


# ---------------------------------------------------------------------------
# collect — structured discovery
# ---------------------------------------------------------------------------


def cmd_collect(args: argparse.Namespace) -> int:
    """Run structured collection.

    Safe by default: no real company is polled unless it has been approved with
    ``active: true`` in ``companies.yaml``. Fixture boards must be asked for
    explicitly.
    """
    from app.services import collection_runner
    from app.services.collection import CollectionService
    from app.services.watchlist import load_watchlist

    watchlist = load_watchlist()
    for problem in watchlist.problems:
        print(f"  WARNING {problem}")

    # One sweep at a time, shared with the browser and with Task Scheduler.
    # A dry run, a fixtures-only run and a single-company run take no lock:
    # none of them is the sweep the lock exists to protect, and refusing them
    # because an unrelated collection is running would be obstruction.
    takes_lock = not (args.dry_run or args.fixtures or args.company or args.force)
    if takes_lock:
        claimed, refusal = collection_runner.claim(
            triggered_by="cli", lane=args.lane, company_name=args.company
        )
        if not claimed:
            print("")
            print(f"  {refusal.detail}")
            print("  Re-run with --force to collect anyway.")
            return 2

    import time

    summary = None
    error: str | None = None
    started = time.monotonic()
    try:
        with session_scope() as session:
            summary = CollectionService(session, triggered_by="cli").collect_watchlist(
                lane=args.lane,
                include_fixtures=args.include_fixtures,
                only_fixtures=args.fixtures,
                company_name=args.company,
                ats_type=args.connector,
                limit=args.max_records,
                analyze=not args.no_analyze,
                dry_run=args.dry_run,
                watchlist=watchlist,
                include_experimental=args.include_experimental,
            )
    except Exception as exc:  # noqa: BLE001 — the lock must be released either way
        error = str(exc)
        raise
    finally:
        if takes_lock:
            collection_runner.release(
                triggered_by="cli",
                summary=summary.as_dict() if summary is not None else None,
                error=error,
            )

    _print_header("COLLECTION")
    for note in summary.skipped:
        print(f"  {note}")

    for outcome in summary.outcomes:
        marker = "ok  " if outcome.ok else "FAIL"
        fixture = " [FIXTURE]" if outcome.config.is_fixture else ""
        print(
            f"  {marker} {outcome.config.company_name}{fixture}: "
            f"{outcome.records_valid}/{outcome.records_received} valid, "
            f"new={outcome.jobs_new} changed={outcome.jobs_changed} "
            f"unchanged={outcome.jobs_unchanged} failed={outcome.jobs_failed}"
        )
        if args.verbose and outcome.http_stats:
            print(f"       http: {outcome.http_stats}")
        for error in outcome.errors[:3]:
            print(f"       {error}")

    totals = summary.as_dict()
    elapsed = time.monotonic() - started
    print(
        f"\n  STATUS {totals['status']} — {totals['companies']} company(ies), "
        f"{totals['records_received']} postings listed, "
        f"{totals['jobs_new']} new, {totals['jobs_changed']} changed, "
        f"{totals['jobs_unchanged']} unchanged."
    )
    slowest = sorted(summary.outcomes, key=lambda o: o.duration_seconds, reverse=True)[:3]
    slow_text = ", ".join(f"{o.config.company_name} {o.duration_seconds:.0f}s" for o in slowest)
    print(f"  Took {elapsed:.0f}s" + (f" (slowest: {slow_text})" if slowest else "") + ".")
    if totals["capability_failures"]:
        print(
            "  Capability unavailable (per-company setting, not an outage): "
            + ", ".join(totals["capability_failures"])
        )
    print("  Structured public sources cost $0.")
    return 0 if summary.status != "FAILED" else 1


def cmd_source_check(args: argparse.Namespace) -> int:
    """One cheap read per configured source. Ingests nothing."""
    from app.services.collection import CollectionService

    with session_scope() as session:
        rows = CollectionService(session).check_sources(include_fixtures=not args.no_fixtures)

    _print_header("SOURCE HEALTH")
    if not rows:
        print("  No sources configured. data/config/companies.yaml is empty by design.")
        return 0

    for row in rows:
        health = row.get("health", {})
        state = "OK" if health.get("reachable") else "UNAVAILABLE"
        if health.get("capability_failure"):
            state = "NOT_PUBLIC"
        flags = []
        if row["is_fixture"]:
            flags.append("FIXTURE")
        if not row["active"]:
            flags.append("INACTIVE")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {state:11} {row['company']}{suffix}")
        print(
            f"              {row['ats_type']}:{row['ats_identifier']} · "
            f"{row.get('capability')} · scope={row['watchlist_scope']}"
        )
        if health.get("detail"):
            print(f"              {health['detail']}")
        if row.get("last_successful_collection_at"):
            print(f"              last success: {row['last_successful_collection_at']}")
    return 0


def cmd_company_list(args: argparse.Namespace) -> int:
    """The company registry: the curated seed plus everything added since."""
    from app.services import monitored_companies

    with session_scope() as session:
        entries = monitored_companies.registry_entries(session)
        _print_header(f"COMPANY REGISTRY ({len(entries)})")
        for entry in entries:
            if args.provider and entry.ats_type != args.provider.upper():
                continue
            if args.status and entry.monitoring_status != args.status.upper():
                continue
            seen = "-" if entry.last_jobs_seen is None else str(entry.last_jobs_seen)
            print(
                f"  {entry.health():17} {entry.monitoring_status:12} {entry.registry_source:17} "
                f"{entry.ats_type:15} {entry.company_name}  [{seen} listed]"
            )
            if args.verbose:
                print(f"{'':20}{entry.ats_identifier or '(no board)'}")
                if entry.last_failure_reason:
                    print(f"{'':20}last failure: {entry.last_failure_reason[:160]}")
    return 0


def cmd_company_add(args: argparse.Namespace) -> int:
    """Add one employer from a careers URL.

    Detection decides what can happen: a CONFIRMED board is added (EXPERIMENTAL
    unless --activate), a LIKELY_REVIEW board is shown but not added, and an
    unknown source is kept MANUAL_ONLY only when --manual-only is given.
    """
    from app.services import monitored_companies
    from app.services.source_detection import detect_source

    detection = detect_source(args.url, company_name=args.name)
    _print_header(f"ADD {args.name}")
    print(f"  confidence  {detection.confidence} ({detection.method})")
    if detection.provider:
        print(f"  provider    {detection.provider}")
        print(f"  board       {detection.identifier}")
    print(f"  {detection.detail}")
    for alternative in detection.alternatives:
        print(f"  also linked {alternative}")

    with session_scope() as session:
        if detection.confirmed:
            assert detection.provider and detection.identifier
            outcome = monitored_companies.monitor(
                session,
                company_name=args.name,
                ats_type=detection.provider,
                ats_identifier=detection.identifier,
                careers_url=args.url,
                registry_source=args.source,
                monitoring_status="ACTIVE" if args.activate else "EXPERIMENTAL",
                notes=args.notes,
            )
        elif args.manual_only:
            outcome = monitored_companies.record_unsupported(
                session,
                company_name=args.name,
                careers_url=args.url,
                ats_type=detection.provider,
                monitoring_status="VERIFY_LATER" if detection.provider else "MANUAL_ONLY",
                source_verification=detection.confidence,
                registry_source=args.source,
                notes=args.notes,
            )
        else:
            print("\n  Not added. Confirm the board and add it by its board URL, or pass")
            print("  --manual-only to keep the employer as a coverage gap.")
            return 1
        if not outcome.ok:
            print(f"\n  Not added: {outcome.detail}")
            return 1
        company = outcome.company
        assert company is not None
        verb = "Added" if outcome.created else "Updated"
        print(f"\n  {verb}: {company.company_name} -> {company.monitoring_status}")
        if company.monitoring_status == "EXPERIMENTAL":
            print(
                "  Canary: collect it with  python -m app.cli collect --company "
                f'"{company.company_name}"  then promote it.'
            )
    return 0


def cmd_company_set(args: argparse.Namespace) -> int:
    """Pause, resume, or promote a canary to the sweep."""
    from app.models.enums import MonitoringStatus
    from app.services import monitored_companies

    with session_scope() as session:
        if args.action == "promote":
            row = monitored_companies.find_row(session, company_name=args.name)
            if row is None or row.monitoring_status != MonitoringStatus.EXPERIMENTAL.value:
                print(f"  {args.name} is not an EXPERIMENTAL registry company.")
                return 1
            if row.last_successful_collection_at is None:
                print(
                    f"  {args.name} has never collected successfully. Run its canary first:"
                    f' python -m app.cli collect --company "{args.name}"'
                )
                return 1
            row.monitoring_status = MonitoringStatus.ACTIVE.value
            row.on_watchlist = True
            print(f"  {row.company_name} promoted: it joins every structured sweep from now on.")
            return 0
        status = (
            MonitoringStatus.PAUSED.value
            if args.action == "pause"
            else MonitoringStatus.ACTIVE.value
        )
        outcome = monitored_companies.set_monitoring(
            session, status=status, company_name=args.name
        )
        if not outcome.ok:
            print(f"  {outcome.detail}")
            return 1
        print(f"  {args.name}: {status}")
    return 0


def cmd_coverage(_args: argparse.Namespace) -> int:
    """Coverage of the company registry, and what collection is producing.

    Company and job counts only — no candidate data, no application state.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from app.models.base import utcnow
    from app.models.enums import JobLifecycleStatus
    from app.services import monitored_companies

    with session_scope() as session:
        cov = monitored_companies.coverage(session)
        _print_header("COMPANY REGISTRY COVERAGE")
        rows = [
            ("Registry companies", cov["total"]),
            ("Actively monitored", cov["monitored"]),
            ("  healthy (24h)", cov["healthy"]),
            ("  stale", cov["stale"]),
            ("  failing", cov["failing"]),
            ("  not yet collected", cov["not_yet_collected"]),
            ("Experimental (canary)", cov["experimental"]),
            ("Paused", cov["paused"]),
            ("Manual-only", cov["manual_only"]),
            ("Verify later", cov["verify_later"]),
            ("Unknown ATS", cov["unknown_ats"]),
            ("On a supported provider", cov["supported_provider"]),
        ]
        for label, value in rows:
            print(f"  {label:26}{value}")
        if cov["failed_recently"]:
            print(f"  Failed in the last 24h    {', '.join(cov['failed_recently'])}")

        _print_header("BY PROVIDER")
        print(
            f"  {'provider':16}{'registry':>9}{'monitored':>10}{'healthy':>9}"
            f"{'stale':>7}{'failing':>9}{'never':>7}"
        )
        for provider, row in cov["by_provider"].items():
            print(
                f"  {provider:16}{row['registry']:>9}{row['monitored']:>10}{row['HEALTHY']:>9}"
                f"{row['STALE']:>7}{row['FAILING']:>9}{row['NOT_YET_COLLECTED']:>7}"
            )

        _print_header("JOBS")
        now = utcnow()
        real = Job.is_demo.is_(False)
        open_by_source = session.execute(
            select(Job.discovery_source, func.count())
            .where(real, Job.lifecycle_status == JobLifecycleStatus.OPEN.value)
            .group_by(Job.discovery_source)
        ).all()
        listing = ", ".join(f"{source} {count}" for source, count in sorted(open_by_source))
        print(f"  Open jobs by source       {listing}")
        for label, hours in (("24h", 24), ("7d", 24 * 7)):
            since = now - timedelta(hours=hours)
            new = session.execute(
                select(func.count()).select_from(Job).where(real, Job.first_seen >= since)
            ).scalar_one()
            relevant = session.execute(
                select(func.count())
                .select_from(Job)
                .where(
                    real,
                    Job.first_seen >= since,
                    Job.target_priority.in_(("PRIMARY_TARGET", "SECONDARY_TARGET")),
                )
            ).scalar_one()
            print(
                f"  New jobs, last {label:10} {new} "
                f"({relevant} primary or secondary target)"
            )
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description="Job Hunt OS command line")
    sub = parser.add_subparsers(dest="group", required=True)

    evidence = sub.add_parser("evidence", help="Evidence Bank operations").add_subparsers(
        dest="action", required=True
    )
    evidence.add_parser("validate").set_defaults(func=cmd_evidence_validate)
    evidence.add_parser("digest").set_defaults(func=cmd_evidence_digest)

    db = sub.add_parser("db", help="Database operations").add_subparsers(
        dest="action", required=True
    )
    db.add_parser("upgrade").set_defaults(func=cmd_db_upgrade)
    db.add_parser("status").set_defaults(func=cmd_db_status)

    imp = sub.add_parser("import", help="Manual import").add_subparsers(
        dest="action", required=True
    )
    imp_file = imp.add_parser("file", help="Import a .json or .csv file")
    imp_file.add_argument("path")
    imp_file.set_defaults(func=cmd_import_file)

    analyze = sub.add_parser("analyze", help="Re-run analysis")
    analyze.add_argument("--lane", choices=[lane.value for lane in SearchLane])
    analyze.add_argument("--force", action="store_true", help="Ignore the content-hash gate")
    analyze.add_argument("--all", action="store_true", help="Analyze every job (default)")
    analyze.set_defaults(func=cmd_analyze)

    report = sub.add_parser("report", help="Reports").add_subparsers(dest="action", required=True)
    report.add_parser("today").set_defaults(func=cmd_report_today)

    demo = sub.add_parser("demo", help="Demo data").add_subparsers(dest="action", required=True)
    demo.add_parser("seed").set_defaults(func=cmd_demo_seed)
    demo.add_parser("clear").set_defaults(func=cmd_demo_clear)
    demo.add_parser(
        "bootstrap", help="Fill an empty database from the synthetic demo Evidence Bank"
    ).set_defaults(func=cmd_demo_bootstrap)

    job = sub.add_parser("job", help="Single-job operations").add_subparsers(
        dest="action", required=True
    )
    job_purge = job.add_parser("purge", help="Delete one job and its dependents (dry run first)")
    job_purge.add_argument("job_id")
    job_purge.add_argument(
        "--yes", action="store_true", help="Actually delete. Without this, dry run."
    )
    job_purge.set_defaults(func=cmd_job_purge)

    reclassify_cmd = sub.add_parser(
        "reclassify", help="Recompute functional classification for stored jobs"
    )
    reclassify_cmd.add_argument(
        "--all",
        dest="only_stale",
        action="store_false",
        help="Reclassify every job, not only ones on an older classifier version",
    )
    reclassify_cmd.add_argument("--limit", type=int, default=0, help="Stop after N jobs")
    reclassify_cmd.set_defaults(func=cmd_reclassify, only_stale=True)

    collect = sub.add_parser("collect", help="Structured collection from the watchlist")
    collect.add_argument("--profile", help="Search profile id (reserved for scheduling)")
    collect.add_argument("--lane", choices=[lane.value for lane in SearchLane])
    collect.add_argument("--company", help="Substring match on company name")
    collect.add_argument(
        "--connector", help="GREENHOUSE | LEVER | ASHBY | SMARTRECRUITERS | WORKDAY"
    )
    collect.add_argument(
        "--include-experimental",
        action="store_true",
        help="Also poll EXPERIMENTAL (canary) registry companies. --company does this too.",
    )
    collect.add_argument("--fixtures", action="store_true", help="Collect ONLY fixture boards")
    collect.add_argument(
        "--include-fixtures", action="store_true", help="Add fixtures to a real run"
    )
    collect.add_argument("--max-records", type=int, help="Cap records per company")
    collect.add_argument("--dry-run", action="store_true", help="Fetch but write nothing")
    collect.add_argument("--no-analyze", action="store_true", help="Stage without analyzing")
    collect.add_argument("--verbose", action="store_true")
    collect.add_argument(
        "--force",
        action="store_true",
        help="Collect even if another run holds the lock. Polls the same boards twice.",
    )
    collect.set_defaults(func=cmd_collect)

    schedule = sub.add_parser(
        "schedule", help="Show (and optionally register) the local collection schedule"
    )
    schedule.add_argument(
        "--register",
        action="store_true",
        help="Create the Windows scheduled task. Asks for confirmation first.",
    )
    schedule.set_defaults(func=cmd_schedule)

    source = sub.add_parser("source", help="Source health").add_subparsers(
        dest="action", required=True
    )
    source_check = source.add_parser("check", help="Reachability per configured source")
    source_check.add_argument("--no-fixtures", action="store_true")
    source_check.set_defaults(func=cmd_source_check)

    company = sub.add_parser("company", help="Company registry").add_subparsers(
        dest="action", required=True
    )
    company_list = company.add_parser("list", help="Every registry company with its health")
    company_list.add_argument("--provider")
    company_list.add_argument(
        "--status", help="ACTIVE | PAUSED | EXPERIMENTAL | MANUAL_ONLY | VERIFY_LATER"
    )
    company_list.add_argument("--verbose", action="store_true")
    company_list.set_defaults(func=cmd_company_list)

    company_add = company.add_parser("add", help="Add an employer from its careers URL")
    company_add.add_argument("name")
    company_add.add_argument("url")
    company_add.add_argument(
        "--activate",
        action="store_true",
        help="Join the sweep now instead of starting as an EXPERIMENTAL canary",
    )
    company_add.add_argument(
        "--manual-only",
        action="store_true",
        help="Keep an unsupported employer as a visible coverage gap",
    )
    company_add.add_argument(
        "--source", default="USER_ADDED", choices=["USER_ADDED", "CURATED_EXPANSION"]
    )
    company_add.add_argument("--notes")
    company_add.set_defaults(func=cmd_company_add)
    for action in ("pause", "resume", "promote"):
        cmd = company.add_parser(action)
        cmd.add_argument("name")
        cmd.set_defaults(func=cmd_company_set)

    coverage = sub.add_parser("coverage", help="Company registry and provider coverage")
    coverage.set_defaults(func=cmd_coverage)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
