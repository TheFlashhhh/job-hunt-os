"""Is this posting an internship, a co-op, or a student-only role?

Job Hunt OS became full-time-only on 2026-09-08. The candidate is not pursuing
internships, co-ops or CPT employment, so a posting that is one of those is not
a target — and the ones already in the database were purged at his direction.

Getting this wrong is expensive in both directions, so the answer is a verdict
with a stated reason rather than a boolean:

``CONFIRMED_INTERNSHIP``   the posting says so
``FULL_TIME``              positively identified as full-time
``AMBIGUOUS``              genuinely unclear — never purged automatically

**The substring trap.** "intern" is inside *international*, *internal*,
*internship program* (in a benefits paragraph), and *Internet*. Every pattern
here is anchored, and the corpus contains real full-time postings at companies
whose boilerplate advertises their internship programme. A title is read as a
title; a body is read only for phrases about *this* role.

**The stored `employment_type` is a hint, not proof.** It came from a classifier
replaced twice since, and the first dry run caught it calling "Global Senior
Manager, Early Talent Programs" a CO_OP — a full-time job *managing* the co-op
programme. On its own it yields AMBIGUOUS; it confirms only when the title or
the description agrees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONFIRMED_INTERNSHIP = "CONFIRMED_INTERNSHIP"
FULL_TIME = "FULL_TIME"
AMBIGUOUS = "AMBIGUOUS"

#: Employment types the active search excludes outright.
INTERN_EMPLOYMENT_TYPES = frozenset({"INTERNSHIP", "CO_OP"})

#: A title that names the role as an internship or co-op. Anchored so
#: "International Data Analyst" and "Internal Tools Engineer" do not match:
#: `intern` must be a whole word or immediately followed by `ship`.
_TITLE_INTERNSHIP = re.compile(
    r"(?:\bintern\b|\binterns\b|\binternship\b|\binternships\b|"
    r"\bco[- ]?op\b|\bcoop\b|\bco[- ]?ops\b|"
    r"\bapprentice(?:ship)?\b|\bpracticum\b|\btrainee\b|"
    r"\bcampus\b|\bstudent\b|\bfellowship\b|\bextern(?:ship)?\b|"
    r"\bsummer analyst\b|\bindustrial placement\b|\bplacement year\b|"
    r"\bworking student\b|\bwerkstudent\b)",
    re.I,
)

#: Title wording that positively says full-time / post-graduation.
_TITLE_FULL_TIME = re.compile(
    r"\b(new grad(?:uate)?|university grad(?:uate)?|entry[- ]level|"
    r"early career|full[- ]time|graduate program(?:me)?|rotational program)\b",
    re.I,
)

#: Titles whose SUBJECT is interns rather than being one. "Early Careers &
#: Interns Specialist", "Coordinator, Emerging Talent Recruiting" and "Head of
#: Early Career Recruiting" are full-time jobs about running an internship
#: programme, and the first dry run had all three queued for deletion.
_TITLE_ABOUT_INTERNS = re.compile(
    r"\b(recruit\w+|talent acquisition|university relations|campus relations|"
    r"program manager|programme manager|coordinator|specialist|"
    r"partner|head of|director of|manager of)\b",
    re.I,
)

#: Body phrases about THIS role being an internship. Deliberately narrow —
#: "we also run a summer internship program" in a benefits paragraph is a fact
#: about the company, not about the job being advertised.
_BODY_INTERNSHIP = (
    re.compile(r"\bthis (?:internship|co[- ]?op|placement)\b", re.I),
    re.compile(r"\b(?:the|our) (?:intern|internship|co[- ]?op) (?:role|position|program)\b", re.I),
    re.compile(r"\bmust be (?:currently )?enrolled\b", re.I),
    re.compile(r"\bcurrently (?:enrolled|pursuing) (?:in |a )?(?:an? )?(?:degree|bachelor|master|phd|undergraduate|graduate)", re.I),
    re.compile(r"\breturning to (?:school|university|your studies)\b", re.I),
    re.compile(r"\brising (?:junior|senior|sophomore)\b", re.I),
    re.compile(r"\b(?:summer|fall|spring|winter) 20\d\d intern(?:ship)?\b", re.I),
    re.compile(r"\bintern(?:ship)? (?:runs|lasts|duration|program dates)\b", re.I),
    re.compile(r"\b(?:10|11|12|13|14|16)[- ]week (?:internship|program|co[- ]?op)\b", re.I),
    # "academic credit" only. `\bfor[- ]credit\b` matched "risk management,
    # regulatory reporting … for credit" in a Snowflake solutions-engineer
    # posting and "subject-matter authority for Credit & Collections" in a
    # Databricks manager posting. Neither has anything to do with a university.
    re.compile(r"\b(?:academic|course|university|school) credit\b", re.I),
)

#: Body phrases that positively say full-time.
_BODY_FULL_TIME = (
    re.compile(r"\bthis is a full[- ]time (?:role|position|opportunity)\b", re.I),
    re.compile(r"\bfull[- ]time (?:role|position|employment|opportunity)\b", re.I),
    re.compile(r"\b(?:new grad|university graduate|entry[- ]level) (?:role|position|hiring)\b", re.I),
)


@dataclass(frozen=True)
class InternshipVerdict:
    """What this posting is, and the words that decided it."""

    verdict: str
    reason: str = ""
    evidence: str = ""
    #: Which signals fired, for the dry-run report.
    signals: tuple[str, ...] = ()

    @property
    def is_internship(self) -> bool:
        return self.verdict == CONFIRMED_INTERNSHIP

    @property
    def purgeable(self) -> bool:
        """Only a confirmed internship is ever deleted without a human look."""
        return self.verdict == CONFIRMED_INTERNSHIP


def classify_internship(
    *,
    title: str | None,
    description: str | None = None,
    employment_type: str | None = None,
    search_lane: str | None = None,
) -> InternshipVerdict:
    """Decide whether a posting is student employment.

    `search_lane` is a hint and never decisive on its own: the old
    `FALL_2026_INTERNSHIP_COOP` bucket was assigned by a classifier that has
    since been replaced twice, and it contains genuinely full-time postings.
    """
    title_text = (title or "").strip()
    body = (description or "")[:12000]
    signals: list[str] = []

    declared_intern = (employment_type or "").upper() in INTERN_EMPLOYMENT_TYPES
    if declared_intern:
        signals.append("EMPLOYMENT_TYPE")

    # 1. The title names it. Checked BEFORE employment_type, because the stored
    #    employment_type came from a classifier that has been replaced twice and
    #    is demonstrably wrong: the dry run found it labelling "Global Senior
    #    Manager, Early Talent Programs" as CO_OP (a full-time job MANAGING the
    #    co-op programme), "Bilingual Hybrid Development Representative" as
    #    INTERNSHIP (a sales role), and "Assistant Production Artist" the same.
    # A title that is ABOUT interns is not an internship. Checked once, and used
    # for both the title and the body rules below.
    about_interns = bool(_TITLE_ABOUT_INTERNS.search(title_text))
    if about_interns:
        signals.append("TITLE_ABOUT_INTERNS")

    title_match = _TITLE_INTERNSHIP.search(title_text)
    if title_match:
        # ...unless the title ALSO says full-time, which happens with
        # "Campus Recruiting — Full-Time New Grad", or the role is about
        # running the programme rather than being in it.
        if not _TITLE_FULL_TIME.search(title_text) and not about_interns:
            return InternshipVerdict(
                verdict=CONFIRMED_INTERNSHIP,
                reason=f"The title names a student role: {title_match.group(0)!r}.",
                evidence=title_text[:120],
                signals=(*signals, "TITLE"),
            )
        signals.append("TITLE_MIXED")

    # 3. The body says this role is one — unless the title says the role is
    #    about running the programme, in which case its description naturally
    #    talks about interns throughout.
    for pattern in () if about_interns else _BODY_INTERNSHIP:
        match = pattern.search(body)
        if match:
            return InternshipVerdict(
                verdict=CONFIRMED_INTERNSHIP,
                reason=f"The description says {match.group(0)!r} about this role.",
                evidence=_sentence_around(body, match.start()),
                signals=(*signals, "BODY"),
            )

    # 3b. The employment type says internship and NOTHING corroborates it.
    #
    # That is not enough to delete a record. Three of the dry run's confirmed
    # purges were exactly this shape and all three were real full-time jobs
    # whose employment_type had been misclassified. Reported for review instead.
    if about_interns and title_match:
        return InternshipVerdict(
            verdict=AMBIGUOUS,
            reason=(
                "The title mentions interns, but as the SUBJECT of the role — this "
                "reads as recruiting or programme management, not an internship."
            ),
            evidence=title_text[:120],
            signals=(*signals, "ABOUT_INTERNS"),
        )

    if declared_intern:
        return InternshipVerdict(
            verdict=AMBIGUOUS,
            reason=(
                f"employment_type is {employment_type}, but neither the title nor the "
                "description says this role is a student one. The stored employment "
                "type has been wrong before."
            ),
            evidence=title_text[:120],
            signals=(*signals, "EMPLOYMENT_TYPE_UNCORROBORATED"),
        )

    # 4. Positive full-time evidence.
    if _TITLE_FULL_TIME.search(title_text):
        return InternshipVerdict(
            verdict=FULL_TIME,
            reason="The title states a full-time or new-grad role.",
            evidence=title_text[:120],
            signals=(*signals, "TITLE_FULL_TIME"),
        )
    if (employment_type or "").upper() == "FULL_TIME":
        return InternshipVerdict(
            verdict=FULL_TIME,
            reason="The source states employment_type = FULL_TIME.",
            signals=(*signals, "EMPLOYMENT_TYPE_FULL_TIME"),
        )
    for pattern in _BODY_FULL_TIME:
        match = pattern.search(body)
        if match:
            return InternshipVerdict(
                verdict=FULL_TIME,
                reason=f"The description says {match.group(0)!r}.",
                evidence=_sentence_around(body, match.start()),
                signals=(*signals, "BODY_FULL_TIME"),
            )

    # 5. Nothing said either way.
    #
    # The old lane is reported but does NOT decide. It was assigned by a
    # classifier replaced twice since, and it holds genuinely full-time
    # postings — treating it as proof would delete real jobs.
    if search_lane == "FALL_2026_INTERNSHIP_COOP":
        return InternshipVerdict(
            verdict=AMBIGUOUS,
            reason=(
                "Was in the retired Fall lane, but nothing in the title, the "
                "employment type or the description confirms it is a student role."
            ),
            signals=(*signals, "OLD_LANE_ONLY"),
        )

    return InternshipVerdict(
        verdict=AMBIGUOUS if signals else FULL_TIME,
        reason=(
            "Nothing identifies this as a student role."
            if not signals
            else "Mixed signals; left for a human."
        ),
        signals=tuple(signals),
    )


def _sentence_around(text: str, index: int) -> str:
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    ends = [pos for pos in (text.find(".", index), text.find("\n", index)) if pos != -1]
    end = min(ends) + 1 if ends else len(text)
    return " ".join(text[start:end].split()).strip()[:300]
