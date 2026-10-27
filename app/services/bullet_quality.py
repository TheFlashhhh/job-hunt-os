"""Does this bullet say what was achieved, or only what was touched?

A resume bullet earns its line by answering three things:

* **X** - what the candidate did
* **Y** - what came of it: a verified figure, a verified scale, a deliverable,
  a finding, a validation result, a stakeholder outcome
* **Z** - how it was done

The order does not matter. "Validated 500+ SQL-backed workflows, improving
release confidence by reconciling API outputs with persisted database records"
is X-Y-Z; "Built a leakage-safe possession classifier using the first three
actions of each possession, reaching 0.341 PR-AUC versus a 0.107 naive
baseline" is X-Z-Y. Both work. What does not work is "Worked with Tableau."

**This scores. It never rejects.** English is not parseable by a hundred lines
of regex, and a truthful bullet that a heuristic misreads must still reach the
page. So the output is a preference signal for
:mod:`app.services.resume_targeting` and a QA lens for the bullet audit - the
hard gates stay exactly where they are, in ``SafeClaimValidator`` and the
Evidence Bank's ownership rules.

**And it is strictly subordinate to truthfulness.** Nothing here rewrites a
bullet, supplies a missing outcome or promotes a figure. A claim with no
defensible Y scores lower and stays as it is; inventing one to satisfy the
pattern is the single worst thing this module could cause, which is why it has
no authorship path at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.service import EvidenceBankService

#: Verbs that name an accomplishment. Deliberately broad - a bullet opening
#: with any of these has cleared the lowest bar, not the highest.
_ACTION_VERBS = frozenset(
    {
        "added", "analysed", "analyzed", "applied", "audited", "authored",
        "automated", "benchmarked", "built", "calibrated", "cleaned",
        "compared", "compiled", "computed", "constructed", "contributed",
        "converted", "created", "curated", "cut", "delivered", "deployed",
        "derived", "designed", "detected", "developed", "diagnosed",
        "documented", "engineered", "established", "estimated", "evaluated",
        "expanded", "extended", "identified", "implemented", "improved",
        "increased", "integrated", "introduced", "investigated", "iterated",
        "led", "linked", "maintained", "mapped", "measured", "merged",
        "migrated", "modelled", "modeled", "monitored", "optimised",
        "optimized", "orchestrated", "organised", "organized", "packaged",
        "parsed", "precomputed", "prepared", "presented", "processed",
        "produced", "prototyped", "quantified", "reconciled", "reduced",
        "refactored", "replaced", "reported", "resolved", "restructured",
        "routed", "scored", "served", "shipped", "simulated", "standardised",
        "standardized", "streamlined", "supported", "tested", "tokenised",
        "tokenized", "tracked", "trained", "tuned", "validated", "verified",
        "visualised", "visualized", "wrote",
    }
)

#: Openers that describe proximity to work rather than the work. A bullet
#: leading with one of these is flagged VAGUE_ACTION however true it is.
_VAGUE_OPENERS = (
    "worked on", "worked with", "helped", "assisted", "participated",
    "involved in", "responsible for", "was responsible", "supported the team",
    "contributed to", "familiar with", "exposure to", "gained experience",
)

#: Wording that marks a real result when no number is available. A deliverable,
#: a finding, a validation result or a stakeholder outcome is a legitimate Y -
#: Part 8 of the standard exists precisely so a numberless bullet is not forced
#: to borrow a statistic.
_OUTCOME_TERMS = (
    "client-approved", "client approved", "approved by", "accepted by",
    "delivered", "deliverable", "presented", "recommendation", "adopted",
    "in production", "deployed", "shipped", "released",
    "backtest", "held-out", "held out", "cross-validation", "cross-validated",
    "grouped cross", "baseline", "regression coverage", "regression test",
    "reproducib", "validated", "verified", "leakage-safe",
    "decision-support", "decision support", "finding", "insight",
    "enabling", "enabled", "allowing", "so that", "to support", "supporting",
    "which let", "used in", "used by", "informing", "informed",
    "improving", "reducing", "cutting", "raising", "surfacing", "supplying",
    "final figures", "final report", "end-to-end",
)

#: Connectives that introduce method. Their presence is one route to Z; naming
#: a Bank technology is the other, and the stronger one.
_METHOD_MARKERS = (
    " using ", " by ", " with ", " via ", " through ", " built on ",
    " based on ", " from ", " across ",
)

_NUMBER_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b|\b\d+\s*%")

#: A figure counts as Y when it QUANTIFIES SOMETHING: "7,349 lap records",
#: "500+ users", "40,460 image captions", "0.341 PR-AUC", "12%". A bare number
#: attached to nothing does not. Enumerating scale nouns was the obvious
#: implementation and the wrong one — every audit run turned up another noun
#: the list had never heard of ("captions", "image IDs", "checkpoints"), which
#: is a list that can only ever be behind.
_QUANTIFIED_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|\+|[-–/]?\s*[a-z])", re.I)

#: Units that measure elapsed time rather than scope. "in 2 weeks" says how
#: long it took, not how much was done.
_DURATION_UNITS = (
    "week", "weeks", "month", "months", "year", "years", "day", "days",
    "hour", "hours", "minute", "minutes", "second", "seconds", "sprint",
    "sprints",
)

#: Four or more technology names with nothing achieved is a tool list wearing a
#: bullet's clothes.
_TOOL_DUMP_THRESHOLD = 4


@dataclass
class BulletQuality:
    """How well one bullet communicates X, Y and Z. Advisory only."""

    verdict: str
    score: float
    components: dict[str, bool] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)

    @property
    def is_weak(self) -> bool:
        """Below ACCEPTABLE_XYZ.

        ``POSSIBLE_XYZ`` counts as weak: it means X and Z are present and Y is
        not, which is "Built a Tableau map." — a true sentence that tells a
        reader nothing about why it mattered.
        """
        return self.verdict in {"WEAK_ACTIVITY_ONLY", "POSSIBLE_XYZ"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "components": self.components,
            "flags": self.flags,
            "notes": self.notes,
            "technologies": self.technologies,
        }


class BulletQualityScorer:
    """Reads the Bank for its technology vocabulary and its metric figures.

    The technology list is not hardcoded: it is ``skills.yaml``. That matters
    twice over - a skill the Bank does not carry cannot be counted as Z, and a
    number that traces to no metric is never counted as a *verified* Y.
    """

    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        if bank is None:
            from app.evidence.service import get_evidence_bank

            bank = get_evidence_bank()
        self.bank = bank
        self._tech_terms = self._build_tech_terms()
        self._metric_digits = self._build_metric_digits()

    def _build_tech_terms(self) -> list[str]:
        terms: set[str] = set()
        for skill in self.bank.skills.values():
            for part in re.split(r"[/,]", skill.name):
                token = part.strip().lower()
                if len(token) >= 3 and token not in {"and", "the"}:
                    terms.add(token)
        return sorted(terms, key=len, reverse=True)

    def _build_metric_digits(self) -> set[str]:
        """Every digit string the Bank actually records, for metric_verified."""
        digits: set[str] = set()
        for metric in self.bank.metrics.values():
            value = metric.get("value")
            sources = [value, *(metric.get("allowed_formulations") or [])]
            if isinstance(value, dict):
                sources.extend(value.values())
            for raw in sources:
                for found in _NUMBER_RE.findall(str(raw)):
                    cleaned = found.replace(",", "").replace("%", "").strip()
                    if cleaned:
                        digits.add(cleaned)
        return digits

    # -- scoring ---------------------------------------------------------
    def score(self, text: str, *, claim: Any | None = None) -> BulletQuality:
        lowered = f" {text.lower().strip()} "
        flags: list[str] = []
        notes: list[str] = []

        technologies = self._technologies(lowered)
        action = self._action_present(lowered)
        vague = self._vague_opener(lowered)
        method = bool(technologies) or any(m in lowered for m in _METHOD_MARKERS)
        numbers = self._numbers(text)
        metric_verified = bool(numbers & self._metric_digits)
        outcome = self._outcome_present(lowered, numbers)

        components = {
            "action_present": action,
            "method_present": method,
            "outcome_or_scope_present": outcome,
            "metric_verified": metric_verified,
        }
        if claim is not None:
            components["ownership_valid"] = not bool(getattr(claim, "gated", False)) or bool(
                getattr(claim, "project_level_alternative", None)
            )
            components["evidence_valid"] = bool(getattr(claim, "evidence_ids", ()) or ())

        if vague:
            flags.append("VAGUE_ACTION")
            notes.append(f"Opens with '{vague}' - describes proximity to work, not the work.")
        elif not action:
            flags.append("VAGUE_ACTION")
            notes.append("No accomplishment verb found.")
        if not outcome:
            flags.append("NO_OUTCOME_OR_SCOPE")
            notes.append("No result, verified scale or deliverable - this is activity, not impact.")
        if len(technologies) >= _TOOL_DUMP_THRESHOLD and not outcome:
            flags.append("TOOL_DUMP")
            notes.append(
                f"{len(technologies)} technologies named and nothing achieved. "
                "Tools belong inside evidence of accomplishment."
            )
        elif action and not outcome:
            flags.append("ACTIVITY_ONLY")

        score = self._numeric_score(components, vague=bool(vague), flags=flags)
        verdict = self._verdict(components, vague=bool(vague), flags=flags)

        return BulletQuality(
            verdict=verdict,
            score=round(score, 3),
            components=components,
            flags=flags,
            notes=notes,
            technologies=technologies,
        )

    # -- signals ---------------------------------------------------------
    def _technologies(self, lowered: str) -> list[str]:
        found: list[str] = []
        for term in self._tech_terms:
            matched = re.search(rf"(?<![\w+#.-]){re.escape(term)}(?![\w+#-])", lowered)
            # Longest first, so "sql server" claims the phrase before "sql"
            # can count it a second time.
            if matched and not any(term in seen for seen in found):
                found.append(term)
        return found

    @staticmethod
    def _action_present(lowered: str) -> bool:
        return any(
            re.search(rf"(?<![\w-]){verb}(?![\w-])", lowered) for verb in _ACTION_VERBS
        )

    @staticmethod
    def _vague_opener(lowered: str) -> str | None:
        head = lowered.strip()[:40]
        for phrase in _VAGUE_OPENERS:
            if phrase in head:
                return phrase
        return None

    @staticmethod
    def _numbers(text: str) -> set[str]:
        return {
            found.replace(",", "").replace("%", "").strip()
            for found in _NUMBER_RE.findall(text)
        }

    @staticmethod
    def _outcome_present(lowered: str, numbers: set[str]) -> bool:
        if any(term in lowered for term in _OUTCOME_TERMS):
            return True
        for match in _QUANTIFIED_RE.finditer(lowered):
            tail = lowered[match.end() - 1 : match.end() + 12].strip()
            word = re.match(r"[a-z]+", tail)
            if word and word.group() in _DURATION_UNITS:
                continue
            return True
        return False

    @staticmethod
    def _numeric_score(components: dict[str, bool], *, vague: bool, flags: list[str]) -> float:
        score = 0.0
        if components["action_present"]:
            score += 0.30
        if components["method_present"]:
            score += 0.25
        if components["outcome_or_scope_present"]:
            score += 0.35
        if components["metric_verified"]:
            score += 0.10
        if vague:
            score -= 0.20
        if "TOOL_DUMP" in flags:
            score -= 0.15
        return max(0.0, min(1.0, score))

    @staticmethod
    def _verdict(components: dict[str, bool], *, vague: bool, flags: list[str]) -> str:
        action = components["action_present"] and not vague
        method = components["method_present"]
        outcome = components["outcome_or_scope_present"]

        if "TOOL_DUMP" in flags:
            return "WEAK_ACTIVITY_ONLY"
        if action and method and outcome:
            return "GOOD_XYZ" if components["metric_verified"] else "ACCEPTABLE_XYZ"
        if action and outcome:
            return "ACCEPTABLE_XYZ"
        if action and method:
            return "POSSIBLE_XYZ"
        return "WEAK_ACTIVITY_ONLY"


_SCORER: BulletQualityScorer | None = None


def get_bullet_quality_scorer(bank: EvidenceBankService | None = None) -> BulletQualityScorer:
    """Process-wide scorer. Rebuilt when an explicit bank is supplied."""
    global _SCORER
    if bank is not None:
        return BulletQualityScorer(bank)
    if _SCORER is None:
        _SCORER = BulletQualityScorer()
    return _SCORER


def reset_bullet_quality_cache() -> None:
    global _SCORER
    _SCORER = None
