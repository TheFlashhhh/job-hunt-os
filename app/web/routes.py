"""FastAPI routes: HTML pages plus a JSON API.

The JSON API is the same data the pages render, so a Next.js frontend can be
added later without reworking the backend.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.evidence.claim_validator import get_claim_validator
from app.evidence.service import get_evidence_bank
from app.models.analysis import JobAnalysis
from app.models.base import utcnow
from app.models.crm import Application, ApplicationNote, ApplicationStatusHistory
from app.models.enums import ApplicationStatus
from app.models.ingestion import Job
from app.services.dashboard import DashboardService
from app.services.experience_fit import classify as classify_experience_fit
from app.services.functional_screening import why_this_matches
from app.services.import_service import ManualImportService
from app.services.pipeline import JobPipeline
from app.services.quality_guards import STALE_AFTER_DAYS
from app.services.quality_guards import check as run_quality_guards
from app.services.search_strategy import archived_lanes, lane_is_active
from app.web.schemas import (
    ClaimCheckResponse,
    DashboardResponse,
    HealthResponse,
    JobDetailResponse,
    JobsResponse,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()
api = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    lane: str = "ALL",
    period: str = "ALL",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    service = DashboardService(db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "kpis": service.kpis(lane=lane, period=period),
            "funnel": service.funnel(lane=lane),
            "panel": service.today_panel(),
            "lane_counts": service.lane_counts(),
            "sources": service.source_performance(),
            "lane": lane,
            "period": period,
            "bank": get_evidence_bank().summary(),
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(
    request: Request,
    lane: str = "ALL",
    status: str = "ALL",
    eligibility: str = "ALL",
    band: str = "ALL",
    q: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stmt = select(Job).where(Job.is_demo.is_(False))
    if lane != "ALL":
        stmt = stmt.where(Job.search_lane == lane)
    if eligibility != "ALL":
        stmt = stmt.where(Job.eligibility_status == eligibility)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(Job.normalized_title.like(like) | Job.normalized_company.like(like))

    jobs = list(db.execute(stmt).scalars().all())
    rows = [_job_row(job) for job in jobs]
    if status != "ALL":
        rows = [r for r in rows if r["application_status"] == status]
    if band != "ALL":
        rows = [r for r in rows if r["band"] == band]
    rows.sort(key=lambda r: r["priority"], reverse=True)

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "rows": rows,
            "lane": lane,
            "status": status,
            "eligibility": eligibility,
            "band": band,
            "q": q,
            "statuses": [s.value for s in ApplicationStatus],
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    analysis = job.latest_analysis
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "analysis": analysis,
            "requirements": _requirement_view(analysis),
            "statuses": [s.value for s in ApplicationStatus],
            "bank": get_evidence_bank(),
        },
    )


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "import.html", {"result": None})


@router.post("/import", response_class=HTMLResponse)
def import_submit(
    request: Request,
    company_name: str = Form(""),
    title: str = Form(""),
    location: str = Form(""),
    url: str = Form(""),
    deadline: str = Form(""),
    description: str = Form(""),
    bulk_payload: str = Form(""),
    bulk_format: str = Form("json"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    service = ManualImportService(db)
    message: dict[str, Any] = {}

    try:
        if bulk_payload.strip():
            result = (
                service.import_json(bulk_payload)
                if bulk_format == "json"
                else service.import_csv(bulk_payload)
            )
            db.commit()
            message = {
                "kind": "bulk",
                "created": result.created,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "errors": result.errors,
            }
        elif description.strip():
            if not company_name.strip() or not title.strip():
                outcome = service.import_pasted_blob(description, url=url or None)
            else:
                outcome = service.import_text(
                    company_name=company_name,
                    title=title,
                    description=description,
                    location=location or None,
                    url=url or None,
                    deadline=_parse_date(deadline),
                )
            db.commit()
            message = {
                "kind": "single",
                "job_id": outcome.job.job_id if outcome.job else None,
                "created": outcome.created,
                "unchanged": outcome.unchanged,
                "messages": outcome.messages,
            }
        else:
            message = {"kind": "error", "errors": ["Provide a job description or a bulk payload."]}
    except Exception as exc:  # surface the failure rather than 500-ing
        db.rollback()
        message = {"kind": "error", "errors": [f"{type(exc).__name__}: {exc}"]}

    return templates.TemplateResponse(request, "import.html", {"result": message})


@router.post("/jobs/{job_id}/status")
def update_status(
    job_id: str,
    new_status: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        target = ApplicationStatus(new_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown status: {new_status}") from exc

    app = job.application
    if app is None:
        app = Application(job_id=job.job_id, current_status=ApplicationStatus.DISCOVERED.value)
        db.add(app)
        db.flush()

    previous = app.current_status
    if previous != target.value:
        app.current_status = target.value
        if target == ApplicationStatus.APPLIED and app.applied_at is None:
            app.applied_at = utcnow()
        # Append-only: history is never overwritten.
        db.add(
            ApplicationStatusHistory(
                application_id=app.application_id,
                from_status=previous,
                to_status=target.value,
                changed_by="USER",
                note=note or None,
            )
        )
    if note:
        db.add(ApplicationNote(application_id=app.application_id, body=note))
    db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/reanalyze")
def reanalyze(job_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    JobPipeline(db).analyze(job)
    db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/evidence", response_class=HTMLResponse)
def evidence_browser(request: Request, q: str = "") -> HTMLResponse:
    bank = get_evidence_bank()
    evidence = list(bank.evidence.values())
    if q:
        needle = q.lower()
        evidence = [
            e
            for e in evidence
            if needle in e.evidence_id.lower()
            or needle in e.organization_or_project.lower()
            or any(needle in str(t).lower() for t in e.technologies)
        ]
    return templates.TemplateResponse(
        request,
        "evidence.html",
        {
            "bank": bank,
            "evidence": sorted(evidence, key=lambda e: e.evidence_id),
            "skills": sorted(bank.skills.values(), key=lambda s: (-s.fit_weight, s.name)),
            "gaps": bank.gaps,
            "open_questions": bank.open_questions,
            "q": q,
        },
    )


@router.get("/claim-check", response_class=HTMLResponse)
def claim_check_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "claim_check.html", {"check": None, "text": ""})


@router.post("/claim-check", response_class=HTMLResponse)
def claim_check_submit(request: Request, text: str = Form("")) -> HTMLResponse:
    check = get_claim_validator().validate(text) if text.strip() else None
    return templates.TemplateResponse(request, "claim_check.html", {"check": check, "text": text})


# ---------------------------------------------------------------------------
# JSON API — same data, ready for a Next.js frontend
# ---------------------------------------------------------------------------


@api.get("/health", response_model=HealthResponse)
def health() -> dict[str, Any]:
    bank = get_evidence_bank()
    return {"status": "ok", "evidence_bank_version": bank.version, "counts": bank.summary()}


@api.get("/dashboard", response_model=DashboardResponse)
def api_dashboard(
    lane: str = "ALL", period: str = "ALL", db: Session = Depends(get_db)
) -> dict[str, Any]:
    service = DashboardService(db)
    panel = service.today_panel()
    return {
        "kpis": service.kpis(lane=lane, period=period).as_dict(),
        "funnel": service.funnel(lane=lane),
        "lane_counts": service.lane_counts(),
        "sources": service.source_performance(),
        "today": {
            "due_today": [j.job_id for j in panel.due_today],
            "due_soon": [j.job_id for j in panel.due_soon],
            "high_priority_unapplied": [j.job_id for j in panel.high_priority_unapplied],
            "hidden_fits_to_review": [j.job_id for j in panel.hidden_fits_to_review],
            "authorization_questions": [j.job_id for j in panel.authorization_questions],
            "total_actions": panel.total_actions,
        },
    }


#: Hard ceiling on one page. The first real collection returned 1,070 jobs and
#: an unbounded response was 826 KB; at full-watchlist scale that becomes
#: megabytes per request. Pagination is a correctness property here, not an
#: optimisation.
MAX_PAGE_SIZE = 200


_SORTS = {
    "RECOMMENDED": ("priority", True),
    "HIGHEST_FIT": ("priority", True),
    "HIGHEST_PRIORITY": ("priority", True),
    "NEWEST": ("first_seen", True),
    "RECENTLY_DISCOVERED": ("first_seen", True),
}


@api.get("/jobs", response_model=JobsResponse)
def api_jobs(
    lane: str = "ALL",
    limit: int = 50,
    offset: int = 0,
    relevant_only: bool = True,
    prefilter: str = "ALL",
    sort: str = "RECOMMENDED",
    role_family: str | None = None,
    location: str | None = None,
    remote: str | None = None,
    employment_type: str | None = None,
    eligibility: str | None = None,
    crm_state: str | None = None,
    experience_fit: str | None = None,
    company: str | None = None,
    source: str | None = None,
    q: str | None = None,
    #: Function the JD describes, e.g. PRODUCT_ANALYTICS. Not the title.
    function: str | None = None,
    #: Only roles whose title hides the work.
    hidden_fit: bool = False,
    #: ENTRY_COMPATIBLE | EARLY_CAREER | REACH | OVERLEVELLED
    experience_band: str | None = None,
    #: Archived lanes — the Fall 2026 internship history. Off by default.
    include_archived: bool = False,
    #: PRIMARY_TARGET | SECONDARY_TARGET. Both are active; this narrows to one.
    target_priority: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Ranked, filtered, sorted jobs, paginated.

    ``relevant_only`` defaults True: a real board is mostly roles that failed
    deterministic screening (sales, support, non-US, too senior). Showing them
    by default buries the handful that matter. Set it False, or pass
    ``crm_state=REJECTED``, to see them — "Show rejected" in the UI is this
    flag, not a separate code path.
    """
    stmt = select(Job).where(Job.is_demo.is_(False))
    if lane != "ALL":
        stmt = stmt.where(Job.search_lane == lane)
    elif not include_archived:
        # The Fall 2026 internship lane was archived on 2026-08-24 (D-46). Its
        # records are preserved and stay openable by ID or through this flag —
        # they are simply not part of the default search any more.
        archived = archived_lanes()
        if archived:
            stmt = stmt.where(
                (Job.search_lane.is_(None)) | (Job.search_lane.notin_(archived))
            )
    if function:
        stmt = stmt.where(Job.functional_primary == function)
    if hidden_fit:
        stmt = stmt.where(Job.hidden_fit.is_(True), Job.functional_match.is_(True))
    if experience_band:
        stmt = stmt.where(Job.experience_band == experience_band)
    if target_priority:
        stmt = stmt.where(Job.target_priority == target_priority)
    if relevant_only:
        stmt = stmt.where(Job.screen_status.in_(["PASSED", "FLAGGED"]))
    if role_family:
        stmt = stmt.where(Job.role_family == role_family)
    if remote:
        stmt = stmt.where(Job.remote_status == remote)
    if employment_type:
        stmt = stmt.where(Job.employment_type == employment_type)
    if eligibility:
        stmt = stmt.where(Job.eligibility_status == eligibility)
    if location:
        stmt = stmt.where(Job.location_raw.ilike(f"%{location}%"))
    if company:
        stmt = stmt.where(Job.company_name.ilike(f"%{company}%"))
    if source:
        # "User Added" is a filter over the same list, never a separate view.
        # A job the user pasted has to keep appearing in Jobs to Review, Apply
        # Soon and the funnel alongside everything else.
        wanted = source.strip().upper()
        if wanted == "USER_ADDED":
            stmt = stmt.where(Job.discovery_source.in_(["USER_URL", "MANUAL", "MANUAL_IMPORT"]))
        elif wanted == "AUTOMATED":
            stmt = stmt.where(Job.discovery_source.notin_(["USER_URL", "MANUAL", "MANUAL_IMPORT"]))
        else:
            stmt = stmt.where(Job.discovery_source == wanted)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(
            (Job.title.ilike(needle))
            | (Job.company_name.ilike(needle))
            | (Job.location_raw.ilike(needle))
        )

    # Irrelevant records stay inspectable — they are real source observations
    # and feed source analytics. They are simply not the default view.
    wanted = prefilter.strip().upper()
    if wanted in ("PLAUSIBLE", "UNCERTAIN", "IRRELEVANT"):
        stmt = stmt.where(Job.prescreen_outcome == f"PREFILTER_{wanted}")

    # _job_row reads latest_analysis and its priority score. Without eager
    # loading that is one query per job — at 7,983 jobs the endpoint spent
    # seconds in round-trips to render a page of 50.
    stmt = stmt.options(
        selectinload(Job.analyses).selectinload(JobAnalysis.priority_score),
        selectinload(Job.application),
    )
    rows = [_job_row(j) for j in db.execute(stmt).scalars().all()]

    # CRM state and experience fit are computed/joined values, not plain
    # columns on Job — filtered in Python rather than forcing every caller
    # of _job_row to also duplicate the filter as SQL.
    if crm_state:
        rows = [r for r in rows if r["application_status"] == crm_state]
    if experience_fit:
        rows = [r for r in rows if r["experience_fit"] == experience_fit]

    sort_key, reverse = _SORTS.get(sort.strip().upper(), _SORTS["RECOMMENDED"])
    rows.sort(key=lambda r: (r[sort_key] is None, r[sort_key]), reverse=reverse)

    # RECOMMENDED puts a primary data/AI target ahead of a secondary software
    # one at similar fit — ranking, not exclusion, so software is neither buried
    # nor allowed to flood the feed. D-49. Python's sort is stable, so the
    # priority pass preserves the fit order inside each tier.
    if sort.strip().upper() == "RECOMMENDED":
        tier = {"PRIMARY_TARGET": 0, "SECONDARY_TARGET": 1}
        rows.sort(key=lambda r: tier.get(r.get("target_priority") or "", 2))

    total = len(rows)
    size = max(1, min(limit, MAX_PAGE_SIZE))
    start = max(0, offset)
    page = rows[start : start + size]
    return {
        "count": total,
        "returned": len(page),
        "offset": start,
        "limit": size,
        "relevant_only": relevant_only,
        "prefilter": wanted,
        "sort": sort.strip().upper(),
        "function": function,
        "hidden_fit": hidden_fit,
        "experience_band": experience_band,
        "include_archived": include_archived,
        "target_priority": target_priority,
        "jobs": page,
    }


