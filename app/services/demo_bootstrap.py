"""Bootstrap a fully synthetic demo database for the public repository.

``python -m app.cli demo bootstrap`` turns a fresh clone into something that can
be clicked through: fictional full-time postings run through the real pipeline
(screening, fit, classification), a few applications moved along the funnel,
and one fictional contact with a drafted, unsent follow-up.

Unlike ``demo seed`` - which adds a handful of ``is_demo`` rows to a real
database, excluded from every KPI - this fills an EMPTY database whose whole
content is synthetic, so the rows are ordinary and the dashboard has something
to count. That is only safe because of two refusals, checked before anything
is written:

* the Evidence Bank must declare ``is_synthetic_demo: true`` - a real bank never
  does, so a real install cannot be bootstrapped by mistake;
* the database must hold no non-demo job - it never adds to existing data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.enums import ApplicationStatus, ContactType, ContactWarmth
from app.models.ingestion import Job
from app.services.crm import ApplicationService
from app.services.import_service import ManualImportService
from app.services.people import PeopleService


class DemoBootstrapRefused(RuntimeError):
    """The database or Evidence Bank is not one the demo may write to."""


@dataclass
class BootstrapResult:
    jobs: int
    applications: int
    contacts: int


_TODAY = date.today()

#: Fictional employers on example.com. None of these is a real posting.
DEMO_POSTINGS: list[dict[str, str]] = [
    {
        "company_name": "Harborline Analytics (Demo)",
        "title": "Data Scientist, New Grad 2027",
        "location": "Boston, MA",
        "url": "https://example.com/harborline/jobs/101",
        "description": """Harborline is hiring a new-graduate Data Scientist to start in January 2027.

What you'll do
- Build predictive models in Python with scikit-learn and evaluate them rigorously
- Write SQL against our product event warehouse
- Design cross-validation that avoids leakage across customers
- Explain model behaviour to product partners

Requirements
- MS in Data Science, Statistics or a related field (completed by start date)
- Strong Python, pandas and SQL
- Experience with model evaluation and feature engineering
- 0-2 years of experience
""",
    },
    {
        "company_name": "Meridian Data Systems (Demo)",
        "title": "Data Engineer I",
        "location": "Remote (US)",
        "url": "https://example.com/meridian/jobs/202",
        "description": """Meridian is hiring an entry-level Data Engineer, full time, starting early 2027.

Responsibilities
- Build streaming ingestion with Kafka and batch transforms with Spark
- Own schema validation and data-quality checks on incoming feeds
- Model analytics tables in PostgreSQL
- Containerise jobs with Docker

Requirements
- Python and SQL
- Experience with ETL/ELT pipelines
- Bachelor's or Master's in Computer Science or a related field
- New graduates welcome
""",
    },
    {
        "company_name": "Copperleaf Software (Demo)",
        "title": "Software Engineer, Backend (New Grad)",
        "location": "New York, NY",
        "url": "https://example.com/copperleaf/jobs/303",
        "description": """Copperleaf builds scheduling software for clinics. Full-time, new-grad 2027.

You will
- Build REST APIs in Python (FastAPI) backed by PostgreSQL
- Write automated tests with pytest
- Tune SQL queries and ORM access paths
- Ship through CI with Docker

Requirements
- Python, SQL, REST APIs
- Unit and integration testing
- 0-2 years of professional experience
""",
    },
    {
        "company_name": "Brightwater Health (Demo)",
        "title": "Product Data Analyst",
        "location": "Boston, MA",
        "url": "https://example.com/brightwater/jobs/404",
        "description": """Brightwater is hiring a Product Data Analyst, full time.

What you'll do
- Analyse funnels, retention and engagement from event logs
- Build dashboards in Tableau and define product metrics
- Present findings and recommendations to product leads

Requirements
- SQL and Python
- Cohort and retention analysis
- Clear written and verbal communication
- Entry level; new graduates welcome
""",
    },
    {
        "company_name": "Tidewater Robotics (Demo)",
        "title": "Machine Learning Engineer, Early Career",
        "location": "San Francisco, CA",
        "url": "https://example.com/tidewater/jobs/505",
        "description": """Tidewater is hiring an early-career ML Engineer, full time from January 2027.

Responsibilities
- Train and evaluate models with PyTorch and scikit-learn
- Build inference services and data pipelines
- Track experiments and model evaluation results

Requirements
- Python, PyTorch, model evaluation
- Docker and SQL
- 0-2 years of experience
""",
    },
    {
        "company_name": "Northgate Capital (Demo)",
        "title": "Senior Staff Machine Learning Scientist",
        "location": "New York, NY",
        "url": "https://example.com/northgate/jobs/606",
        "description": """Northgate is hiring a Senior Staff ML Scientist.

Requirements
- 10+ years of industry machine learning experience
- Track record of leading large technical teams
- PhD required
""",
    },
]

#: (company, status, next action) for the applications the demo moves along.
DEMO_PIPELINE: list[tuple[str, ApplicationStatus, str]] = [
    ("Harborline Analytics (Demo)", ApplicationStatus.APPLIED, "Follow up with the recruiter"),
    ("Copperleaf Software (Demo)", ApplicationStatus.RECRUITER_SCREEN, "Prepare for the screen"),
    ("Meridian Data Systems (Demo)", ApplicationStatus.READY_TO_APPLY, "Submit the application"),
]


def bootstrap_demo(session: Session, *, bank: EvidenceBankService | None = None) -> BootstrapResult:
    """Fill an empty database with the synthetic demo. Refuses anything else."""
    bank = bank or get_evidence_bank()
    if not bank.is_synthetic_demo:
        raise DemoBootstrapRefused(
            "The loaded Evidence Bank is not the synthetic demo bank "
            "(data/candidate.yaml has no `is_synthetic_demo: true`). Refusing to "
            "write demo rows next to real career data."
        )
    real_jobs = session.execute(
        select(func.count()).select_from(Job).where(Job.is_demo.is_(False))
    ).scalar_one()
    if real_jobs:
        raise DemoBootstrapRefused(
            f"The database already holds {real_jobs} job(s). The demo only bootstraps "
            "an empty database; delete it (or point JOBHUNT_DB_URL elsewhere) to start over."
        )

    importer = ManualImportService(session)
    jobs: dict[str, Job] = {}
    for spec in DEMO_POSTINGS:
        outcome = importer.import_text(
            company_name=spec["company_name"],
            title=spec["title"],
            description=spec["description"],
            location=spec["location"],
            url=spec["url"],
            posted_date=_TODAY - timedelta(days=3),
        )
        if outcome.job is not None:
            jobs[spec["company_name"]] = outcome.job

    applications = ApplicationService(session)
    moved = 0
    for company, status, action in DEMO_PIPELINE:
        job = jobs.get(company)
        if job is None:
            continue
        applications.transition(job, status, note="Demo data.")
        applications.set_next_action(job, action, _TODAY + timedelta(days=4))
        moved += 1

    people = PeopleService(session)
    contact = people.create_contact(
        name="Sam Rivera (Demo)",
        company_name="Copperleaf Software (Demo)",
        role="Engineering Manager",
        contact_type=ContactType.HIRING_MANAGER,
        warmth=ContactWarmth.COLD,
        notes="Fictional contact for the public demo.",
        next_follow_up=_TODAY + timedelta(days=2),
    )
    copperleaf = jobs.get("Copperleaf Software (Demo)")
    if copperleaf is not None:
        people.link_to_job(contact, copperleaf)

    session.flush()
    return BootstrapResult(jobs=len(jobs), applications=moved, contacts=1)
