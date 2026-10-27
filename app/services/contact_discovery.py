"""Who should I contact about this job?

The candidate should not have to already know someone at a target company. That is the gap this
closes — but it closes it with **search strategies a human runs**, not with an
account, a cookie or a scraper.

Two sources, in this order:

**People already in the CRM.** A contact recorded for one role at a company
is a plausible path into another role there. Free, certain, and already consented to.

**Public searches, phrased for you.** Four personas that matter for a specific
job — a BU alum in the same function, a same-function peer, a relevant manager,
a technical recruiter — each rendered as a query and a link. The user opens it,
reads real profiles, and adds whoever is actually right.

**This module never fetches anything.** It builds strings. That is deliberate,
not a limitation waiting to be fixed: the networking experience has to work with
zero external people-discovery source, because every people-discovery source
evaluated so far has been rejected. A returned link is a link — the person
clicks it, in their own browser, in their own session. No LinkedIn account is
automated, no credential is supplied, and nothing is scraped.

Ranking is transparent for the same reason. A persona carries the reasons it is
worth contacting — "BU alum · same function" — rather than a score presented as
objective truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ContactType
from app.models.ingestion import Job
from app.models.misc import Contact, JobContact
from app.services.candidate_profile import alma_mater

#: Function words per role family. These shape a SEARCH QUERY — they never
#: describe the candidate and never reach a resume.
_FUNCTION_TERMS: dict[str, tuple[str, ...]] = {
    "DATA_SCIENTIST": ("Data Scientist", "Machine Learning", "Analytics"),
    "ML_ENGINEER": ("Machine Learning Engineer", "ML Engineer", "Data Scientist"),
    "AI_ENGINEER": ("AI Engineer", "Machine Learning Engineer", "Applied Scientist"),
    "DATA_ENGINEERING": ("Data Engineer", "Analytics Engineer", "Data Platform"),
    "ANALYTICS_ENGINEERING": ("Analytics Engineer", "Data Engineer", "BI Engineer"),
    "DATA_ANALYST": ("Data Analyst", "Product Analyst", "Analytics"),
    "PRODUCT_ANALYTICS": ("Product Analyst", "Data Scientist", "Analytics"),
    "DECISION_SCIENCE": ("Decision Scientist", "Data Scientist", "Analytics"),
    "AI_SOFTWARE_ENGINEER": ("Software Engineer", "AI Engineer", "Machine Learning Engineer"),
    "SWE": ("Software Engineer", "Backend Engineer"),
}
_DEFAULT_TERMS = ("Data Scientist", "Data Analyst", "Analytics")


@dataclass
class SearchStrategy:
    """One persona worth looking for, and how to look."""

    persona: str
    label: str
    why: str
    query: str
    linkedin_url: str
    web_url: str
    #: Best-first among the personas. Ordering, shown with its reasons.
    rank: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "label": self.label,
            "why": self.why,
            "query": self.query,
            "linkedin_url": self.linkedin_url,
            "web_url": self.web_url,
            "rank": self.rank,
        }


@dataclass
class ContactSuggestion:
    """Someone already in the CRM who could be linked to this job."""

    contact_id: str
    name: str
    role: str | None
    company_name: str | None
    relationship_type: str
    status: str
    already_linked: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "role": self.role,
            "company_name": self.company_name,
            "relationship_type": self.relationship_type,
            "status": self.status,
            "already_linked": self.already_linked,
            "reasons": self.reasons,
        }


@dataclass
class DiscoveryResult:
    company_name: str
    function_label: str
    existing_contacts: list[ContactSuggestion]
    strategies: list[SearchStrategy]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name,
            "function_label": self.function_label,
            "existing_contacts": [c.as_dict() for c in self.existing_contacts],
            "strategies": [s.as_dict() for s in self.strategies],
            "note": self.note,
        }


class ContactDiscoveryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def for_job(self, job: Job) -> DiscoveryResult:
        company = (job.company_name or "").strip()
        terms = _FUNCTION_TERMS.get(job.role_family or "", _DEFAULT_TERMS)
        function = terms[0]

        return DiscoveryResult(
            company_name=company,
            function_label=function,
            existing_contacts=self._existing(job),
            strategies=self._strategies(company, terms),
            note=(
                "These open a search in your browser. Read the real profiles, pick the "
                "person yourself, and add them here. Nothing is scraped and no message "
                "is ever sent for you."
            ),
        )

    # -- people already known --------------------------------------------
    def _existing(self, job: Job) -> list[ContactSuggestion]:
        linked_ids = {
            row.contact_id
            for row in self.session.execute(
                select(JobContact).where(JobContact.job_id == job.job_id)
            ).scalars()
        }
        target = _normalize(job.company_name or "")
        rows = self.session.execute(select(Contact).where(Contact.is_demo.is_(False))).scalars()

        out: list[ContactSuggestion] = []
        for contact in rows:
            linked = contact.contact_id in linked_ids
            same_company = bool(target) and _normalize(contact.company_name or "") == target
            if not (linked or same_company):
                continue
            out.append(
                ContactSuggestion(
                    contact_id=contact.contact_id,
                    name=contact.name,
                    role=contact.role,
                    company_name=contact.company_name,
                    relationship_type=contact.relationship_type,
                    status=contact.status,
                    already_linked=linked,
                    reasons=_reasons(contact, same_company=same_company, linked=linked),
                )
            )
        # Linked first, then the strongest relationship. Both are facts on file.
        out.sort(key=lambda c: (not c.already_linked, _relationship_rank(c.relationship_type)))
        return out

    # -- searches a human runs -------------------------------------------
    def _strategies(self, company: str, terms: tuple[str, ...]) -> list[SearchStrategy]:
        if not company:
            return []
        function = terms[0]
        alternates = " OR ".join(f'"{t}"' for t in terms[:3])

        specs = [
            (
                "BU_ALUM_SAME_FUNCTION",
                f"{alma_mater()} alumni at {company} in {function}",
                "Strongest opening: a real shared affiliation and the same function.",
                f'"{company}" "{alma_mater()}" ({alternates})',
            ),
            (
                "SAME_FUNCTION_PEER",
                f"{company} {function}s",
                "A peer can tell you what the team actually works on.",
                f'"{company}" ({alternates})',
            ),
            (
                "RELEVANT_MANAGER",
                f"{company} data and analytics managers",
                "Closer to the hiring decision, and worth a shorter, sharper message.",
                f'"{company}" ("Data Science Manager" OR "Analytics Manager" '
                f'OR "Head of Data" OR "Director of Data")',
            ),
            (
                "TECHNICAL_RECRUITER",
                f"{company} technical recruiters",
                "Least warm, most reachable — and they know which reqs are real.",
                f'"{company}" ("Technical Recruiter" OR "Recruiter" OR "Talent Acquisition")',
            ),
        ]

        return [
            SearchStrategy(
                persona=persona,
                label=label,
                why=why,
                query=query,
                linkedin_url=(
                    f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(query)}"
                ),
                web_url=f"https://duckduckgo.com/?q={quote_plus(query + ' site:linkedin.com/in')}",
                rank=index + 1,
            )
            for index, (persona, label, why, query) in enumerate(specs)
        ]


# ---------------------------------------------------------------------------
# Reasons, not scores
# ---------------------------------------------------------------------------

#: Ordering only, and only over facts already recorded on the contact.
_RELATIONSHIP_RANK = {
    ContactType.BU_ALUM.value: 0,
    ContactType.FORMER_COLLEAGUE.value: 1,
    ContactType.TEAM_MEMBER.value: 2,
    ContactType.HIRING_MANAGER.value: 3,
    ContactType.PROFESSOR.value: 4,
    ContactType.RECRUITER.value: 5,
    ContactType.FRIEND_CLASSMATE.value: 6,
    ContactType.OTHER.value: 7,
}


def _relationship_rank(relationship: str | None) -> int:
    return _RELATIONSHIP_RANK.get(relationship or "", 9)


def _reasons(contact: Contact, *, same_company: bool, linked: bool) -> list[str]:
    """Why this person is worth contacting, in words a human can check.

    Every reason restates something recorded on the contact. Nothing here
    infers a relationship that was not entered, and no number is presented as
    an objective measure of who to contact.
    """
    reasons: list[str] = []
    if linked:
        reasons.append("Already linked to this job")
    if same_company:
        reasons.append(f"Works at {contact.company_name}")
    if contact.relationship_type == ContactType.BU_ALUM.value:
        reasons.append(f"{alma_mater()} alum")
    if contact.relationship_type == ContactType.RECRUITER.value:
        reasons.append("Recruiter")
    if contact.relationship_type == ContactType.HIRING_MANAGER.value:
        reasons.append("Hiring manager")
    if contact.role:
        reasons.append(contact.role)
    return reasons


def _normalize(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in name or "")
    return " ".join(cleaned.split())
