"""Cover letters: detect, generate, edit, download, record.

The same shape as the resume path in `api_slice5.py` — versions, not
overwrites; render on generate; download by version — because they are the two
halves of one application and a user should not have to learn two workflows.

Three properties are load-bearing:

**Versions accumulate.** A regeneration never silently replaces a version a
human edited and saved. `replace_draft` exists for the case the user means —
"try again, I hadn't touched it" — and refuses when there is an edit to lose.

**Evidence IDs stay inside the API.** They are on `why_these_examples` for the
internal panel and on the version record for provenance. They never reach the
letter, the PDF, the DOCX, or the text the user copies.

**Nothing is submitted.** These endpoints produce files. The employer's form is
still the user's to fill in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.evidence.service import get_evidence_bank
from app.models.crm import Application
from app.models.ingestion import Job
from app.models.misc import Contact, CoverLetterVersion, Referral
from app.services import cover_letter_documents
from app.services.cover_letter import CoverLetterService
from app.services.cover_letter_requirement import detect_cover_letter_requirement
from app.services.role_targeting import build_profile_for_job
from app.web.schemas import (
    CoverLetterGenerateResponse,
    CoverLetterVersionsResponse,
    CoverLetterVersionView,
    ExternalCoverLetterPayload,
    GenerateCoverLetterPayload,
    SaveCoverLetterPayload,
)

api_cover_letter = APIRouter(prefix="/api")

DRAFT = "DRAFT"
SAVED = "SAVED"
SUPERSEDED = "SUPERSEDED"
EXTERNAL = "EXTERNAL"


def _job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _version_view(row: CoverLetterVersion, job: Job) -> dict[str, Any]:
    return CoverLetterVersionView(
        cover_letter_version_id=row.cover_letter_version_id,
        job_id=row.job_id,
        version=row.version,
        status=row.status,
        user_edited=row.user_edited,
        salutation=row.salutation,
        content=row.content or "",
        word_count=len((row.content or "").split()),
        role_archetype=row.role_archetype,
        label=row.label,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        evidence_ids_used=list(row.evidence_ids_used or []),
        safe_claim_ids_used=list(row.safe_claim_ids_used or []),
        pdf_available=bool(row.file_path and Path(row.file_path).exists()),
        docx_available=bool(row.docx_path and Path(row.docx_path).exists()),
        pdf_filename=cover_letter_documents.filename_for(job, "pdf"),
        docx_filename=cover_letter_documents.filename_for(job, "docx"),
        notes=row.notes,
    ).model_dump()


def _versions(db: Session, job_id: str) -> list[CoverLetterVersion]:
    return list(
        db.execute(
            select(CoverLetterVersion)
            .where(CoverLetterVersion.job_id == job_id)
            .order_by(CoverLetterVersion.version.desc())
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Listing — also where the requirement verdict lives
# ---------------------------------------------------------------------------


@api_cover_letter.get(
    "/jobs/{job_id}/cover-letter", response_model=CoverLetterVersionsResponse
)
def cover_letter_state(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything the Application tab needs to show the cover-letter row."""
    job = _job_or_404(db, job_id)
    rows = _versions(db, job_id)
    requirement = detect_cover_letter_requirement(job.description)

    # Open the newest thing the user would expect: their own saved edit if one
    # exists, otherwise the newest draft.
    saved = next((r for r in rows if r.status == SAVED), None)
    current = saved or next((r for r in rows if r.status == DRAFT), None)

    return {
        "job_id": job_id,
        "requirement": requirement.as_dict(),
        "count": len(rows),
        "versions": [_version_view(r, job) for r in rows],
        "current": _version_view(current, job) if current else None,
        "external_recorded": any(r.status == EXTERNAL for r in rows),
    }


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


