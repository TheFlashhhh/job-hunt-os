"""DEMO data for UI development.

Every row is marked ``is_demo=True`` and is excluded from all funnel analytics,
KPIs and source performance. No real application history is ever invented.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ingestion import Company, Job
from app.services.import_service import ManualImportService

_TODAY = date.today()

DEMO_JOBS: list[dict[str, Any]] = [
    {
        "company_name": "Harborline Analytics (DEMO)",
        "title": "Data Science Intern, Fall 2026",
        "location": "Boston, MA",
        "url": "https://boards.greenhouse.io/harborlinedemo/jobs/1000001",
        "deadline": _TODAY + timedelta(days=5),
        "description": """About the role
We are hiring a Fall 2026 Data Science Intern in Boston, MA. This is a part-time
position, 20 hours per week, running September through December 2026.

What you'll do
- Build and maintain data pipelines using Python and SQL
- Analyze product usage and event-log data to identify engagement drivers
- Partner with product managers to define and debug metrics
- Present findings to stakeholders

Requirements
- Currently enrolled in a Master's program in Data Science, Statistics or a related field
- Strong proficiency in Python and SQL
- Experience with pandas and data cleaning
- Excellent written and verbal communication

Preferred qualifications
- Experience with Spark or Kafka
- Familiarity with geospatial analysis
""",
    },
    {
        "company_name": "Meridian Data Systems (DEMO)",
        "title": "Data Engineer, New Grad 2027",
        "location": "Remote (US)",
        "url": "https://jobs.lever.co/meridiandemo/aaaa1111-bbbb-2222-cccc-333344445555",
        "description": """The role
Meridian is hiring a new-graduate Data Engineer to start in early 2027. Fully remote
within the United States.

Responsibilities
- Design and operate streaming data pipelines
- Build batch analytics jobs and monitor data quality
- Model time-series analytics tables
- Support alerting and pipeline observability

Minimum qualifications
- Bachelor's or Master's degree, graduating by mid-2027
- 0-2 years of experience
- Proficiency in Python and SQL
- Exposure to Kafka, Spark or an equivalent streaming stack
- Experience with containerized deployment

Preferred qualifications
- Experience with Airflow and dbt
- Familiarity with AWS or GCP
- Experience with Snowflake or Databricks

We sponsor work visas for qualified candidates.
""",
    },
    {
        "company_name": "Vantage AI Labs (DEMO)",
        "title": "AI Engineer, Early Career",
        "location": "San Francisco, CA",
        "url": "https://jobs.ashbyhq.com/vantagedemo/9999aaaa-8888-bbbb-7777-cccc6666dddd",
        "description": """About Vantage
We build LLM-powered products for enterprise customers.

What you'll do
- Design and ship production RAG pipelines over customer knowledge bases
- Build multi-agent orchestration workflows using LangGraph
- Own LLM evaluation harnesses and hallucination benchmarking
- Optimize inference cost and latency

Requirements
- 2+ years building production LLM applications
- Deep experience with vector databases and semantic retrieval
- Strong Python
- Experience with prompt engineering at scale

Applicants must be authorized to work in the United States without sponsorship.
""",
    },
    {
        "company_name": "Copley Product Group (DEMO)",
        "title": "Senior Staff Machine Learning Engineer",
        "location": "Austin, TX",
        "description": """We are seeking a Senior Staff ML Engineer with 10+ years of experience
to lead our modeling organization. You will define technical strategy and mentor
a team of senior engineers.

Requirements
- 10+ years of industry machine learning experience
- Track record of leading large technical teams
- PhD preferred
""",
    },
]


def seed_demo_data(session: Session) -> int:
    """Insert demo jobs. Idempotent — re-running does not duplicate."""
    service = ManualImportService(session)
    count = 0
    for spec in DEMO_JOBS:
        outcome = service.import_text(
            company_name=spec["company_name"],
            title=spec["title"],
            description=spec["description"],
            location=spec.get("location"),
            url=spec.get("url"),
            deadline=spec.get("deadline"),
            is_demo=True,
        )
        if outcome.job is not None:
            outcome.job.is_demo = True
            if outcome.job.application:
                outcome.job.application.is_demo = True
            if outcome.job.company:
                outcome.job.company.is_demo = True
            count += 1
    return count


def clear_demo_data(session: Session) -> int:
    """Remove every demo job and demo-only company."""
    jobs = session.execute(select(Job).where(Job.is_demo.is_(True))).scalars().all()
    for job in jobs:
        session.delete(job)
    session.flush()

    companies = session.execute(select(Company).where(Company.is_demo.is_(True))).scalars().all()
    for company in companies:
        if not company.jobs:
            session.delete(company)
    return len(jobs)
