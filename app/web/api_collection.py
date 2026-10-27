"""Collection: freshness, running it, the monitored set, and why a job was missed.

Slice 3's API was read-only, and said so: "**No endpoint here triggers a
collection.** Running a connector reaches out to someone else's servers and
writes to the real database, so it stays behind the CLI." That reasoning was
right about the risk and wrong about the consequence — the terminal requirement
is exactly why the corpus sat five days stale while the browser said nothing.

So the risk is handled rather than avoided:

* one run at a time, guarded by a ``system_task`` row that survives a restart
  and is shared with the CLI;
* the run happens on a background thread, because a full sweep takes tens of
  minutes and an HTTP request must not hold it;
* nothing here changes what collection *does*. It is the same service, the same
  connectors, the same content-hash gate.

Monitoring a company is likewise explicit: it happens on a click, for one named
employer, and it is reversible. No endpoint here enumerates ATS namespaces
looking for boards.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.base import as_utc
from app.models.ingestion import Job
from app.services import collection_runner, monitored_companies
from app.services.collection_status import CollectionStatusService
from app.services.watchlist import load_watchlist
from app.services.why_missed import WhyMissedService
from app.web.schemas import (
    AddCompanyPayload,
    CollectionRunStartResponse,
    CollectionStatusResponse,
    CompanyRegistryResponse,
    MonitorCompanyPayload,
    MonitorCompanyResponse,
    MonitoredCompaniesResponse,
    MonitoredCompanyView,
    MonitoringChangePayload,
    RegistryActionResponse,
    RegistryCompanyView,
    ScheduleSetupResponse,
    SourceDetectionPayload,
    SourceDetectionResponse,
    TestSourcePayload,
    TestSourceResponse,
    WhyMissedResponse,
)

api_collection = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Status and freshness
# ---------------------------------------------------------------------------


@api_collection.get("/collection/status", response_model=CollectionStatusResponse)
def collection_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """How fresh the corpus is, what is monitored, and what ran last."""
    payload = CollectionStatusService(db).status().as_dict()
    payload["task"] = collection_runner.task_state()
    return payload


@api_collection.post("/collection/run", response_model=CollectionRunStartResponse)
def run_collection(
    lane: str | None = None,
    company: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start a structured collection now. Refuses if one is already running."""
    result = collection_runner.start(lane=lane, company_name=company, triggered_by="browser")
    return {
        "started": result.started,
        "reason": result.reason,
        "detail": result.detail,
        "task": result.task,
    }


@api_collection.get("/collection/schedule", response_model=ScheduleSetupResponse)
def collection_schedule() -> dict[str, Any]:
    """What a local schedule would look like. This endpoint registers nothing.

    Registration touches the operating system's scheduler, which is a change to
    the machine rather than to this application's data. It happens from the CLI,
    after the user reads the command.
    """
    from app.cli import schedule_plan

    plan = schedule_plan()
    plan["registered"] = _task_registered(str(plan["task_name"]))
    return plan


def _task_registered(task_name: str) -> bool:
    """Ask Windows whether the task exists. False everywhere else."""
    import platform
    import subprocess

    if platform.system() != "Windows":
        return False
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# Monitored companies
# ---------------------------------------------------------------------------


@api_collection.get("/monitored-companies", response_model=MonitoredCompaniesResponse)
def list_monitored(db: Session = Depends(get_db)) -> dict[str, Any]:
    watchlist = load_watchlist()
    counts = _jobs_by_company(db)

    rows: list[MonitoredCompanyView] = []
    for entry in watchlist.watchlist_companies:
        rows.append(
            MonitoredCompanyView(
                company_id=entry.company_id,
                company_name=entry.company_name,
                ats_type=entry.ats_type,
                ats_identifier=entry.ats_identifier or None,
                careers_url=entry.careers_url,
                watchlist_scope=entry.watchlist_scope,
                source=monitored_companies.CORE,
                jobs_collected=counts.get(_normalize(entry.company_name), 0),
                removable=False,
            )
        )

    discovered = monitored_companies.discovered_companies(db)
    for company in discovered:
        metadata = company.source_metadata or {}
        rows.append(
            MonitoredCompanyView(
                company_id=company.company_id,
                company_name=company.company_name,
                ats_type=company.ats_type,
                ats_identifier=company.ats_identifier,
                careers_url=company.careers_url,
                watchlist_scope=company.watchlist_scope or "BOTH",
                source=monitored_companies.DISCOVERED,
                monitored_since=str(metadata.get("monitored_since") or "") or None,
                last_successful_collection_at=_iso(
                    as_utc(company.last_successful_collection_at)
                ),
                jobs_collected=counts.get(_normalize(company.company_name), 0),
                removable=True,
            )
        )

    return {
        "core_count": len(watchlist.watchlist_companies),
        "discovered_count": len(discovered),
        "companies": [r.model_dump() for r in rows],
    }


