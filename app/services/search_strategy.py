"""Which lane is active, how senior a posting is, and when it starts.

Three questions the active search asks of every posting, kept together because
they share one config file (`data/config/search_strategy.yaml`) and because
answering them separately is how they drifted apart before.

**Lane activity.** As of 2026-08-24 there is one active lane, `FULL_TIME_2027`.
`FALL_2026_INTERNSHIP_COOP` is ARCHIVED: its records stay, stay accurate and
stay reachable, but it produces no active targets, no KPIs and no next actions.
Read from config rather than hard-coded, so this is a config change and not a
code change if it moves again.

**Experience band.** Four bands, and the point of having four is that REACH is
visible rather than deleted. A posting asking "2+ years" is a normal new-grad
posting in this market; hard-rejecting every one would remove most of it. What
matters is that the band is stated honestly on the card, not that the job is
hidden.

**Start-date compatibility.** The candidate finishes coursework in December 2026 and the
degree is conferred around January 2027. A posting that explicitly requires
starting before that is a timing mismatch worth flagging. **A posting with no
start date is not a mismatch** — most postings say nothing, and treating silence
as a conflict would reject the market.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import load_yaml_config

STRATEGY_CONFIG = "search_strategy.yaml"

#: Bumped when banding or lane rules change.
STRATEGY_VERSION = "2.0"

ENTRY_COMPATIBLE = "ENTRY_COMPATIBLE"
EARLY_CAREER = "EARLY_CAREER"
REACH = "REACH"
OVERLEVELLED = "OVERLEVELLED"
EXPERIENCE_UNKNOWN = "EXPERIENCE_UNSTATED"

START_COMPATIBLE = "START_COMPATIBLE"
START_UNSTATED = "START_UNSTATED"
START_MISMATCH = "START_TIMING_MISMATCH"

LANE_ACTIVE = "ACTIVE"
LANE_ARCHIVED = "ARCHIVED"


def load_strategy() -> dict[str, Any]:
    return load_yaml_config(STRATEGY_CONFIG)


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------


def active_lanes() -> list[str]:
    """Lanes that produce active targets. One, as of the 2026-08-24 reset."""
    strategy = load_strategy()
    declared = strategy.get("active_lanes")
    if isinstance(declared, list) and declared:
        return [str(lane) for lane in declared]
    return [
        name
        for name, spec in (strategy.get("lanes") or {}).items()
        if str((spec or {}).get("status", "")).upper() == LANE_ACTIVE
    ]


def lane_status(lane: str | None) -> str:
    """ACTIVE or ARCHIVED. An unknown lane is treated as archived, not active.

    `BOTH_OR_AMBIGUOUS` deliberately resolves to ACTIVE: it means the classifier
    could not tell, and the corpus is full of it (9,647 rows). Hiding those
    would hide most of the search behind a classification failure.
    """
    if not lane:
        return LANE_ACTIVE
    if lane in ("BOTH_OR_AMBIGUOUS", "UNKNOWN"):
        return LANE_ACTIVE
    strategy = load_strategy()
    spec = (strategy.get("lanes") or {}).get(lane)
    if spec is None:
        return LANE_ARCHIVED
    return str(spec.get("status", LANE_ARCHIVED)).upper()


def lane_is_active(lane: str | None) -> bool:
    return lane_status(lane) == LANE_ACTIVE


def archived_lanes() -> list[str]:
    return [
        name
        for name, spec in (load_strategy().get("lanes") or {}).items()
        if str((spec or {}).get("status", "")).upper() == LANE_ARCHIVED
    ]


def excluded_employment_types() -> set[str]:
    """Internships and co-ops, as of the reset. Existing records keep theirs."""
    employment = load_strategy().get("employment") or {}
    return {str(t) for t in (employment.get("excluded_types") or [])}


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

#: "2+ years", "3-5 years", "minimum of 4 years". Reused rather than
#: re-derived — `classifiers.extract_years_experience` already handles the
#: shapes seen in this corpus, including the "experience must be nearby" guard
#: that stops "5 years" in a benefits paragraph counting.
_NEW_GRAD_PHRASES = re.compile(
    r"\b(new grad(?:uate)?|recent grad(?:uate)?|university grad(?:uate)?|"
    r"entry[- ]level|early career|campus hire|graduating (?:in|by)|"
    r"class of 20\d\d|rotational program|graduate program|"
    r"0[-–]1 years?|0[-–]2 years?|no prior experience|"
    r"experience not required)\b",
    re.I,
)

#: Seniority in the title, at two strengths.
#:
#: Written as explicit per-alternative boundaries rather than one wrapping
#: `\b(...)\b`. A trailing `\b` after `sr\.` can NEVER match: a period followed
#: by a space is two non-word characters with no boundary between them. That one
#: bug banded every "Sr. Business Intelligence Engineer" in the corpus as
#: ENTRY_COMPATIBLE, and the audit is how it was caught.
_HARD_SENIOR_TITLE = re.compile(
    r"(?:\bsenior\b|\bsr\b\.?|\bstaff\b|\bprincipal\b|\bdistinguished\b|\bfellow\b|"
    r"\bdirector\b|\bvice president\b|\bvp\b|\bhead of\b|\bchief\b|\barchitect\b|"
    r"\biii\b|\biv\b)",
    re.I,
)

#: Leadership words that a genuine new-grad posting can still carry.
_SOFT_LEAD_TITLE = re.compile(r"(?:\bmanager\b|\blead\b|\bsupervisor\b)", re.I)

#: Wording that turns a years figure from a wish into a requirement.
_REQUIRED_NEARBY = re.compile(r"\b(required|must have|minimum|at least)\b", re.I)


@dataclass(frozen=True)
class ExperienceBand:
    band: str
    years_min: int | None = None
    years_max: int | None = None
    required: bool = False
    detail: str = ""

    @property
    def active(self) -> bool:
        """OVERLEVELLED is the only band the active search does not pursue."""
        return self.band != OVERLEVELLED

    @property
    def label(self) -> str:
        return {
            ENTRY_COMPATIBLE: "Entry compatible",
            EARLY_CAREER: "Early career",
            REACH: "Reach",
            OVERLEVELLED: "Overlevelled",
            EXPERIENCE_UNKNOWN: "Experience unstated",
        }.get(self.band, self.band)

    def as_dict(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "label": self.label,
            "years_min": self.years_min,
            "years_max": self.years_max,
            "required": self.required,
            "active": self.active,
            "detail": self.detail,
        }


def classify_experience_band(title: str, description: str) -> ExperienceBand:
    """Band a posting by what it actually asks for.

    Parsed from the description rather than taken from a provider's seniority
    label, which in this corpus is absent for 90% of rows.
    """
    from app.services.classifiers import extract_years_experience

    title_text = title or ""
    body = (description or "")[:12000]
    blob = f"{title_text}\n{body}"

    lo, hi = extract_years_experience(body)
    new_grad = bool(_NEW_GRAD_PHRASES.search(blob))

    # A senior title is decisive: a "Staff Data Scientist" posting that also
    # says "0-2 years" somewhere is not an entry role.
    if _HARD_SENIOR_TITLE.search(title_text):
        return ExperienceBand(
            band=OVERLEVELLED,
            detail=f"Title states a senior level: {title_text[:60]!r}.",
        )

    # "Manager" and "Lead" usually mean the same thing, but not always:
    # "Associate Product Manager, New Grad" is a real new-grad posting. Explicit
    # new-grad wording overrides a soft leadership word; it never overrides
    # "Senior" or "Staff".
    if _SOFT_LEAD_TITLE.search(title_text) and not new_grad:
        return ExperienceBand(
            band=OVERLEVELLED,
            detail=f"Title is a leadership role: {title_text[:60]!r}.",
        )

    if lo is None:
        if new_grad:
            return ExperienceBand(
                band=ENTRY_COMPATIBLE,
                detail="Posting uses new-grad or entry-level wording.",
            )
        return ExperienceBand(
            band=EXPERIENCE_UNKNOWN,
            detail="The posting does not state a years requirement.",
        )

    # Is the figure a requirement or a preference? "2+ years preferred" is a
    # softer thing than "2+ years required", and the band should say so.
    window = ""
    match = re.search(rf"\b{lo}\s*\+?\s*(?:to|-|–)?\s*\d*\s*years?", body, re.I)
    if match:
        window = body[max(0, match.start() - 80) : match.end() + 80]
    required = bool(_REQUIRED_NEARBY.search(window))

    strategy = load_strategy()
    hard_ceiling = int((strategy.get("experience") or {}).get("hard_ceiling_years", 6))

    # "Required" is the axis that matters, not the number alone. "2+ years
    # preferred" is a normal new-grad posting; "2+ years required" is a genuine
    # stretch and is labelled one — while staying visible, because REACH is an
    # active band.
    if lo <= 1 or new_grad:
        band = ENTRY_COMPATIBLE
    elif lo <= 3:
        band = REACH if required else EARLY_CAREER
    elif lo < hard_ceiling:
        band = OVERLEVELLED if (required and lo >= 5) else REACH
    else:
        band = OVERLEVELLED

    detail = f"Posting asks for {lo}{f'-{hi}' if hi else '+'} years"
    detail += " (required)." if required else " (preferred or unqualified)."
    return ExperienceBand(band=band, years_min=lo, years_max=hi, required=required, detail=detail)


# ---------------------------------------------------------------------------
# Start date
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_START_PATTERNS = (
    re.compile(r"\bstart(?:ing|s)?\s+(?:date\s+)?(?:in\s+|on\s+|by\s+)?"
               r"(january|february|march|april|may|june|july|august|september|october|"
               r"november|december)\s+(20\d\d)\b", re.I),
    re.compile(r"\b(spring|summer|fall|autumn|winter)\s+(20\d\d)\s+start\b", re.I),
    re.compile(r"\b(20\d\d)\s+(?:university\s+)?grad(?:uate)?s?\b", re.I),
    re.compile(r"\bgraduat\w+\s+(?:between\s+)?(?:in\s+)?"
               r"(december|january|may|june)\s+(20\d\d)\b", re.I),
)

_SEASON_MONTHS = {"spring": 3, "summer": 6, "fall": 9, "autumn": 9, "winter": 12}

_IMMEDIATE = re.compile(r"\b(immediate(?:ly)? start|start immediately|asap start)\b", re.I)


@dataclass(frozen=True)
class StartFit:
    status: str
    stated: str = ""
    detail: str = ""

    @property
    def compatible(self) -> bool:
        """Unstated counts as compatible. Silence is not a conflict."""
        return self.status != START_MISMATCH

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stated": self.stated,
            "detail": self.detail,
            "compatible": self.compatible,
        }


def classify_start_fit(description: str, *, strategy: dict[str, Any] | None = None) -> StartFit:
    """Does the posting demand a start before the candidate is available?

    Only an EXPLICIT earlier start is a mismatch. Everything else — no date, a
    later date, a vague "flexible" — is compatible.
    """
    strategy = strategy or load_strategy()
    availability = strategy.get("availability") or {}
    threshold = str(availability.get("flag_start_before") or "2026-12")
    t_year, t_month = (int(part) for part in threshold.split("-")[:2])

    body = (description or "")[:12000]

    if _IMMEDIATE.search(body):
        return StartFit(
            status=START_MISMATCH,
            stated="immediate start",
            detail=(
                "The posting asks for an immediate start; the candidate is available from "
                f"{threshold}."
            ),
        )

    for pattern in _START_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 1:  # "2027 University Graduates"
            year, month = int(groups[0]), 6
            stated = match.group(0)
        else:
            token, year_text = groups[0].lower(), groups[1]
            month = _MONTHS.get(token) or _SEASON_MONTHS.get(token) or 6
            year = int(year_text)
            stated = match.group(0)

        if (year, month) < (t_year, t_month):
            return StartFit(
                status=START_MISMATCH,
                stated=stated,
                detail=(
                    f"The posting states {stated!r}, which is before the candidate finishes "
                    f"coursework ({threshold})."
                ),
            )
        return StartFit(
            status=START_COMPATIBLE,
            stated=stated,
            detail=f"The posting states {stated!r}, which fits.",
        )

    return StartFit(
        status=START_UNSTATED,
        detail="No start date stated — treated as compatible, because most postings give none.",
    )


# ---------------------------------------------------------------------------
# Location priority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocationPriority:
    tier: int
    name: str
    #: Priority orders results; it never removes them. Only non-US does that,
    #: and that decision lives in the prescreen.
    reasons: tuple[str, ...] = field(default_factory=tuple)


def location_tier(location_raw: str | None, *, strategy: dict[str, Any] | None = None) -> LocationPriority:
    """Rank a US location. A strong role in Denver stays visible, ranked lower."""
    strategy = strategy or load_strategy()
    config = strategy.get("location") or {}
    text = (location_raw or "").lower()
    if not text:
        return LocationPriority(tier=int(config.get("other_us_tier", 5)), name="Unstated")

    for entry in config.get("priority") or []:
        for term in entry.get("terms") or []:
            if str(term).lower() in text:
                return LocationPriority(
                    tier=int(entry.get("tier", 5)),
                    name=str(entry.get("name", "")),
                    reasons=(f"Matches {entry.get('name')}.",),
                )
    return LocationPriority(tier=int(config.get("other_us_tier", 5)), name="Other US")