@api.get("/jobs/{job_id}", response_model=JobDetailResponse)
def api_job_detail(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    analysis = job.latest_analysis
    guard = run_quality_guards(job)
    return {
        "job": _job_row(job),
        "description": job.description or "",
        # Why the system thinks this is relevant, in language a person reads.
        # No formulas and no internal scores — D-46.
        "why_this_matches": why_this_matches(job),
        "guard": guard.as_dict(),
        "eligibility": {
            "status": job.eligibility_status,
            "authorization_state": job.authorization_state,
            "authorization_wording": job.authorization_wording_verbatim,
            "reasons": analysis.eligibility.reasons if analysis and analysis.eligibility else [],
            # Defaults to True: absence of an assessment is not clearance.
            "requires_isso_verification": (
                analysis.eligibility.requires_isso_verification
                if analysis and analysis.eligibility
                else True
            ),
        },
        "sponsorship": {
            "role_signal": job.sponsorship_role_signal,
            "quote": job.sponsorship_quote,
            "company_context": (
                analysis.sponsorship.company_context if analysis and analysis.sponsorship else None
            ),
        },
        "legitimacy": job.legitimacy_status,
        "priority": (
            {
                "total": analysis.priority_score.total,
                "band": analysis.priority_score.band,
                "dimensions": analysis.priority_score.dimensions,
                "max_dimensions": analysis.priority_score.max_dimensions,
                "explanation": analysis.priority_score.explanation,
            }
            if analysis and analysis.priority_score
            else None
        ),
        "why_apply": analysis.why_apply if analysis else None,
        "risks": analysis.risks if analysis else [],
        "missing_requirements": analysis.missing_requirements if analysis else [],
        "requirements": _requirement_view(analysis),
        "sources": [
            {
                "connector": s.connector,
                "channel": s.discovery_channel,
                "role": s.role,
                "url": s.source_url,
                "first_seen": s.first_seen.isoformat() if s.first_seen else None,
            }
            for s in job.sources
        ],
        "provenance": {
            "discovery_source": job.discovery_source,
            "canonical_source": job.canonical_source,
            "canonical_verification": job.canonical_verification,
            "parser_version": job.parser_version,
            "evidence_bank_version": job.evidence_bank_version,
            "scoring_config_version": job.scoring_config_version,
        },
    }


@api.post("/claim-check", response_model=ClaimCheckResponse)
def api_claim_check(payload: dict[str, str]) -> JSONResponse:
    text = payload.get("text", "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    return JSONResponse(get_claim_validator().validate(text).as_dict())


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def _job_row(job: Job) -> dict[str, Any]:
    analysis = job.latest_analysis
    score = analysis.priority_score if analysis else None
    app = job.application
    fit, fit_reason = classify_experience_fit(job)
    return {
        # Functional classification (D-46): what the JD describes, as opposed
        # to what the title says. `role_family` below is the old title verdict;
        # the two disagreeing is informative rather than a bug.
        "functional_primary": job.functional_primary,
        "functional_match": bool(job.functional_match),
        "hidden_fit": bool(job.hidden_fit),
        "experience_band": job.experience_band,
        "start_fit": job.start_fit,
        "active_target": bool(job.active_target),
        "target_priority": job.target_priority or "",
        "lane_archived": not lane_is_active(job.search_lane),
        "job_id": job.job_id,
        "company": job.company_name,
        "title": job.title,
        "lane": job.search_lane,
        "role_family": job.role_family,
        "role_priority": job.role_priority,
        "location": job.location_raw,
        "remote": job.remote_status,
        "posted_date": job.posted_date.isoformat() if job.posted_date else None,
        "first_seen": job.first_seen.isoformat() if job.first_seen else None,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        # Scheduling facts. The Fall lane needs these to judge CPT plausibility;
        # they are meaningless for most full-time roles but cheap to carry.
        "start_date": job.start_date.isoformat() if job.start_date else None,
        "end_date": job.end_date.isoformat() if job.end_date else None,
        "weekly_hours": job.weekly_hours,
        "requires_enrollment": job.requires_enrollment,
        "eligibility": job.eligibility_status,
        "authorization": job.authorization_state,
        "sponsorship": job.sponsorship_role_signal,
        "legitimacy": job.legitimacy_status,
        "screen_status": job.screen_status,
        "priority": score.total if score else 0.0,
        "band": score.band if score else "LOW",
        "application_status": app.current_status if app else ApplicationStatus.DISCOVERED.value,
        "discovery_source": job.discovery_source,
        "canonical_source": job.canonical_source,
        "url": job.canonical_url,
        "next_action": app.next_action if app else None,
        "employment_type": job.employment_type,
        "comp_min": job.comp_min,
        "comp_max": job.comp_max,
        "comp_currency": job.comp_currency,
        "lifecycle_status": job.lifecycle_status,
        "experience_fit": fit.value,
        "experience_fit_reason": fit_reason,
        "is_stale": _is_stale(job),
    }


def _is_stale(job: Job) -> bool:
    reference = job.last_seen or job.first_seen
    if reference is None:
        return False
    ref = reference if reference.tzinfo else reference.replace(tzinfo=UTC)
    return datetime.now(UTC) - ref > timedelta(days=STALE_AFTER_DAYS)


def _requirement_view(analysis: Any) -> list[dict[str, Any]]:
    if analysis is None:
        return []
    out: list[dict[str, Any]] = []
    for requirement in sorted(analysis.requirements, key=lambda r: r.order_index):
        match = requirement.matches[0] if requirement.matches else None
        out.append(
            {
                "text": requirement.text,
                "kind": requirement.kind,
                "support_status": match.support_status if match else "UNKNOWN",
                "evidence_ids": (match.evidence_ids or []) if match else [],
                "confidence": match.confidence if match else 0.0,
                "explanation": match.explanation if match else "",
                "transferable_only": match.transferable_only if match else False,
                "ownership_gated": match.ownership_gated if match else False,
            }
        )
    return out


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