@api_collection.post("/monitored-companies", response_model=MonitorCompanyResponse)
def monitor_company(
    payload: MonitorCompanyPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Start polling one employer's board, because the user asked."""
    note = None
    if payload.job_id:
        job = db.get(Job, payload.job_id)
        note = f"Added from a posting for {job.title}." if job else None

    outcome = monitored_companies.monitor(
        db,
        company_name=payload.company_name,
        ats_type=payload.ats_type,
        ats_identifier=payload.ats_identifier,
        careers_url=payload.careers_url,
        watchlist_scope=payload.watchlist_scope,
        notes=note,
        discovered_from_job_id=payload.job_id,
    )
    if not outcome.ok:
        db.rollback()
        return {"ok": False, "reason": outcome.reason, "detail": outcome.detail}

    db.commit()
    company = outcome.company
    assert company is not None
    return {
        "ok": True,
        "created": outcome.created,
        "detail": (
            f"{company.company_name} is now monitored. Its board is collected on every "
            "structured refresh from here on."
        ),
        "company": MonitoredCompanyView(
            company_id=company.company_id,
            company_name=company.company_name,
            ats_type=company.ats_type,
            ats_identifier=company.ats_identifier,
            careers_url=company.careers_url,
            watchlist_scope=company.watchlist_scope or "BOTH",
            source=monitored_companies.DISCOVERED,
            monitored_since=str((company.source_metadata or {}).get("monitored_since") or "")
            or None,
            removable=True,
        ).model_dump(),
    }


@api_collection.post(
    "/monitored-companies/{company_id}/unmonitor", response_model=MonitorCompanyResponse
)
def unmonitor_company(company_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    outcome = monitored_companies.unmonitor(db, company_id)
    if not outcome.ok:
        db.rollback()
        return {"ok": False, "reason": outcome.reason, "detail": outcome.detail}
    db.commit()
    company = outcome.company
    assert company is not None
    return {
        "ok": True,
        "detail": (
            f"{company.company_name} is no longer polled. Jobs already collected from it stay."
        ),
    }


# ---------------------------------------------------------------------------
# Company registry (Discovery Coverage V2)
# ---------------------------------------------------------------------------


@api_collection.get("/company-registry", response_model=CompanyRegistryResponse)
def company_registry(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every registry company with its derived health, plus coverage totals.

    Read-only. Health comes from recorded polls; nothing here touches a board.
    """
    watchlist = load_watchlist()
    open_jobs = _open_jobs_by_company(db)
    entries = monitored_companies.registry_entries(db, watchlist=watchlist)
    coverage = monitored_companies.coverage(db, watchlist=watchlist)
    return {
        "coverage": coverage,
        "companies": [_registry_view(e, open_jobs).model_dump() for e in entries],
    }


@api_collection.post("/company-registry/detect", response_model=SourceDetectionResponse)
def detect_company_source(payload: SourceDetectionPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    """What a careers URL is, and whether it can be monitored. Writes nothing.

    At most one request to the board the URL names, and one fetch of the page
    itself (through the same guarded fetcher URL import uses).
    """
    from app.services.source_detection import detect_source

    detection = detect_source(payload.careers_url, company_name=payload.company_name)
    existing = None
    if detection.provider and detection.identifier:
        row = monitored_companies.find_row(
            db, ats_type=detection.provider, ats_identifier=detection.identifier
        )
        if row is not None and row.ats_identifier:
            existing = row.company_name
    if existing is None:
        for entry in monitored_companies.registry_entries(db):
            same_board = bool(detection.identifier) and monitored_companies.board_key(
                entry.ats_type, entry.ats_identifier
            ) == monitored_companies.board_key(detection.provider or "", detection.identifier or "")
            if same_board:
                existing = entry.company_name
                break
    return {
        **detection.as_dict(),
        "can_monitor": detection.confirmed and existing is None,
        "already_in_registry": existing,
    }


@api_collection.post("/company-registry", response_model=RegistryActionResponse)
def add_company(payload: AddCompanyPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Add an employer from its careers URL, because the user asked.

    Detection runs again here rather than trusting a provider or board sent by
    the page: the browser never decides what a board is. Only a CONFIRMED board
    is monitored. Anything else is stored only when the user asked to keep it
    as manual-only, so the gap stays visible.
    """
    from app.services.source_detection import detect_source

    name = payload.company_name.strip()
    if not name or not payload.careers_url.strip():
        return {"ok": False, "reason": "MISSING_FIELDS", "detail": "A name and a careers URL are both needed."}

    detection = detect_source(payload.careers_url, company_name=name)
    if detection.confirmed and not payload.manual_only:
        assert detection.provider and detection.identifier
        outcome = monitored_companies.monitor(
            db,
            company_name=name,
            ats_type=detection.provider,
            ats_identifier=detection.identifier,
            careers_url=payload.careers_url.strip(),
            registry_source="USER_ADDED",
        )
        detail = (
            f"{name} is now monitored on {detection.provider.title()}. Its board is collected "
            "on every structured refresh from here on."
        )
    elif payload.manual_only:
        outcome = monitored_companies.record_unsupported(
            db,
            company_name=name,
            careers_url=payload.careers_url.strip(),
            ats_type=detection.provider,
            monitoring_status="VERIFY_LATER" if detection.provider else "MANUAL_ONLY",
            source_verification=detection.confidence,
        )
        detail = f"{name} is kept as manual-only. Postings from it arrive when you paste them."
    else:
        return {
            "ok": False,
            "reason": f"NOT_CONFIRMED_{detection.confidence}",
            "detail": detection.detail,
        }

    if not outcome.ok:
        db.rollback()
        return {"ok": False, "reason": outcome.reason, "detail": outcome.detail}
    db.commit()
    return {
        "ok": True,
        "created": outcome.created,
        "detail": detail,
        "company": _registry_view_for(db, outcome.company.company_id if outcome.company else None),
    }


@api_collection.post("/company-registry/monitoring", response_model=RegistryActionResponse)
def change_monitoring(payload: MonitoringChangePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Pause or resume one registry company. Seed companies included; the
    curated file is never edited."""
    outcome = monitored_companies.set_monitoring(
        db,
        status=payload.status.upper(),
        company_id=payload.company_id,
        company_name=payload.company_name,
    )
    if not outcome.ok:
        db.rollback()
        return {"ok": False, "reason": outcome.reason, "detail": outcome.detail}
    db.commit()
    company = outcome.company
    verb = "paused" if payload.status.upper() == "PAUSED" else "monitored again"
    return {
        "ok": True,
        "detail": f"{company.company_name if company else 'Company'} is {verb}. Jobs already collected stay.",
        "company": _registry_view_for(db, company.company_id if company else None),
    }


@api_collection.post("/company-registry/test", response_model=TestSourceResponse)
def test_company_source(payload: TestSourcePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    """One cheap read of a registry company's board. Ingests nothing."""
    from app.connectors import get_connector

    entry = next(
        (
            e
            for e in monitored_companies.registry_entries(db)
            if (payload.company_id and e.company_id == payload.company_id)
            or (payload.company_name and e.company_name == payload.company_name)
        ),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="No such company in the registry.")
    if not entry.collectable:
        return {
            "ok": False,
            "status": "UNSUPPORTED",
            "detail": f"{entry.company_name} has no careers source Job Hunt OS can poll.",
        }
    connector = get_connector(entry.ats_type)
    try:
        health = connector.health_check(entry.as_config())
    finally:
        connector.close()
    return {
        "ok": health.reachable,
        "status": health.status,
        "detail": health.detail,
        "postings_visible": health.postings_visible,
    }


def _registry_view(entry: monitored_companies.RegistryEntry, open_jobs: dict[str, int]) -> RegistryCompanyView:
    health = entry.health()
    return RegistryCompanyView(
        company_id=entry.company_id,
        company_name=entry.company_name,
        ats_type=entry.ats_type,
        ats_identifier=entry.ats_identifier or None,
        careers_url=entry.careers_url,
        registry_source=entry.registry_source,
        monitoring_status=entry.monitoring_status,
        source_verification=entry.source_verification,
        health=health,
        last_successful_collection_at=_iso(as_utc(entry.last_successful_collection_at)),
        last_failure_at=_iso(as_utc(entry.last_failure_at)),
        last_failure_reason=entry.last_failure_reason,
        last_jobs_seen=entry.last_jobs_seen,
        last_new_jobs=entry.last_new_jobs,
        open_jobs=open_jobs.get(_normalize(entry.company_name), 0),
        can_pause=entry.monitoring_status in ("ACTIVE", "EXPERIMENTAL"),
        can_resume=entry.monitoring_status == "PAUSED"
        and entry.collectable
        and entry.source_verification == "CONFIRMED",
        can_test=entry.collectable,
    )


def _registry_view_for(db: Session, company_id: str | None) -> dict[str, Any] | None:
    if not company_id:
        return None
    entry = next(
        (e for e in monitored_companies.registry_entries(db) if e.company_id == company_id), None
    )
    return _registry_view(entry, _open_jobs_by_company(db)).model_dump() if entry else None


def _open_jobs_by_company(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Job.company_name, func.count())
        .where(Job.is_demo.is_(False), Job.lifecycle_status == "OPEN")
        .group_by(Job.company_name)
    ).all()
    counts: dict[str, int] = {}
    for name, count in rows:
        key = _normalize(str(name or ""))
        counts[key] = counts.get(key, 0) + int(count)
    return counts


# ---------------------------------------------------------------------------
# Why wasn't this found?
# ---------------------------------------------------------------------------


@api_collection.get("/jobs/{job_id}/why-missed", response_model=WhyMissedResponse)
def why_missed(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = WhyMissedService(db).explain(job).as_dict()
    result["job_id"] = job_id
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jobs_by_company(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Job.company_name, func.count())
        .where(Job.is_demo.is_(False))
        .group_by(Job.company_name)
    ).all()
    counts: dict[str, int] = {}
    for name, count in rows:
        counts[_normalize(str(name or ""))] = counts.get(_normalize(str(name or "")), 0) + int(
            count
        )
    return counts


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _normalize(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in name or "")
    return " ".join(cleaned.split())
