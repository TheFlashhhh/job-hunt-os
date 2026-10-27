"""Slice 5 JSON API: Prepare Application, Resume Review, Generation,
Download, the Application checklist tab, and a lightweight Interview tab.

Same rule as api_slice2/api_slice3: route handlers stay thin. Nothing here
decides what is safe to claim (that's ``SafeClaimValidator``, already run
inside ``ResumeRecommendationService``) or whether a job is safe to prepare
(that's ``app/services/quality_guards.py``). This module wires the existing
services into the actions Slice 5's UI needs and does not duplicate their
logic.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.evidence.service import get_evidence_bank
from app.models.enums import ApplicationStatus, RecommendationKind, ReferralState
from app.models.ingestion import Job
from app.models.misc import (
    CoverLetterVersion,
    JobContact,
    Referral,
    ResumeFamily,
    ResumeRecommendation,
    ResumeVersion,
)
from app.services import resume_documents, resume_library
from app.services.cover_letter_requirement import detect_cover_letter_requirement
from app.services.crm import ApplicationService
from app.services.quality_guards import check as run_quality_guards
from app.services.resume import ResumeRecommendationService, base_for_role_family, seed_resume_bases
from app.services.resume_fitting import ResumeOverflowError
from app.services.role_targeting import build_profile_for_job
from app.web.schemas import (
    ApplicationTabResponse,
    BaseResumeEducationView,
    BaseResumeEntryView,
    BaseResumeView,
    DecisionPayload,
    DecisionResponse,
    GenerateBaseResumeResponse,
    GenerateResumeResponse,
    InterviewPrepView,
    MarkAppliedPayload,
    PrepareApplicationResponse,
    ResumeVersionsResponse,
    TransitionResponse,
)

api5 = APIRouter(prefix="/api")


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _guard_view(result: Any) -> dict[str, Any]:
    return {
        "blocked": result.blocked,
        "block_reason": result.block_reason,
        "warnings": [{"code": w.code, "message": w.message} for w in result.warnings],
        "experience_fit": result.experience_fit,
        "experience_fit_reason": result.experience_fit_reason,
    }


def _evidence_labels(evidence_ids: list[str] | None) -> list[str]:
    """Human-readable project/experience names for a set of Evidence IDs.

    Resolved here rather than in React: the Evidence Bank is backend
    source-of-truth, and a frontend copy of the ID-to-name mapping would be
    exactly the duplication the API contract exists to prevent.

    Names are de-duplicated and order-preserved — four IDs from one project
    should read as that one project, not as the same name four times.
    """
    if not evidence_ids:
        return []
    bank = get_evidence_bank()
    labels: list[str] = []
    for evidence_id in evidence_ids:
        record = bank.get_evidence(evidence_id)
        name = getattr(record, "organization_or_project", None) if record else None
        if name and name not in labels:
            labels.append(name)
    return labels


def _rec_view(row: ResumeRecommendation) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "body": row.body,
        "rationale": row.rationale,
        "evidence_ids": row.evidence_ids or [],
        "evidence_labels": _evidence_labels(row.evidence_ids),
        "safe_claim_ids": row.safe_claim_ids or [],
        "requirement_text": row.requirement_text,
        "verdict": row.verdict,
        "rank": row.rank,
        "accepted": row.accepted,
    }


# ---------------------------------------------------------------------------
# Prepare Application
# ---------------------------------------------------------------------------


@api5.post("/jobs/{job_id}/prepare-application", response_model=PrepareApplicationResponse)
def prepare_application(
    job_id: str, base: str | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Steps A-F of Slice 5 §6: guard, choose base, parse, map evidence,
    propose defensible tailoring, run SafeClaimValidator.

    Does not alter a source resume — it produces (or refreshes) a review
    screen's worth of ``ResumeRecommendation`` rows and returns the guard
    result alongside them. A blocked job raises 422 with the reason rather
    than silently returning nothing, so the UI can show *why*.
    """
    job = _job_or_404(db, job_id)
    guard = run_quality_guards(job)
    if guard.blocked:
        raise HTTPException(status_code=422, detail=guard.block_reason)

    seed_resume_bases(db)
    # The base is chosen by the posting's ARCHETYPE, not by the classifier's
    # role family. "Analytics Engineer, Data Science" is a DATA_SCIENTIST by
    # title and an analytics-engineering job by content; the family alone
    # selected DS_ML_AI and led the page with computer vision.
    resolved_base = base or build_profile_for_job(db, job).resume_base
    service = ResumeRecommendationService(db)
    result = service.build(job, resume_base=resolved_base)
    rows = service.save(result)
    db.commit()

    return {
        "job_id": job_id,
        "resume_base": result.resume_base,
        "guard": _guard_view(guard),
        "recommendations": [_rec_view(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Resume Review — accept / reject each recommendation
# ---------------------------------------------------------------------------


@api5.post(
    "/jobs/{job_id}/recommendations/{recommendation_id}/decision",
    response_model=DecisionResponse,
)
def decide_recommendation(
    job_id: str, recommendation_id: str, payload: DecisionPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    _job_or_404(db, job_id)
    row = db.get(ResumeRecommendation, recommendation_id)
    if row is None or row.job_id != job_id:
        raise HTTPException(status_code=404, detail="Recommendation not found for this job")
    row.accepted = payload.accepted
    db.commit()
    return {"recommendation_id": row.id, "accepted": row.accepted}


# ---------------------------------------------------------------------------
# Resume Generation + Download
# ---------------------------------------------------------------------------


@api5.post("/jobs/{job_id}/generate-resume", response_model=GenerateResumeResponse)
def generate_resume(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Renders only rows the user explicitly accepted. Rejects and pending
    rows are excluded — nothing defaults onto the document."""
    job = _job_or_404(db, job_id)
    guard = run_quality_guards(job)
    if guard.blocked:
        raise HTTPException(status_code=422, detail=guard.block_reason)

    all_rows = (
        db.execute(
            select(ResumeRecommendation)
            .where(ResumeRecommendation.job_id == job_id)
            .order_by(ResumeRecommendation.rank)
        )
        .scalars()
        .all()
    )
    if not all_rows:
        raise HTTPException(
            status_code=422, detail="No recommendations yet — call Prepare Application first."
        )

    accepted = [
        r
        for r in all_rows
        if r.accepted is True
        and r.kind in (RecommendationKind.EMPHASIZE.value, RecommendationKind.SAFE_REWORD.value)
        # a SAFE_REWORD or an ownership-gated EMPHASIZE recommendation whose
        # verdict was BLOCKED never reaches here — build() already downgraded
        # blocked text to a DO_NOT_ADD row, which this filter excludes.
    ]
    if not accepted:
        raise HTTPException(
            status_code=422,
            detail="Accept at least one recommendation before generating a resume.",
        )

    profile = build_profile_for_job(db, job)
    resume_family_id = all_rows[0].resume_family_id
    family = db.get(ResumeFamily, resume_family_id) if resume_family_id else None
    base_name = family.name if family else base_for_role_family(job.role_family)

    label = f"{job.company_name} — {job.title}"
    evidence_ids = sorted({e for r in accepted for e in (r.evidence_ids or [])})
    version = ResumeVersion(
        resume_family_id=resume_family_id,
        label=label,
        job_id=job_id,
        evidence_ids_used=evidence_ids or None,
        safe_claim_ids_used=sorted({c for r in accepted for c in (r.safe_claim_ids or [])}) or None,
        evidence_bank_version=get_evidence_bank().version,
    )
    db.add(version)
    db.flush()

    pdf_path, docx_path = resume_documents.generate(
        job,
        accepted,
        resume_base=base_name,
        resume_version_id=version.resume_version_id,
        target=profile,
    )
    version.file_path = pdf_path
    version.docx_path = docx_path
    db.commit()

    return {
        "resume_version_id": version.resume_version_id,
        "job_id": job_id,
        "resume_base": base_name,
        "label": label,
        "accepted_count": len(accepted),
        "evidence_ids_used": evidence_ids,
        "generated_at": version.created_at.isoformat(),
        "pdf_available": True,
        "docx_available": True,
        "pdf_filename": resume_documents.filename_for(job, "pdf"),
        "docx_filename": resume_documents.filename_for(job, "docx"),
    }


@api5.get("/jobs/{job_id}/resume-versions", response_model=ResumeVersionsResponse)
def resume_versions(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _job_or_404(db, job_id)
    rows = (
        db.execute(
            select(ResumeVersion)
            .where(ResumeVersion.job_id == job_id)
            .order_by(ResumeVersion.created_at.desc())
        )
        .scalars()
        .all()
    )

    def _base_name(family_id: str | None) -> str:
        family = db.get(ResumeFamily, family_id) if family_id else None
        return family.name if family is not None else "UNKNOWN"

    return {
        "job_id": job_id,
        "versions": [
            {
                "resume_version_id": v.resume_version_id,
                "resume_base": _base_name(v.resume_family_id),
                "label": v.label,
                "job_id": v.job_id,
                "created_at": v.created_at.isoformat(),
                "pdf_available": bool(v.file_path and os.path.exists(v.file_path)),
                "docx_available": bool(v.docx_path and os.path.exists(v.docx_path)),
            }
            for v in rows
        ],
    }


def _resume_or_404(db: Session, resume_version_id: str) -> ResumeVersion:
    version = db.get(ResumeVersion, resume_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Resume version not found")
    return version


@api5.get("/resumes/{resume_version_id}/download.pdf")
def download_pdf(resume_version_id: str, db: Session = Depends(get_db)) -> FileResponse:
    version = _resume_or_404(db, resume_version_id)
    if not version.file_path or not os.path.exists(version.file_path):
        raise HTTPException(status_code=404, detail="PDF has not been generated for this version")
    job = db.get(Job, version.job_id) if version.job_id else None
    filename = resume_documents.filename_for(job, "pdf") if job else f"{resume_version_id}.pdf"
    return FileResponse(version.file_path, media_type="application/pdf", filename=filename)


@api5.get("/resumes/{resume_version_id}/download.docx")
def download_docx(resume_version_id: str, db: Session = Depends(get_db)) -> FileResponse:
    version = _resume_or_404(db, resume_version_id)
    if not version.docx_path or not os.path.exists(version.docx_path):
        raise HTTPException(status_code=404, detail="DOCX has not been generated for this version")
    job = db.get(Job, version.job_id) if version.job_id else None
    filename = resume_documents.filename_for(job, "docx") if job else f"{resume_version_id}.docx"
    return FileResponse(
        version.docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Application tab — checklist, canonical link, mark applied
# ---------------------------------------------------------------------------


@api5.get("/jobs/{job_id}/application-tab", response_model=ApplicationTabResponse)
def application_tab(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    guard = run_quality_guards(job)

    warning_codes = {w.code for w in guard.warnings}
    canonical_verified = job.canonical_verification == "CANONICAL_VERIFIED" and not (
        {"POSSIBLY_CLOSED", "STALE", "NO_CANONICAL_URL"} & warning_codes
    )
    eligibility_reviewed = job.eligibility_status != "UNCERTAIN"
    resume_prepared = (
        db.execute(
            select(ResumeVersion.resume_version_id).where(ResumeVersion.job_id == job_id)
        ).first()
        is not None
    )
    networking_checked = (
        db.execute(select(JobContact.id).where(JobContact.job_id == job_id)).first() is not None
        or db.execute(select(Referral.id).where(Referral.job_id == job_id)).first() is not None
    )
    authorization_reviewed = not (
        {"NEEDS_ISSO_REVIEW", "CITIZENSHIP_CLEARANCE_ISSUE"} & warning_codes
    )

    # A cover letter blocks readiness ONLY when the posting actually asked for
    # one. An optional letter is an opportunity, not a gate, and treating it as
    # a blocker would stop applications the employer would have accepted.
    requirement = detect_cover_letter_requirement(job.description)
    cover_letter_recorded = (
        db.execute(
            select(CoverLetterVersion.cover_letter_version_id).where(
                CoverLetterVersion.job_id == job_id
            )
        ).first()
        is not None
    )
    cover_letter_satisfied = cover_letter_recorded or not requirement.required

    checklist = {
        "canonical_posting_verified": canonical_verified,
        "eligibility_reviewed": eligibility_reviewed,
        "resume_prepared": resume_prepared,
        "networking_checked": networking_checked,
        "authorization_reviewed": authorization_reviewed,
        "cover_letter_status": requirement.status,
        "cover_letter_required": requirement.required,
        "cover_letter_recorded": cover_letter_recorded,
        "ready_to_apply": (
            not guard.blocked
            and canonical_verified
            and eligibility_reviewed
            and resume_prepared
            and authorization_reviewed
            and cover_letter_satisfied
        ),
    }
    return {
        "job_id": job_id,
        "checklist": checklist,
        "canonical_url": job.canonical_url,
        "guard": _guard_view(guard),
        "cover_letter_requirement": requirement.as_dict(),
    }


@api5.post("/jobs/{job_id}/mark-applied", response_model=TransitionResponse)
def mark_applied(
    job_id: str, payload: MarkAppliedPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Requires an application date and a resume version — the UI collects
    both before this fires. Sending is always external; this only records
    that the user did it themselves."""
    job = _job_or_404(db, job_id)
    service = ApplicationService(db)

    applied_at: datetime | None = None
    if payload.applied_at:
        try:
            applied_at = datetime.combine(
                date.fromisoformat(payload.applied_at), datetime.min.time()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="applied_at must be YYYY-MM-DD") from exc

    if payload.resume_version_id:
        version = db.get(ResumeVersion, payload.resume_version_id)
        if version is None or version.job_id != job_id:
            raise HTTPException(status_code=400, detail="resume_version_id not found for this job")
        service.set_resume_version(job, payload.resume_version_id)

    if payload.referral_used:
        service.set_referral(
            job,
            state=ReferralState.SUBMITTED_WITH_REFERRAL,
            contact_id=payload.referral_contact_id,
        )

    note_parts = []
    if payload.confirmation_id:
        note_parts.append(f"Confirmation ID: {payload.confirmation_id}")
    if payload.notes:
        note_parts.append(payload.notes)
    note = "\n".join(note_parts) or None

    result = service.transition(job, ApplicationStatus.APPLIED, note=note, applied_at=applied_at)
    db.commit()
    return {
        "job_id": job_id,
        "changed": result.changed,
        "from_status": result.from_status,
        "to_status": result.to_status,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# Interview tab — lightweight, deterministic, not a curriculum
# ---------------------------------------------------------------------------


@api5.get("/jobs/{job_id}/interview-prep", response_model=InterviewPrepView)
def interview_prep(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = _job_or_404(db, job_id)
    app = job.application
    stage = app.current_status if app else None

    rows = (
        db.execute(
            select(ResumeRecommendation)
            .where(ResumeRecommendation.job_id == job_id)
            .order_by(ResumeRecommendation.rank)
        )
        .scalars()
        .all()
    )
    bank = get_evidence_bank()

    accepted_or_strong = [
        r for r in rows if r.accepted is True or r.kind in (RecommendationKind.EMPHASIZE.value,)
    ]
    stories: list[str] = []
    evidence_to_review: list[str] = []
    for r in accepted_or_strong:
        for evidence_id in r.evidence_ids or []:
            record = bank.get_evidence(evidence_id)
            org = record.organization_or_project if record else None
            if org and org not in stories:
                stories.append(org)
            if evidence_id not in evidence_to_review:
                evidence_to_review.append(evidence_id)

    gaps = [r.body for r in rows if r.kind == RecommendationKind.GAP.value][:6]
    topics = [
        r.requirement_text
        for r in rows
        if r.requirement_text
        and r.kind in (RecommendationKind.EMPHASIZE.value, RecommendationKind.SAFE_REWORD.value)
    ]
    topics = list(dict.fromkeys(topics))[:6]

    questions = [
        f"Walk through {story} end to end — what you built, why, and what you'd change."
        for story in stories[:2]
    ]
    if gaps:
        questions.append(
            "How would you approach a requirement you don't have direct experience with?"
        )
    questions.append(
        f"Why {job.company_name}, and why this {job.role_family or 'role'} specifically?"
    )

    return {
        "job_id": job_id,
        "stage": stage,
        "likely_technical_topics": topics,
        "likely_project_stories": stories[:5],
        "evidence_to_review": evidence_to_review[:10],
        "known_gaps": gaps,
        "questions_to_practice": questions[:6],
    }


@api5.get("/resume-bases/{base_name}/preview", response_model=BaseResumeView)
def preview_resume_base(base_name: str) -> dict[str, Any]:
    """The complete base resume for a role family, before tailoring.

    Lets the review screen distinguish BASE RESUME from TAILORING CHANGES, so
    a list of seven proposed edits does not read as the whole document.

    It is the Resume Library document: built with no job, through the same
    builder and claim gate as a job resume, and fitted to one page - so the
    preview, the download and the base a job tailors all agree.
    """
    info = _library_base_or_404(base_name)
    try:
        content = resume_library.build_content(base_name)
    except ResumeOverflowError as exc:
        raise HTTPException(status_code=422, detail=_base_failure(exc)) from exc
    return {
        "base_name": base_name,
        "display_name": info.display_name,
        "name": content.name,
        "location": content.location,
        "contact_line": content.contact_line,
        "summary": content.summary,
        "education_lines": content.education_lines,
        "education": [
            BaseResumeEducationView(
                institution=school.institution,
                degree=school.degree_line,
                dates=school.dates,
                location=school.location,
                coursework=school.coursework,
            )
            for school in content.education
        ],
        "entries": [
            BaseResumeEntryView(
                heading=section.heading,
                subheading=section.subheading,
                kind=section.kind,
                bullets=list(section.bullets),
            )
            for section in content.sections
        ],
        "skill_lines": content.skill_lines,
        "total_bullets": sum(len(s.bullets) for s in content.sections),
        "page_count": content.page_count,
        "fit_reductions": content.fit_reductions,
        "best_for": info.best_for,
        "pdf_filename": info.pdf_filename,
        "docx_filename": info.docx_filename,
    }


# ---------------------------------------------------------------------------
# Resume Library - base resumes on their own, with no job
#
# Nothing below creates a Job, an Application or a ResumeVersion, or moves any
# CRM state. A base is regenerated from the current Evidence Bank on every
# download, so a file can never be staler than the Bank behind it.
# ---------------------------------------------------------------------------


def _library_base_or_404(base_name: str) -> resume_library.LibraryBase:
    try:
        return resume_library.get_base(base_name)
    except resume_library.UnknownBaseError as exc:
        raise HTTPException(status_code=404, detail=f"No resume base named {base_name!r}.") from exc


def _base_failure(exc: Exception) -> str:
    return (
        "Could not generate this base resume. Review Evidence Bank validation and "
        f"layout errors. {exc}"
    )


def _generate_base(base_name: str) -> resume_library.GeneratedBase:
    _library_base_or_404(base_name)
    try:
        return resume_library.generate(base_name)
    except ResumeOverflowError as exc:
        raise HTTPException(status_code=422, detail=_base_failure(exc)) from exc


@api5.post("/resume-bases/{base_name}/generate", response_model=GenerateBaseResumeResponse)
def generate_resume_base(base_name: str) -> dict[str, Any]:
    generated = _generate_base(base_name)
    return {
        "base_name": base_name,
        "display_name": generated.base.display_name,
        "generated_at": generated.generated_at.isoformat(),
        "page_count": generated.content.page_count,
        "entry_count": generated.entry_count,
        "bullet_count": generated.bullet_count,
        "pdf_filename": generated.base.pdf_filename,
        "docx_filename": generated.base.docx_filename,
        "fit_reductions": generated.content.fit_reductions,
    }


@api5.get("/resume-bases/{base_name}/download.pdf")
def download_base_pdf(base_name: str) -> FileResponse:
    generated = _generate_base(base_name)
    return FileResponse(
        generated.pdf_path,
        media_type="application/pdf",
        filename=generated.base.pdf_filename,
    )


@api5.get("/resume-bases/{base_name}/download.docx")
def download_base_docx(base_name: str) -> FileResponse:
    generated = _generate_base(base_name)
    return FileResponse(
        generated.docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=generated.base.docx_filename,
    )
