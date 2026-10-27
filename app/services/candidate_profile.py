"""The candidate's own contact details, in one place.

Every applicant-facing document carries the same header: name, location, phone,
email, LinkedIn, GitHub. Before this module each renderer reached into
``candidate.yaml`` and formatted that line itself, so "the profile" was really
four copies of the same formatting spread across the PDF renderer, the DOCX
renderer, the API serializer and whatever came next.

That mattered on 2026-08-24, when the LinkedIn URL in the Bank turned out to be
wrong — not stale, wrong: it was not the candidate's profile, and it had been printed on
every generated resume since the renderer existed. One wrong value, and the fix
had to be correct in four places at once or it would be half-fixed.

So the value is stored once (`data/candidate.yaml`) and shaped once (here).
Renderers ask for a profile and get both forms of every link:

``linkedin_display``   ``linkedin.com/in/jordan-lee``
``linkedin_url``       ``https://www.linkedin.com/in/jordan-lee``

The display form is what a human reads on paper; the URL is what a hyperlink
points at. Documents show the short one — the reference resume does, and
"https://www." on a printed line is noise.

**This module formats. It does not invent.** Every field comes from the Evidence
Bank; a missing field yields an empty string rather than a plausible default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.evidence.service import EvidenceBankService, get_evidence_bank

#: Hosts whose canonical URL form is not simply "https://" + the display value.
#: LinkedIn redirects the bare host to `www`, and a profile link that redirects
#: is a link some ATS parsers drop.
_CANONICAL_HOSTS = {
    "linkedin.com": "www.linkedin.com",
}


@dataclass(frozen=True)
class CandidateProfile:
    """Who the applicant is, as every generated document should state it."""

    name: str
    location: str
    phone: str
    email: str
    linkedin_display: str
    linkedin_url: str
    github_display: str
    github_url: str

    @property
    def contact_line(self) -> str:
        """Phone · email · LinkedIn · GitHub, as printed.

        Location is deliberately absent: the resume template sets it under the
        name, and the DOCX prepends it. Repeating it here put "Boston, MA"
        twice on the DOCX header.
        """
        parts = [self.phone, self.email, self.linkedin_display, self.github_display]
        return " | ".join(p for p in parts if p)

    @property
    def contact_items(self) -> list[tuple[str, str | None]]:
        """The same line as (display, href) pairs, for renderers that can link.

        `href` is None for items that are not links, so a renderer never has to
        guess whether a string is clickable.
        """
        return [
            # The phone number is not linked: a `tel:` URI would have to strip
            # the formatting a human reads it by, and nobody clicks a phone
            # number on a resume.
            (self.phone, None),
            (self.email, f"mailto:{self.email}" if self.email else None),
            (self.linkedin_display, self.linkedin_url or None),
            (self.github_display, self.github_url or None),
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "location": self.location,
            "phone": self.phone,
            "email": self.email,
            "linkedin_display": self.linkedin_display,
            "linkedin_url": self.linkedin_url,
            "github_display": self.github_display,
            "github_url": self.github_url,
            "contact_line": self.contact_line,
        }


def load_candidate_profile(bank: EvidenceBankService | None = None) -> CandidateProfile:
    """Build the profile from the Evidence Bank. The only place that does."""
    record = (bank or get_evidence_bank()).candidate
    linkedin = _clean(record.get("linkedin"))
    github = _clean(record.get("github"))
    return CandidateProfile(
        name=_clean(record.get("name")),
        location=_clean(record.get("location")),
        phone=_clean(record.get("phone")),
        # `preferred_application_email` is UNKNOWN in the Bank and documented as
        # such; the personal address is what the resume has always shown.
        email=_clean(record.get("email_personal")),
        linkedin_display=linkedin,
        linkedin_url=canonical_url(linkedin),
        github_display=github,
        github_url=canonical_url(github),
    )


def alma_mater(bank: EvidenceBankService | None = None) -> str:
    """The candidate's current school - the first education record in the Bank.

    Used only where a shared affiliation is a fact on file (a contact recorded
    as an alum, a professor). Read from the Bank rather than hard-coded, so
    the networking text names whichever school the Bank actually holds.
    """
    education = (bank or get_evidence_bank()).education
    return _clean(education[0].get("institution")) if education else ""


def canonical_url(display: str) -> str:
    """The fully-qualified form of a display-style link.

    ``linkedin.com/in/x`` → ``https://www.linkedin.com/in/x``. A value that
    already carries a scheme is returned as-is, so the Bank can switch to
    storing full URLs without this becoming wrong.
    """
    value = _clean(display).rstrip("/")
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    host, _, path = value.partition("/")
    host = _CANONICAL_HOSTS.get(host.lower(), host)
    return f"https://{host}/{path}" if path else f"https://{host}"


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    # The Bank uses UNKNOWN as a real value for "not established". It is a
    # statement about knowledge, not something to print on a resume.
    return "" if text.upper() == "UNKNOWN" else text
