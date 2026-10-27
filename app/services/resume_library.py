"""The Resume Library: every canonical base resume, on its own, with no job.

Job applications start from a base and tailor it to a posting. This module
makes the same bases available independently - to preview, and to download as
PDF or DOCX for anything that is not one specific application.

**It adds no resume logic.** A library base is built by the same
``ResumeBaseBuilder`` a job resume uses, through the same claim gate, and
written by the same ``write_fitted`` that renders, fits and page-counts every
job resume. The only difference is the input: a posting-free profile from
``RoleTargetProfileBuilder.for_base`` instead of one derived from a job, and no
accepted recommendations. Nothing here can reach content a job resume could not.

**It touches no application state.** Generating a base creates no Job, no
Application, no ResumeVersion and no CRM transition. The files it writes live in
the git-ignored ``data/generated/resumes/bases/`` and are overwritten on each
generation: a base is a projection of the CURRENT Evidence Bank, and the file on
disk is a cache of that projection, never a canonical copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.services import resume_documents
from app.services.resume_base import load_base_config
from app.services.resume_documents import RESUME_DIR, ResumeContent
from app.services.resume_fitting import fit_to_one_page
from app.services.role_targeting import RoleTargetProfileBuilder

BASE_DIR = RESUME_DIR / "bases"


class UnknownBaseError(KeyError):
    """No base of that name exists in resume_bases.yaml."""


@dataclass
class LibraryBase:
    """What the library page says about one base, before anything is generated."""

    base_name: str
    display_name: str
    description: str
    #: Archetype labels whose postings are positioned onto this base - derived
    #: from role_targeting.yaml, never a second taxonomy.
    best_for: list[str] = field(default_factory=list)
    #: Which archetype frames the base when there is no posting.
    library_archetype_label: str = ""
    last_generated_at: datetime | None = None
    pdf_filename: str = ""
    docx_filename: str = ""


@dataclass
class GeneratedBase:
    """One rendered base: where it went, and what is on the page."""

    base: LibraryBase
    content: ResumeContent
    pdf_path: Path
    docx_path: Path
    generated_at: datetime

    @property
    def entry_count(self) -> int:
        return len(self.content.sections)

    @property
    def bullet_count(self) -> int:
        return sum(len(s.bullets) for s in self.content.sections)


def list_bases() -> list[LibraryBase]:
    """Every base in resume_bases.yaml, in file order."""
    return [_describe(spec) for spec in load_base_config().get("bases", [])]


def get_base(base_name: str) -> LibraryBase:
    return _describe(_spec(base_name))


def build_content(base_name: str, *, bank: EvidenceBankService | None = None) -> ResumeContent:
    """The base as a document, fitted to one page. Nothing is written.

    Used for the preview, so what the page shows is what the download holds -
    including anything the fitting loop had to remove.
    """
    spec = _spec(base_name)
    content = resume_documents.build_base_content(
        base_name, target=RoleTargetProfileBuilder().for_base(spec), bank=bank
    )
    fit = fit_to_one_page(content, resume_documents.render_pdf_bytes)
    content.fit_reductions = list(fit.reductions)
    content.page_count = fit.pages
    return content


def generate(base_name: str, *, bank: EvidenceBankService | None = None) -> GeneratedBase:
    """Render the base to PDF and DOCX from the current Evidence Bank.

    Raises ``UnknownBaseError`` for a name that is not a base, and lets
    ``ResumeOverflowError`` propagate - a base that cannot be fitted to one
    page is an error to show, never a two-page file to serve.
    """
    spec = _spec(base_name)
    bank = bank or get_evidence_bank()
    content = resume_documents.build_base_content(
        base_name, target=RoleTargetProfileBuilder().for_base(spec), bank=bank
    )
    pdf_path, docx_path = _paths(base_name)
    resume_documents.write_fitted(content, pdf_path, docx_path)
    base = _describe(spec)
    base.last_generated_at = _mtime(pdf_path)
    return GeneratedBase(
        base=base,
        content=content,
        pdf_path=pdf_path,
        docx_path=docx_path,
        generated_at=base.last_generated_at or datetime.now(UTC),
    )


def filename_for(base_name: str, extension: str) -> str:
    """``Jordan_Lee_Software_Engineering_Base_Resume.pdf``.

    Built from the display name and the candidate's own name - never the
    internal base ID, which is Job Hunt OS vocabulary.
    """
    spec = _spec(base_name)
    name = str(get_evidence_bank().candidate.get("name", "Resume"))
    label = str(spec.get("display_name") or "Base")
    return f"{_slug(name)}_{_slug(label)}_Base_Resume.{extension}"


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _spec(base_name: str) -> dict[str, Any]:
    """Strict lookup. The builder falls back to the first base for an unknown
    name so a job never renders empty; the library must instead refuse, or a
    typo in a URL would download a different resume under the wrong name."""
    for spec in load_base_config().get("bases", []):
        if spec.get("name") == base_name:
            return dict(spec)
    raise UnknownBaseError(base_name)


def _describe(spec: dict[str, Any]) -> LibraryBase:
    base_name = str(spec.get("name") or "")
    builder = RoleTargetProfileBuilder()
    archetype = builder.spec_for(str(spec.get("library_archetype") or ""))
    pdf_path, _ = _paths(base_name)
    return LibraryBase(
        base_name=base_name,
        display_name=str(spec.get("display_name") or base_name.replace("_", " ").title()),
        description=str(spec.get("description") or ""),
        best_for=builder.archetype_labels_for_base(base_name),
        library_archetype_label=str(archetype.get("label") or ""),
        last_generated_at=_mtime(pdf_path),
        pdf_filename=filename_for(base_name, "pdf"),
        docx_filename=filename_for(base_name, "docx"),
    )


def _paths(base_name: str) -> tuple[Path, Path]:
    # The internal name is fine on disk - that path is never shown or served
    # under its own name; downloads use filename_for().
    return BASE_DIR / f"{base_name}.pdf", BASE_DIR / f"{base_name}.docx"


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    return re.sub(r"_+", "_", slug) or "Resume"