@api_cover_letter.post(
    "/jobs/{job_id}/cover-letter/generate", response_model=CoverLetterGenerateResponse
)
def generate_cover_letter(
    job_id: str, payload: GenerateCoverLetterPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Compose a letter from the Evidence Bank and render both formats."""
    job = _job_or_404(db, job_id)
    existing = _versions(db, job_id)

    if payload.replace_draft:
        latest = existing[0] if existing else None
        if latest is not None and latest.user_edited:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The most recent version has edits you saved. Generating a new "
                    "version keeps it; replacing it would discard your wording."
                ),
            )

    referral_name = _referral_name(db, job_id, payload.include_referral_contact_id)
    if payload.include_referral_contact_id and referral_name is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "That contact has not actually referred you for this job. A cover "
                "letter may name a referrer only once the referral is recorded as "
                "received."
            ),
        )

    target = build_profile_for_job(db, job)
    letter = CoverLetterService().generate(
        job=job,
        target=target,
        recipient=payload.recipient,
        referral_contact=referral_name,
        include_work_authorization=payload.include_work_authorization,
        work_authorization_text=payload.work_authorization_text,
        extra_context=payload.extra_context,
    )

    application = db.execute(
        select(Application).where(Application.job_id == job_id)
    ).scalar_one_or_none()

    # Supersede earlier DRAFTS only. A saved edit stays exactly as it is.
    for row in existing:
        if row.status == DRAFT:
            row.status = SUPERSEDED

    version = CoverLetterVersion(
        job_id=job_id,
        application_id=application.application_id if application else None,
        version=(existing[0].version + 1) if existing else 1,
        label=f"{job.company_name} — {job.title}",
        content=letter.body,
        salutation=letter.salutation,
        status=DRAFT,
        user_edited=False,
        role_archetype=letter.role_archetype or None,
        evidence_ids_used=letter.evidence_ids or None,
        safe_claim_ids_used=letter.safe_claim_ids or None,
        evidence_bank_version=get_evidence_bank().version,
        generation_provenance=letter.provenance,
        is_demo=job.is_demo,
    )
    db.add(version)
    db.flush()

    pdf_path, docx_path = cover_letter_documents.generate(
        letter, job, cover_letter_version_id=version.cover_letter_version_id
    )
    version.file_path = pdf_path
    version.docx_path = docx_path
    db.commit()

    return {
        "version": _version_view(version, job),
        "why_these_examples": letter.as_dict()["why_these_examples"],
        "omissions": letter.omissions,
        "requirement": detect_cover_letter_requirement(job.description).as_dict(),
    }


def _referral_name(db: Session, job_id: str, contact_id: str | None) -> str | None:
    """A name, only when the referral actually happened.

    A contact replying to outreach is not an endorsement, and a requested
    referral is not a received one. Anything short of RECEIVED or
    SUBMITTED_WITH_REFERRAL returns None and the caller refuses.
    """
    if not contact_id:
        return None
    referral = db.execute(
        select(Referral).where(Referral.job_id == job_id, Referral.contact_id == contact_id)
    ).scalar_one_or_none()
    if referral is None or referral.status not in ("RECEIVED", "SUBMITTED_WITH_REFERRAL"):
        return None
    contact = db.get(Contact, contact_id)
    return contact.name if contact else None


# ---------------------------------------------------------------------------
# Edit, download, record
# ---------------------------------------------------------------------------


@api_cover_letter.post(
    "/cover-letters/{version_id}/save", response_model=CoverLetterVersionView
)
def save_cover_letter(
    version_id: str, payload: SaveCoverLetterPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Persist the user's own wording, and re-render from it.

    The saved text is what gets rendered — the user's edit is the document from
    here on, not a note attached to the generated one.
    """
    row = db.get(CoverLetterVersion, version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Cover letter version not found")
    job = _job_or_404(db, row.job_id)

    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="A cover letter needs some text.")

    row.content = content
    if payload.salutation:
        row.salutation = payload.salutation.strip()
    if payload.notes is not None:
        row.notes = payload.notes
    row.status = SAVED
    row.user_edited = True

    from app.services.cover_letter import CoverLetter

    edited = CoverLetter(
        salutation=row.salutation or "",
        paragraphs=[p.strip() for p in content.split("\n\n") if p.strip()],
    )
    pdf_path, docx_path = cover_letter_documents.generate(
        edited, job, cover_letter_version_id=row.cover_letter_version_id
    )
    row.file_path = pdf_path
    row.docx_path = docx_path
    db.commit()
    return _version_view(row, job)


@api_cover_letter.post(
    "/jobs/{job_id}/cover-letter/external", response_model=CoverLetterVersionView
)
def record_external_cover_letter(
    job_id: str, payload: ExternalCoverLetterPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Record that a letter written elsewhere was used.

    The same path an external resume takes: the system tracks that the document
    exists so readiness is honest, without pretending to hold a copy of it.
    """
    job = _job_or_404(db, job_id)
    existing = _versions(db, job_id)
    version = CoverLetterVersion(
        job_id=job_id,
        version=(existing[0].version + 1) if existing else 1,
        label=f"{job.company_name} — {job.title} (external)",
        content="",
        status=EXTERNAL,
        user_edited=False,
        notes=payload.notes or "Written outside Job Hunt OS.",
        is_demo=job.is_demo,
    )
    db.add(version)
    db.commit()
    return _version_view(version, job)


@api_cover_letter.get("/cover-letters/{version_id}/download.pdf")
def download_cover_letter_pdf(version_id: str, db: Session = Depends(get_db)) -> FileResponse:
    return _download(db, version_id, "pdf")


@api_cover_letter.get("/cover-letters/{version_id}/download.docx")
def download_cover_letter_docx(version_id: str, db: Session = Depends(get_db)) -> FileResponse:
    return _download(db, version_id, "docx")


def _download(db: Session, version_id: str, extension: str) -> FileResponse:
    row = db.get(CoverLetterVersion, version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Cover letter version not found")
    path_value = row.file_path if extension == "pdf" else row.docx_path
    if not path_value or not Path(path_value).exists():
        raise HTTPException(status_code=404, detail=f"No {extension.upper()} has been rendered.")
    job = _job_or_404(db, row.job_id)
    media = (
        "application/pdf"
        if extension == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return FileResponse(
        path_value,
        media_type=media,
        filename=cover_letter_documents.filename_for(job, extension),
    )
