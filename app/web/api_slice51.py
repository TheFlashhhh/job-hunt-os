"""Slice 5.1 JSON API: role-targeted resumes and manual job import.

Same rule as the other API modules — handlers stay thin. Nothing here decides
what is safe to claim (``SafeClaimValidator``), how a posting is positioned
(``role_targeting``), whether a URL is safe to fetch (``url_fetch``) or what
state an application is in (``ApplicationService``). This module wires those
services to the routes the UI needs.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models.analysis import JobAnalysis
from app.models.enums import ApplicationStatus, ReferralState
from app.models.ingestion import Job
from app.models.misc import ResumeVersion
from app.services.crm import ApplicationService
from app.services.next_actions import NextActionService
from app.services.resume_audit import ResumeTargetingAudit
from app.services.resume_base import ResumeBaseBuilder
from app.services.role_targeting import INTENSITY_LEVELS, build_profile_for_job
from app.services.url_fetch import UrlFetchError
from app.services.url_import import ManualJobUrlImportService
from app.web.schemas import (
    AlreadyAppliedPayload,
    ImportJobResponse,
    ImportUrlPayload,
    NextActionsResponse,
    NextActionView,
    ResumeTargetingResponse,
    TransitionResponse,
)

api51 = APIRouter(prefix="/api")


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Role targeting
# ---------------------------------------------------------------------------


@api51.get("/jobs/{job_id}/resume-targeting", response_model=ResumeTargetingResponse)
def resume_targeting(
    job_id: str, intensity: str | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """How this posting is being positioned, and what that did to the resume.

    A GET so the Resume tab can render the explanation on first load without
    POSTing anything. Building a profile and a base resume is pure computation
    over the Evidence Bank — no writes, no LLM, no network.
    """
    job = _job_or_404(db, job_id)
    if intensity and intensity.upper() not in INTENSITY_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"intensity must be one of {', '.join(INTENSITY_LEVELS)}",
        )

    profile = build_profile_for_job(db, job, intensity=intensity)
    base = ResumeBaseBuilder().build(profile.resume_base, target=profile)
    audit = ResumeTargetingAudit().run(base, profile)

    return {
        "job_id": job_id,
        "role_archetype": profile.role_archetype,
        "archetype_label": profile.archetype_label,
        "resume_base": profile.resume_base,
        "intensity": profile.intensity,
        "business_context": profile.business_context,
        "top_required_capabilities": list(profile.top_required_capabilities),
        "experience_bar": profile.experience_bar,
        "targeting": base.targeting.as_dict() if base.targeting else None,
        "audit": audit.as_dict(),
    }


# ---------------------------------------------------------------------------
# Manual URL import
# ---------------------------------------------------------------------------


def _import_view(result: Any, job: Job) -> dict[str, Any]:
    posting = result.posting
    application = job.application
    return {
        "ok": True,
        "job_id": job.job_id,
        "created": bool(result.outcome.created),
        "duplicate": result.duplicate,
        "existing_job_id": result.existing_job_id,
        "provider": posting.provider if posting else None,
        "external_id": posting.external_id if posting else None,
        "canonical_url": job.canonical_url,
        "company": job.company_name,
        "title": job.title,
        "location": job.location_raw,
        "description_chars": len(job.description or ""),
        "application_status": application.current_status if application else None,
        "messages": result.messages,
    }


@api51.post("/jobs/import-url", response_model=ImportJobResponse)
def import_job_url(payload: ImportUrlPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Import a listing the user found themselves.

    Three shapes, chosen by what the payload carries:

    * ``save_url_only`` — record the link and the user's own words for company
      and title. Nothing is inferred about the role.
    * a ``description`` — the paste fallback, for pages that cannot be read.
    * neither — fetch and extract the URL under the SSRF guards.

    ``intent`` decides the post-import state and is applied through the normal
    append-only transition system. Nothing here marks a job applied.
    """
    service = ManualJobUrlImportService(db)

    try:
        if payload.save_url_only:
            result = service.save_url_only(
                url=payload.url,
                company_name=payload.company_name or "",
                title=payload.title or "",
                location=payload.location,
            )
        elif payload.description and payload.description.strip():
            result = service.import_pasted(
                description=payload.description,
                url=payload.url or None,
                company_name=payload.company_name,
                title=payload.title,
                location=payload.location,
            )
        else:
            result = service.import_url(payload.url)
    except UrlFetchError as exc:
        # Raised by URL normalization before any fetch is attempted.
        db.rollback()
        return {
            "ok": False,
            "error_code": exc.code,
            "error": str(exc),
            "can_paste_description": exc.can_paste_description,
        }

    job = result.outcome.job if result.outcome else None
    if not result.ok or job is None:
        db.rollback()
        return {
            "ok": False,
            "error_code": result.error_code or "IMPORT_FAILED",
            "error": result.error or "That listing could not be imported.",
            "can_paste_description": result.can_paste_description,
            "messages": result.messages,
        }

    # A URL-only record has no description, no requirements and no fit, so
    # "review" is not a thing that can be done to it yet whatever the user
    # picked. It stays at DISCOVERED with a verify-first next action.
    intent = "SAVE" if payload.save_url_only else (payload.intent or "REVIEW").upper()
    if intent == "REVIEW":
        # SAVE leaves the job at DISCOVERED, which it already is. APPLIED is
        # never set here — recording a submission is a separate, explicit step
        # that collects a date and a resume.
        crm = ApplicationService(db)
        if job.application is None or job.application.current_status == (
            ApplicationStatus.DISCOVERED.value
        ):
            crm.transition(
                job,
                ApplicationStatus.REVIEW,
                note="Imported from a URL you pasted; moved to review.",
                changed_by="USER",
            )

    db.commit()
    return _import_view(result, job)


