"""The public demo runs end to end on fictional data, and only on fictional data.

The repository ships a SYNTHETIC Evidence Bank (Jordan Lee, fictional employers
and projects). These tests prove the real pipeline works on it - the bank
validates, every base resume renders on one page through the same builder and
claim gate a real resume uses - and that the demo bootstrap refuses to write
anywhere it could mix with real career data.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

from app.config import DATA_DIR, REPO_ROOT
from app.evidence.claim_validator import SafeClaimValidator
from app.evidence.service import EvidenceBankService
from app.models.ingestion import Job
from app.services import resume_library
from app.services.demo_bootstrap import DemoBootstrapRefused, bootstrap_demo
from app.services.import_service import ManualImportService
from app.services.resume_documents import render_pdf_bytes
from app.services.resume_fitting import count_pages

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import validate_evidence_bank  # noqa: E402


@pytest.fixture(scope="module")
def bank() -> EvidenceBankService:
    return EvidenceBankService(DATA_DIR)


def test_the_shipped_bank_is_the_synthetic_demo(bank):
    assert bank.is_synthetic_demo
    assert bank.candidate["name"] == "Jordan Lee"
    assert bank.candidate["email_personal"].endswith("@example.com")
    for record in bank.experiences.values():
        assert "(Fictional)" in record["organization"]


def test_the_synthetic_bank_passes_the_evidence_bank_validator():
    report = validate_evidence_bank.validate(DATA_DIR)
    assert report.errors == []
    assert report.checks_run and report.checks_passed == report.checks_run


@pytest.mark.parametrize("base", [b.base_name for b in resume_library.list_bases()])
def test_every_base_resume_renders_one_page(bank, base):
    content = resume_library.build_content(base, bank=bank)
    assert count_pages(render_pdf_bytes(content)) == 1
    assert content.name == "Jordan Lee"
    experiences = [s.heading for s in content.sections if s.kind == "EXPERIENCE"]
    # Every experience is core and renders newest first.
    assert len(experiences) == 3
    assert experiences[0].startswith("Northbridge University")
    rendered = " ".join(b for s in content.sections for b in s.bullets)
    assert not re.search(r"\b(EXP|EV|SC|DNC|PRJ|M)-[A-Z0-9-]+\b", rendered)


def test_unsupported_keywords_stay_unsupported(bank):
    validator = SafeClaimValidator(bank)
    assert validator.validate("Built RAG pipelines deployed to production.").verdict == "UNSUPPORTED"
    assert validator.validate("Built Airflow DAGs for nightly loads.").verdict == "UNSUPPORTED"


def test_planned_project_never_reaches_a_resume(bank):
    for base in resume_library.list_bases():
        content = resume_library.build_content(base.base_name, bank=bank)
        assert all("Portfolio Chatbot" not in s.heading for s in content.sections)


def test_bootstrap_fills_an_empty_database(session, bank):
    result = bootstrap_demo(session, bank=bank)
    assert result.jobs >= 5
    assert result.applications == 3
    assert result.contacts == 1
    names = {j.company_name for j in session.query(Job).all()}
    assert all(name.endswith("(Demo)") for name in names)


def test_bootstrap_refuses_a_database_that_already_has_jobs(session, bank):
    ManualImportService(session).import_text(
        company_name="Someone Else", title="Data Analyst", description="SQL and Python."
    )
    with pytest.raises(DemoBootstrapRefused):
        bootstrap_demo(session, bank=bank)


def test_bootstrap_refuses_a_bank_that_is_not_the_demo(session, tmp_path):
    real_like = tmp_path / "data"
    real_like.mkdir()
    for path in DATA_DIR.glob("*.yaml"):
        (real_like / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (real_like / "config").mkdir()
    for path in (DATA_DIR / "config").glob("*.yaml"):
        (real_like / "config" / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    candidate = yaml.safe_load((real_like / "candidate.yaml").read_text(encoding="utf-8"))
    candidate.pop("is_synthetic_demo")
    (real_like / "candidate.yaml").write_text(yaml.safe_dump(candidate), encoding="utf-8")
    with pytest.raises(DemoBootstrapRefused):
        bootstrap_demo(session, bank=EvidenceBankService(Path(real_like)))
