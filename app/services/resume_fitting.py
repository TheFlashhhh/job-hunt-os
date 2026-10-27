"""Make the resume fit on one page by rendering it and looking.

One page is an OUTPUT invariant, not a budget. It was enforced by proxies —
`max_entries: 5`, `max_total_bullets: 15`, `max_bullets_per_entry_targeted: 4` —
each calibrated by rendering once, at one moment, against one Evidence Bank.
Nothing ever rendered and checked, so the invariant held only for as long as
every input stayed the shape it had on the day the numbers were measured.

It stopped holding. A real "Data Scientist, Finance" resume came out at **two
pages, the second containing only KENT Cam Technologies and its four bullets** —
while satisfying every proxy: 5 entries, 15 bullets, none over four. The counts
were right and the page was still full, because a bullet's height depends on how
much text it wraps to and the proxies count bullets, not lines.

So this renders the document, counts the pages, and if there is more than one,
removes the lowest-value content and renders again.

**What it removes, in order** (`_reduction_steps`):

1. the lowest-ranked bullet from a PROJECT carrying four
2. the lowest-ranked bullet from a PROJECT carrying three
3. the lowest-ranked CORE_ROLE skill, one at a time (D-70)
4. the lowest-ranked project entry, entirely
5. the lowest-ranked skill INFERRED from the posting, one at a time
6. a skill group holding no protected skill
7. only then, a bullet from paid work

A skill the posting names (DIRECT_JD) or the archetype pins is never removed:
it outranks a paid-work bullet. A general core skill does not outrank a
project's strongest lines, so it goes before the project does - never the
other way round (D-69 had it backwards).

**What it protects.** Both formal employment entries, education, the relevant
coursework line and the contact block. A four-bullet project loses a bullet
before OpenText or KENT loses its place on the page — the broken PDF had
OpenText on page 1 and KENT alone on page 2, which is the worst possible split
and is exactly what the ordering above prevents.

**What it does not do.** Shrink the type, crush the leading, or eat the margins.
The Spark-style layout is measured against a reference document and reducing
content is the honest lever; making the page unreadable to fit is not.

Nothing here invents or rewords. It only removes, so every surviving line is
still the Evidence Bank's own text and still passed the same claim gate.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader

from app.services.resume_targeting import (
    SKILL_CORE,
    SKILL_DIRECT,
    SKILL_INFERRED,
    SKILL_PINNED,
)

#: Never removed to fit: the posting asked for it, or the archetype pins it.
_PROTECTED_SKILL_TIERS = frozenset({SKILL_DIRECT, SKILL_PINNED})

#: A resume is one page. There is no mode in which two is acceptable today; if
#: one is ever added, it sets this explicitly rather than letting overflow decide.
MAX_PAGES = 1

#: Give up after this many removals rather than reducing to an empty document.
#: Reaching it is a bug worth surfacing, not a page to ship.
#: Skills now go one at a time, so this is sized for a handful of them on top
#: of the bullet and entry steps.
MAX_REDUCTIONS = 40


@dataclass
class FitResult:
    """What fitting did, and whether it worked."""

    pages: int
    reductions: list[str] = field(default_factory=list)
    fitted: bool = True

    @property
    def summary(self) -> str:
        if not self.reductions:
            return f"Fitted on the first render ({self.pages} page)."
        return (
            f"{len(self.reductions)} reduction(s) to reach {self.pages} page: "
            + "; ".join(self.reductions)
        )


class ResumeOverflowError(RuntimeError):
    """The document could not be reduced to one page.

    Raised rather than returning a two-page PDF. A resume that silently runs to
    two pages is a different document from the one the user asked for, and
    finding out from the employer is the worst possible time.
    """


def count_pages(pdf_bytes: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def fit_to_one_page(content: Any, render: Any) -> FitResult:
    """Render, count, reduce, repeat.

    `render` takes the content and returns PDF bytes. Injected so this is
    testable without touching the filesystem, and so the DOCX path can reuse the
    content that the PDF proved fits.
    """
    reductions: list[str] = []

    for _ in range(MAX_REDUCTIONS + 1):
        pages = count_pages(render(content))
        if pages <= MAX_PAGES:
            return FitResult(pages=pages, reductions=reductions, fitted=True)

        removed = _reduce_once(content)
        if removed is None:
            # Nothing left that may be removed. Better to say so than to ship
            # a two-page document that claims to be one.
            raise ResumeOverflowError(
                f"The resume renders to {pages} pages and no further content can be "
                "removed without cutting protected sections (both experiences, "
                "education, coursework). "
                + (f"Already removed: {'; '.join(reductions)}." if reductions else "")
            )
        reductions.append(removed)

    raise ResumeOverflowError(
        f"Still {MAX_PAGES + 1}+ pages after {MAX_REDUCTIONS} reductions: "
        + "; ".join(reductions)
    )


# ---------------------------------------------------------------------------
# Reduction
# ---------------------------------------------------------------------------

#: Entry KINDS that are never dropped wholesale, and are asked for bullets last.
#: A formal job disappearing to make room for a class project is a worse resume,
#: not a shorter one.
#:
#: This reads `kind`, not `heading`. `heading` is the ORGANISATION — "OpenText",
#: "KENT Cam Technologies" — so matching "EXPERIENCE" against it never fired,
#: and the first version of this module took a bullet from KENT while a
#: four-bullet project sat untouched.
_PROTECTED_KINDS = ("EXPERIENCE", "EDUCATION")


def _reduce_once(content: Any) -> str | None:
    """Remove exactly one thing, the cheapest first. Returns what went."""
    for step in _reduction_steps():
        removed = step(content)
        if removed:
            return removed
    return None


def _reduction_steps() -> list[Any]:
    """Cheapest content first, and PROJECT content long before EXPERIENCE.

    Every project bullet, the skills block and even a whole project entry go
    before a formal job loses a single line. Two internships are the only paid
    work on this resume; a class project is not competing with them for space.
    """
    return [
        # 1. A project carrying four bullets loses its fourth. Carrying four on
        #    a one-page resume needs unusual relevance, and the fourth is by
        #    construction the least relevant of them.
        lambda content: _drop_weakest_bullet(content, minimum=4, protected=False),
        # 2. Then a project carrying three.
        lambda content: _drop_weakest_bullet(content, minimum=3, protected=False),
        # 3. A general core skill the posting did not ask for. It is less
        #    evidence than the strongest lines of a project (D-70).
        lambda content: _drop_weakest_skill(content, tier=SKILL_CORE),
        # 4. A whole project.
        _drop_weakest_project,
        # 5. A skill the posting implies but does not name.
        lambda content: _drop_weakest_skill(content, tier=SKILL_INFERRED),
        # 6. Content built without tiers: a group with nothing protected in it.
        _drop_weakest_skill_group,
        # 7. Only now does paid work give anything up. A DIRECT_JD or pinned
        #    skill is never removed at all.
        lambda content: _drop_weakest_bullet(content, minimum=4, protected=True),
        lambda content: _drop_weakest_bullet(content, minimum=3, protected=True),
    ]


def _sections(content: Any) -> list[Any]:
    return list(getattr(content, "sections", []) or [])


def _is_protected(section: Any) -> bool:
    kind = str(getattr(section, "kind", "") or "").upper()
    return any(name in kind for name in _PROTECTED_KINDS)


def _drop_weakest_bullet(content: Any, *, minimum: int, protected: bool) -> str | None:
    """Drop the last bullet of an entry carrying `minimum` or more.

    Bullets arrive ranked, so the last one in an entry is the weakest the
    targeting found for it. `protected` selects which population to take from —
    the caller decides the order, and experience comes last.

    Scanned in reverse so the lowest-ranked ENTRY gives up a line first.
    """
    candidates = [s for s in _sections(content) if _is_protected(s) is protected]
    for section in reversed(candidates):
        bullets = list(getattr(section, "bullets", []) or [])
        if len(bullets) >= minimum:
            section.bullets = bullets[:-1]
            return (
                f"a bullet from {_label(section)} "
                f"({len(bullets)} → {len(bullets) - 1})"
            )
    return None


def _drop_weakest_skill(content: Any, *, tier: str) -> str | None:
    """Remove the lowest-ranked rendered skill of `tier`.

    Groups arrive with de-emphasised ones last and each group ranked, so the
    weakest candidate is the LAST skill of that tier in the LAST group holding
    one. It is removed from both the PDF's groups and the DOCX's lines, which
    must never disagree about what the candidate lists.
    """
    tiers = dict(getattr(content, "skill_tiers", {}) or {})
    groups = list(getattr(content, "skill_groups", []) or [])
    for label, joined in reversed(groups):
        names = [n for n in str(joined).split(", ") if n]
        for name in reversed(names):
            if tiers.get(name) == tier:
                _remove_skill(content, name)
                return f"the {name} skill ({label})"
    return None


def _remove_skill(content: Any, name: str) -> None:
    groups = []
    for label, joined in getattr(content, "skill_groups", []) or []:
        kept = [n for n in str(joined).split(", ") if n and n != name]
        if kept:
            groups.append((label, ", ".join(kept)))
    content.skill_groups = groups
    lines = []
    for line in getattr(content, "skill_lines", []) or []:
        label, _, rest = str(line).partition(": ")
        kept = [n for n in rest.split(", ") if n and n != name]
        if kept:
            lines.append(f"{label}: {', '.join(kept)}")
    content.skill_lines = lines


def _drop_weakest_skill_group(content: Any) -> str | None:
    """The last group, when nothing in it is protected.

    Reached for content built without tiers, or a page whose remaining
    unprotected skills are all in groups that also hold protected ones - in
    which case there is no group to drop and paid work is next.
    """
    groups = list(getattr(content, "skill_groups", []) or [])
    if len(groups) <= 1:
        return None
    label, joined = groups[-1] if isinstance(groups[-1], (list, tuple)) else (str(groups[-1]), "")
    tiers = dict(getattr(content, "skill_tiers", {}) or {})
    if any(tiers.get(n) in _PROTECTED_SKILL_TIERS for n in str(joined).split(", ")):
        return None
    content.skill_groups = groups[:-1]
    lines = list(getattr(content, "skill_lines", []) or [])
    content.skill_lines = [ln for ln in lines if not str(ln).startswith(f"{label}:")]
    return f"the {label} skill group"


def _drop_weakest_project(content: Any) -> str | None:
    """Remove the last unprotected entry outright."""
    sections = _sections(content)
    for index in range(len(sections) - 1, -1, -1):
        section = sections[index]
        if _is_protected(section):
            continue
        # Never take the last remaining project: a PROJECTS heading with
        # nothing under it is worse than one short project.
        siblings = [s for s in sections if not _is_protected(s)]
        if len(siblings) <= 1:
            return None
        content.sections = sections[:index] + sections[index + 1 :]
        return f"the {_label(section)} entry"
    return None


def _label(section: Any) -> str:
    heading = str(getattr(section, "heading", "") or "").strip()
    sub = str(getattr(section, "title", "") or "").strip()
    return heading or sub or "an entry"