@api51.post("/jobs/{job_id}/already-applied", response_model=TransitionResponse)
def already_applied(
    job_id: str, payload: AlreadyAppliedPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Record an application the user submitted outside Job Hunt OS.

    This is the point of manual import for a job already in flight: the Job Hunt
    OS should be the source of truth for the search even when it neither
    discovered the job nor produced the resume. It uses the same append-only
    transition system as everything else — no raw status update.
    """
    job = _job_or_404(db, job_id)
    service = ApplicationService(db)

    try:
        applied_at = datetime.combine(date.fromisoformat(payload.applied_at), datetime.min.time())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="applied_at must be YYYY-MM-DD") from exc

    notes: list[str] = []
    if payload.resume_version_id:
        version = db.get(ResumeVersion, payload.resume_version_id)
        if version is None or version.job_id != job_id:
            raise HTTPException(status_code=400, detail="resume_version_id not found for this job")
        service.set_resume_version(job, payload.resume_version_id)
    elif payload.external_resume:
        # An honest record of "not this system's document". Leaving the field
        # empty would later read as "we do not know", which is different.
        notes.append("Resume used: external resume, not generated in Job Hunt OS.")

    if payload.referral_used:
        service.set_referral(
            job,
            state=ReferralState.SUBMITTED_WITH_REFERRAL,
            contact_id=payload.referral_contact_id,
        )

    if payload.confirmation_id:
        notes.append(f"Confirmation ID: {payload.confirmation_id}")
    if payload.notes:
        notes.append(payload.notes)

    result = service.transition(
        job,
        ApplicationStatus.APPLIED,
        note="\n".join(notes) or "Applied outside Job Hunt OS.",
        applied_at=applied_at,
    )
    db.commit()
    return {
        "job_id": job_id,
        "changed": result.changed,
        "from_status": result.from_status,
        "to_status": result.to_status,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# Next actions
# ---------------------------------------------------------------------------


@api51.get("/jobs/{job_id}/next-action", response_model=NextActionView)
def job_next_action(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    return NextActionService(db).for_job(job).as_dict()


@api51.get("/next-actions", response_model=NextActionsResponse)
def next_actions(
    limit: int = 12, source: str | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """The most urgent next step across the pipeline.

    ``source=USER_ADDED`` filters to jobs the user imported. That is a filter,
    not a separate feature: imported jobs appear in every other list too, which
    is the whole point of routing them through the same pipeline.
    """
    stmt = (
        select(Job)
        .where(Job.is_demo.is_(False))
        .where(Job.screen_status.in_(["PASSED", "FLAGGED"]))
        .options(
            selectinload(Job.analyses).selectinload(JobAnalysis.priority_score),
            selectinload(Job.application),
        )
    )
    if source and source.upper() == "USER_ADDED":
        stmt = stmt.where(Job.discovery_source.in_(["USER_URL", "MANUAL", "MANUAL_IMPORT"]))

    jobs = list(db.execute(stmt).scalars().all())
    actions = NextActionService(db).for_jobs(jobs)
    return {"actions": [a.as_dict() for a in actions[: max(1, min(limit, 100))]]}
