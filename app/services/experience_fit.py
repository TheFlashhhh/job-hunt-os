"""Experience Fit — a visible, conservative signal the priority score cannot hide.

Slice 5's live-verification audit found the deterministic pipeline's
``experience_level`` tag is unreliable: multiple postings tagged
``EARLY_CAREER`` actually required 2-8+ years on the live JD (several
live postings — see ``IMPLEMENTATION_LOG.md`` D-26). A job's
priority score must never be the only place experience fit shows up, and the
underlying JD text is the most trustworthy signal available without an LLM
call — it is what actually contradicted the stored tag every time this
session.

This module is deliberately conservative and deterministic only: a regex
scan over the already-collected ``Job.description`` text for explicit
"N years" language, falling back to the structured ``experience_level``
field only when the text gives no number. It does not call an LLM and does
not replace or retune the priority score.
"""

from __future__ import annotations

import re
from enum import Enum

from app.models.ingestion import Job

_YEARS_PATTERN = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2}\s*\+?)?\s*years?\b",
    re.IGNORECASE,
)
#: Only trust a number when it appears near a phrase that actually means
#: "you need this much professional experience" — a bare "3 years" near
#: "3 years running" (a project duration) is not a requirement.
_EXPERIENCE_CONTEXT = re.compile(
    r"(years?\s+of\s+(?:professional\s+|relevant\s+|industry\s+|work\s+)?experience"
    r"|years?\s+(?:in|working)"
    r"|\d\s*\+?\s*years?\s+(?:of\s+)?experience)",
    re.IGNORECASE,
)


class ExperienceFit(str, Enum):
    ENTRY_COMPATIBLE = "ENTRY_COMPATIBLE"
    EARLY_CAREER = "EARLY_CAREER"
    REACH_2_PLUS = "REACH_2_PLUS"
    STRETCH_3_PLUS = "STRETCH_3_PLUS"
    SENIOR = "SENIOR"
    UNKNOWN = "UNKNOWN"


_SENIOR_TITLE_MARKERS = ("senior", "staff", "principal", "lead ", "director", " ii", " iii")


def _min_years_from_text(description: str) -> int | None:
    """Smallest explicit "N years of experience"-shaped number in the JD text."""
    if not description:
        return None
    found: list[int] = []
    for match in _YEARS_PATTERN.finditer(description):
        window = description[max(0, match.start() - 40) : match.end() + 30]
        if not _EXPERIENCE_CONTEXT.search(window):
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if 0 < value <= 20:
            found.append(value)
    return min(found) if found else None


def _band_for_years(years: int) -> ExperienceFit:
    if years >= 5:
        return ExperienceFit.SENIOR
    if years >= 3:
        return ExperienceFit.STRETCH_3_PLUS
    if years >= 2:
        return ExperienceFit.REACH_2_PLUS
    return ExperienceFit.ENTRY_COMPATIBLE


def classify(job: Job) -> tuple[ExperienceFit, str]:
    """Return (category, reason). Conservative: prefers real JD text over tags."""
    analysis = job.latest_analysis
    text_min = _min_years_from_text(job.description or "")
    if text_min is not None:
        band = _band_for_years(text_min)
        return band, f'JD text requires {text_min}+ years ("years of experience" phrasing found)'

    if analysis is not None and analysis.years_experience_min is not None:
        band = _band_for_years(analysis.years_experience_min)
        return band, f"Parsed requirement: {analysis.years_experience_min}+ years"

    title = (job.title or "").lower()
    if any(marker in title for marker in _SENIOR_TITLE_MARKERS):
        return (
            ExperienceFit.SENIOR,
            "Title contains a senior/staff/principal/level-II+ marker",
        )

    level = job.experience_level
    if level == "INTERNSHIP":
        return ExperienceFit.ENTRY_COMPATIBLE, "Tagged as an internship"
    if level in ("NEW_GRAD", "ENTRY_LEVEL"):
        return ExperienceFit.ENTRY_COMPATIBLE, f"Tagged {level}, no contradicting JD text found"
    if level == "EARLY_CAREER":
        return (
            ExperienceFit.EARLY_CAREER,
            "Tagged EARLY_CAREER, no explicit years requirement found in the JD text — "
            "verify before treating as confirmed entry-level",
        )
    if level == "MID_LEVEL":
        return ExperienceFit.REACH_2_PLUS, "Tagged MID_LEVEL"
    if level in ("SENIOR", "STAFF_PLUS"):
        return ExperienceFit.SENIOR, f"Tagged {level}"
    return ExperienceFit.UNKNOWN, "No years requirement in the JD text and no experience tag"
