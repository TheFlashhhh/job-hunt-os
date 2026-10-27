"""SafeClaimValidator — the core product safety feature.

Answers *"Is this sentence supported?"* against the Evidence Bank. A proposed
resume bullet, outreach line or application answer that comes back
``UNSUPPORTED`` must not be generated.

Checks, in order:

1. **Prohibited claims** — 39 ``DNC-*`` rules. Any hit is an immediate block.
2. **Forbidden metrics** — categories that exist nowhere and may never appear.
3. **Ownership gating** — first-person verbs over gated evidence are downgraded
   to the project-level alternative rather than emitted.
4. **Skill support** — technology mentions must map to a skill with non-zero
   evidence weight.
5. **Safe-claim match** — supported claims should trace to ``SC-*`` records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.service import (
    FIRST_PERSON_OWNERSHIP_VERBS,
    ZERO_WEIGHT_SKILL_LEVELS,
    EvidenceBankService,
    get_evidence_bank,
)

#: Numeric patterns that suggest a fabricated performance metric.
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%|\b\d[\d,]*(?:\.\d+)?\b")


def _inflections(token: str) -> set[str]:
    """Singular/plural variants of a prohibited subject.

    ``prohibited_claims`` stores subjects as written ("LLMs", "multi-agent
    systems"). Matching only the stored form misses "LLM agents", so both the
    singular and plural are indexed.
    """
    variants = {token}
    if token.endswith("ies") and len(token) > 4:
        variants.add(token[:-3] + "y")
    elif token.endswith("es") and len(token) > 3:
        variants.add(token[:-2])
        variants.add(token[:-1])
    elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        variants.add(token[:-1])
    else:
        variants.add(token + "s")
    return {v for v in variants if len(v) >= 3}


@dataclass
class ClaimCheck:
    """Result of validating one proposed claim."""

    verdict: str  # SUPPORTED | PARTIAL | UNSUPPORTED
    reasons: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    matched_claim_ids: list[str] = field(default_factory=list)
    matched_evidence_ids: list[str] = field(default_factory=list)
    ownership_downgrade: str | None = None
    suggested_text: str | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict == "UNSUPPORTED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": self.reasons,
            "violations": self.violations,
            "matched_claim_ids": self.matched_claim_ids,
            "matched_evidence_ids": self.matched_evidence_ids,
            "ownership_downgrade": self.ownership_downgrade,
            "suggested_text": self.suggested_text,
        }


class SafeClaimValidator:
    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        self.bank = bank or get_evidence_bank()
        self._prohibited_terms = self._build_prohibited_terms()
        self._prohibited_phrases = self._build_prohibited_phrases()
        self._forbidden_metric_terms = self._build_forbidden_metric_terms()
        #: Metrics exempted BY NAME, which must also trace to a safe claim.
        self._claim_bound_exemptions: set[str] = set()
        self._category_exemptions = self._build_category_exemptions()

    # -- rule construction ----------------------------------------------
    def _build_prohibited_terms(self) -> list[tuple[str, str, str]]:
        """(lowercased subject phrase, claim_id, rule) for subject-shaped DNC records.

        ``prohibited_claims`` has two shapes: ``subject`` + ``rule`` (a category
        of claim) and ``text`` + ``correct_boundary`` (a verbatim prohibited
        sentence). This handles the first; :meth:`_build_prohibited_phrases`
        handles the second.
        """
        out: list[tuple[str, str, str]] = []
        for claim in self.bank.prohibited_claims.values():
            subject = (claim.subject or "").strip()
            if not subject:
                continue
            for part in re.split(r"[,/]| and ", subject):
                token = part.strip().strip('"').strip("'").lower()
                # Skip long prose subjects — they are matched as phrases instead.
                if 3 <= len(token) <= 40 and token.count(" ") <= 3:
                    for variant in _inflections(token):
                        out.append((variant, claim.claim_id, claim.rule))
        return out

    def _build_prohibited_phrases(self) -> list[tuple[set[str], str, str]]:
        """(token set, claim_id, boundary) for verbatim prohibited phrasings."""
        out: list[tuple[set[str], str, str]] = []
        for claim in self.bank.prohibited_claims.values():
            raw = claim.raw
            phrase = str(raw.get("text") or "").strip()
            if not phrase:
                # Long prose subjects also behave as phrases.
                subject = (claim.subject or "").strip()
                if len(subject) > 40 or subject.count(" ") > 3:
                    phrase = subject
            if not phrase:
                continue
            tokens = self._tokens(phrase)
            if len(tokens) >= 3:
                boundary = str(raw.get("correct_boundary") or claim.rule or "")
                out.append((tokens, claim.claim_id, boundary))
        return out

    def _build_forbidden_metric_terms(self) -> list[tuple[str, str]]:
        """(lowercased metric term, category) from ``metrics.yaml``."""
        raw = self.bank._raw["metrics"].get("forbidden_metrics", [])
        out: list[tuple[str, str]] = []
        for entry in raw:
            if isinstance(entry, dict):
                category = str(entry.get("category", "FORBIDDEN"))
                for item in entry.get("items", []) or entry.get("examples", []) or []:
                    term = str(item).strip().lower()
                    if term:
                        out.append((term, category))
            elif isinstance(entry, str):
                out.append((entry.strip().lower(), "FORBIDDEN"))
        return out

    def _build_category_exemptions(self) -> dict[str, set[str]]:
        """Category → the metric IDs that category's own narrowing permits.

        ``forbidden_metrics`` is not a flat ban any more. MODEL_PERFORMANCE
        became ``DEFAULT_PROHIBITED_UNLESS_ALL_CONDITIONS_MET`` in 2026-08, with
        ``permitted_for_projects`` naming the projects whose evaluation is real
        and ``permitted_only_when_all_true`` naming what each figure must carry.
        Nothing read any of it, so the word "accuracy" was banned outright even
        in a bullet whose whole point was that the accuracy figure was
        misleading — the Bank held a claim its own validator rejected.

        A metric earns its category's exemption by meeting the stated
        conditions: it belongs to a permitted project, it has its own ID here,
        and it records ``model_variant`` and ``evaluation_protocol``. A metric
        that skips any of them is not exempt, which is the point — the
        conditions are the whole permission.
        """
        exemptions: dict[str, set[str]] = {}
        for entry in self.bank._raw["metrics"].get("forbidden_metrics", []) or []:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category", "FORBIDDEN"))
            cleared: set[str] = set()

            permitted_projects = {str(p) for p in (entry.get("permitted_for_projects") or ())}
            if permitted_projects:
                cleared |= {
                    metric_id
                    for metric_id, metric in self.bank.metrics.items()
                    if str(metric.get("source_project", "")) in permitted_projects
                    and metric.get("model_variant")
                    and metric.get("evaluation_protocol")
                }

            # A category may instead name its exceptions one by one - the
            # narrowest permission there is. SYSTEM_PERFORMANCE uses it for the
            # single recalled OpenText latency figure (M-075). A named metric is
            # exempt only if it also carries a non-strippable scope caveat: the
            # permission is for "~40% ... across targeted workflows", not for
            # the word "latency".
            for metric_id in entry.get("permitted_metric_ids") or ():
                metric = self.bank.metrics.get(str(metric_id))
                if (
                    metric is not None
                    and metric.get("mandatory_caveat")
                    and metric.get("caveat_strippable") is False
                ):
                    cleared.add(str(metric_id))
                    self._claim_bound_exemptions.add(str(metric_id))

            if cleared:
                exemptions[category] = cleared
        return exemptions

    def _cites_exempt_metric(self, lowered: str, category: str, text: str = "") -> bool:
        """True when the text quotes a figure this category explicitly permits.

        A metric exempted BY NAME is exempt only inside a sentence that traces
        to a safe claim citing it. "~40% ... across targeted workflows" is a
        permission for OpenText's recalled figure, not for any system's latency
        that happens to be written with a tilde and the same scope words.
        """
        for metric_id in self._category_exemptions.get(category, ()):
            if not self._mentions_metric_value(lowered, self.bank.metrics[metric_id]):
                continue
            if metric_id not in self._claim_bound_exemptions:
                return True
            if any(
                metric_id in (claim.raw.get("metric_ids") or ())
                for claim in self._match_safe_claims(text)
            ):
                return True
        return False

    # -- validation ------------------------------------------------------
    def validate(self, text: str, *, allow_learning_tier: bool = False) -> ClaimCheck:
        """Validate a single proposed claim sentence."""
        result = ClaimCheck(verdict="SUPPORTED")
        lowered = f" {text.lower()} "

        # 1. Prohibited claims — hard block.
        for term, claim_id, rule in self._prohibited_terms:
            if self._contains_term(lowered, term):
                result.verdict = "UNSUPPORTED"
                result.violations.append(
                    {"type": "PROHIBITED_CLAIM", "claim_id": claim_id, "term": term, "rule": rule}
                )
                result.reasons.append(f"{claim_id}: '{term}' — {rule}")

        # 1b. Verbatim prohibited phrasings (text-shaped DNC records).
        text_tokens = self._tokens(text)
        if text_tokens:
            for phrase_tokens, claim_id, boundary in self._prohibited_phrases:
                overlap = len(text_tokens & phrase_tokens) / len(phrase_tokens)
                if overlap >= 0.7:
                    result.verdict = "UNSUPPORTED"
                    result.violations.append(
                        {
                            "type": "PROHIBITED_PHRASING",
                            "claim_id": claim_id,
                            "overlap": round(overlap, 2),
                            "correct_boundary": boundary,
                        }
                    )
                    result.reasons.append(
                        f"{claim_id}: matches a prohibited phrasing. Correct boundary: {boundary}"
                    )

        # 2. Forbidden metric categories — only when a number is present.
        if _NUMBER_RE.search(text):
            for term, category in self._forbidden_metric_terms:
                if term and self._contains_term(lowered, term):
                    if self._cites_exempt_metric(lowered, category, text):
                        continue
                    result.verdict = "UNSUPPORTED"
                    result.violations.append(
                        {"type": "FORBIDDEN_METRIC", "term": term, "category": category}
                    )
                    result.reasons.append(
                        f"Forbidden metric '{term}' ({category}): no such measurement exists "
                        f"anywhere in the evidence base."
                    )

        # 2b. Per-metric safety: prohibited formulations, causality, caveats.
        for finding in self._check_metrics(text):
            result.violations.append(finding)
            result.reasons.append(finding["reason"])
            if finding["severity"] == "BLOCK":
                result.verdict = "UNSUPPORTED"
            elif result.verdict == "SUPPORTED":
                result.verdict = "PARTIAL"

        # 3. Safe-claim / evidence matching.
        matched = self._match_safe_claims(text)
        result.matched_claim_ids = [c.claim_id for c in matched]
        for claim in matched:
            result.matched_evidence_ids.extend(claim.evidence_ids)
        result.matched_evidence_ids = sorted(set(result.matched_evidence_ids))

        # 4. Ownership gating on first-person verbs.
        verb = self._first_person_verb(lowered)
        if verb:
            gated_ev = self._gated_evidence_for(text)
            if gated_ev and not result.blocked:
                alt = self._project_level_alternative(matched)
                result.verdict = "PARTIAL" if result.verdict == "SUPPORTED" else result.verdict
                result.ownership_downgrade = (
                    f"'{verb}' asserts personal authorship over evidence with unresolved "
                    f"ownership ({', '.join(gated_ev)})."
                )
                result.reasons.append(result.ownership_downgrade)
                if alt:
                    result.suggested_text = alt

        # 5. Skill support for technology mentions.
        weak = self._weak_skill_mentions(text, allow_learning_tier=allow_learning_tier)
        for skill_name, level in weak:
            result.violations.append(
                {"type": "WEAK_SKILL", "skill": skill_name, "evidence_level": level}
            )
            result.reasons.append(
                f"'{skill_name}' is {level} — not usable as a work/project claim."
            )
            if level in ZERO_WEIGHT_SKILL_LEVELS:
                result.verdict = "UNSUPPORTED"
            elif result.verdict == "SUPPORTED":
                result.verdict = "PARTIAL"

        # 6. No supporting evidence at all.
        if result.verdict == "SUPPORTED" and not matched:
            result.verdict = "PARTIAL"
            result.reasons.append(
                "No exact safe-claim match. Wording is not prohibited but is unverified — "
                "trace it to an Evidence ID before use."
            )

        return result

    # -- metric safety ---------------------------------------------------
    #: Verbs that assert causation. Blocked on CAUSALITY_NOT_ESTABLISHED metrics.
    CAUSAL_VERBS = frozenset(
        {
            "drove",
            "drive",
            "caused",
            "cause",
            "increased",
            "improved",
            "lifted",
            "boosted",
            "raised",
            "grew",
            "delivered",
            "achieved",
            "generated",
            "produced",
        }
    )

    def _check_metrics(self, text: str) -> list[dict[str, Any]]:
        """Validate any metric figures present against their safety fields."""
        findings: list[dict[str, Any]] = []
        lowered = f" {text.lower()} "
        text_tokens = self._tokens(text)
        # Prohibited formulations are compared on FIGURE-BEARING tokens. The
        # general tokenizer drops anything shorter than three characters, which
        # turned M-032's "90% accuracy" into the single token {accuracy} — a
        # two-word ban on one figure silently became a ban on the word, and any
        # bullet containing "accuracy" was blocked by a metric it never cited.
        metric_text_tokens = self._figure_tokens(text)

        for metric_id, metric in self.bank.metrics.items():
            statuses = set(metric.get("status") or [])

            # Explicit prohibited formulations — phrase overlap.
            for bad in metric.get("prohibited_formulations") or []:
                bad_tokens = self._figure_tokens(str(bad))
                # One surviving token is a word, not a formulation.
                if len(bad_tokens) < 2:
                    continue
                overlap = len(metric_text_tokens & bad_tokens) / len(bad_tokens)
                if overlap >= 0.8:
                    findings.append(
                        {
                            "type": "PROHIBITED_METRIC_FORMULATION",
                            "metric_id": metric_id,
                            "formulation": str(bad),
                            "severity": "BLOCK",
                            "reason": (
                                f"{metric_id}: '{bad}' is a prohibited formulation. "
                                f"{metric.get('precision_rule', '')}".strip()
                            ),
                        }
                    )

            # Causal verbs over a metric whose causality was never established.
            if "CAUSALITY_NOT_ESTABLISHED" in statuses and self._mentions_metric_value(
                lowered, metric
            ):
                for verb in self.CAUSAL_VERBS:
                    if re.search(rf"(?<![\w-]){verb}(?![\w-])", lowered):
                        findings.append(
                            {
                                "type": "CAUSAL_VERB_ON_OBSERVATIONAL_METRIC",
                                "metric_id": metric_id,
                                "verb": verb,
                                "severity": "BLOCK",
                                "reason": (
                                    f"{metric_id}: causality was never established, so '{verb}' "
                                    f"may not attach to this figure. "
                                    f"Allowed: {(metric.get('allowed_formulations') or [''])[0]}"
                                ),
                            }
                        )
                        break

            # A recalled approximation keeps its "~". "Cut round trips by 65%"
            # is not a shorter way to say "~65%": it turns a number the candidate
            # remembers into one that sounds measured.
            if metric.get("approximation_qualifier_required") and self._strips_qualifier(
                lowered, metric
            ):
                findings.append(
                    {
                        "type": "APPROXIMATION_QUALIFIER_STRIPPED",
                        "metric_id": metric_id,
                        "severity": "BLOCK",
                        "reason": (
                            f"{metric_id}: '{metric.get('value')}' must keep its approximation "
                            f"marker. {metric.get('precision_rule', '')}".strip()
                        ),
                    }
                )

            # Mandatory, non-strippable caveat must travel with the metric.
            caveat = metric.get("mandatory_caveat")
            if (
                caveat
                and metric.get("caveat_strippable") is False
                and self._mentions_metric_value(lowered, metric)
            ):
                caveat_tokens = self._tokens(str(caveat))
                present = (
                    len(text_tokens & caveat_tokens) / len(caveat_tokens) >= 0.4
                    if caveat_tokens
                    else True
                )
                if not present:
                    findings.append(
                        {
                            "type": "MISSING_MANDATORY_CAVEAT",
                            "metric_id": metric_id,
                            # WARN by default, which is what every metric here
                            # has always produced. A metric may raise its own
                            # stakes with `caveat_missing_severity: BLOCK`, for
                            # the figures where the caveat IS the claim rather
                            # than a qualification of it: "90.8% accuracy"
                            # without "8.2% churn recall" is not a shorter true
                            # sentence, it is the opposite of the finding.
                            "severity": str(metric.get("caveat_missing_severity") or "WARN"),
                            "reason": (
                                f"{metric_id}: mandatory caveat is missing and cannot be dropped "
                                f"for brevity — {str(caveat)[:160]}"
                            ),
                        }
                    )
        return findings

    @classmethod
    def _mentions_metric_value(cls, lowered: str, metric: dict[str, Any]) -> bool:
        """True when the text appears to cite this metric's numeric value."""
        pattern = cls._metric_value_pattern(metric)
        return pattern is not None and pattern.search(lowered) is not None

    @staticmethod
    def _metric_value_pattern(metric: dict[str, Any]) -> re.Pattern[str] | None:
        """Where this metric's figure appears in a sentence.

        Compared on digits so "~12" matches "12%". A PERCENTAGE value must also
        be quoted as one: "~40%" is not cited by "40,460 captions", and with a
        BLOCK-severity caveat on a two-digit percentage, matching bare digits
        would reject unrelated bullets for a figure they never mention.
        """
        raw = str(metric.get("value", "")).strip()
        digits = re.sub(r"[^\d.]", "", raw)
        if not digits:
            return None
        suffix = r"\s*%" if "%" in raw else r"(?![\d.])"
        return re.compile(rf"(?<![\d.,]){re.escape(digits)}{suffix}")

    #: What may stand in for "~" in running text.
    _APPROXIMATION_MARKERS = ("~", "approximately", "approx.", "roughly", "about", "around")

    @classmethod
    def _strips_qualifier(cls, lowered: str, metric: dict[str, Any]) -> bool:
        """True when any citation of this figure has lost its approximation marker.

        A percentage is identified by its "%". A bare count is not identifiable
        by digits alone - "~2" would claim every "2" on the page - so it must be
        followed by the first word of its unit ("~2 months").
        """
        pattern = cls._metric_value_pattern(metric)
        if pattern is None:
            return False
        if "%" not in str(metric.get("value", "")):
            unit = re.match(r"[a-z]+", str(metric.get("unit", "")).lower())
            if unit is None:
                return False
            pattern = re.compile(rf"{pattern.pattern}\s*-?\s*{re.escape(unit.group())}")
        for match in pattern.finditer(lowered):
            before = lowered[: match.start()].rstrip()
            if not before.endswith(cls._APPROXIMATION_MARKERS):
                return True
        return False

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _contains_term(haystack_lower_padded: str, term: str) -> bool:
        """Word-boundary-ish containment that tolerates multi-word terms."""
        return (
            re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", haystack_lower_padded) is not None
        )

    @staticmethod
    def _first_person_verb(lowered: str) -> str | None:
        for verb in FIRST_PERSON_OWNERSHIP_VERBS:
            if re.search(rf"(?<![\w-]){verb}(?![\w-])", lowered):
                return verb
        return None

    def _match_safe_claims(self, text: str) -> list[Any]:
        """Token-overlap match against safe claims. Deliberately conservative."""
        tokens = self._tokens(text)
        if not tokens:
            return []
        scored: list[tuple[float, Any]] = []
        for claim in self.bank.safe_claims.values():
            claim_tokens = self._tokens(claim.text)
            if not claim_tokens:
                continue
            overlap = len(tokens & claim_tokens) / len(claim_tokens)
            if overlap >= 0.5:
                scored.append((overlap, claim))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [claim for _, claim in scored[:3]]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        stop = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "of",
            "to",
            "for",
            "in",
            "on",
            "with",
            "using",
            "across",
            "from",
            "by",
            "at",
            "as",
            "into",
            "over",
            "this",
            "that",
            "was",
            "were",
        }
        words = re.findall(r"[a-z0-9#+.]+", text.lower())
        return {w for w in words if w not in stop and len(w) > 2}

    @classmethod
    def _figure_tokens(cls, text: str) -> set[str]:
        """:meth:`_tokens`, but short NUMBERS survive.

        "90", "9", "12" are the discriminating half of a metric formulation and
        the only thing separating one figure's prohibited wording from another
        figure's permitted one.
        """
        words = re.findall(r"[a-z0-9#+.]+", text.lower())
        return cls._tokens(text) | {w for w in words if any(ch.isdigit() for ch in w)}

    def _gated_evidence_for(self, text: str) -> list[str]:
        """Evidence IDs with unresolved ownership referenced by this text."""
        hits: set[str] = set()
        for token in self._tokens(text):
            for rec in self.bank.find_evidence_by_keyword(token):
                if rec.ownership_gated:
                    hits.add(rec.evidence_id)
        return sorted(hits)

    def _project_level_alternative(self, matched: list[Any]) -> str | None:
        for claim in matched:
            text, downgraded = self.bank.usable_claim_text(claim.claim_id)
            if downgraded and text:
                return text
        return None

    def _weak_skill_mentions(
        self, text: str, *, allow_learning_tier: bool
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        lowered = f" {text.lower()} "
        for skill in self.bank.skills.values():
            level = skill.evidence_level
            if level == "WORK_PROJECT_VERIFIED":
                continue
            if allow_learning_tier and level == "LEARNING_NOT_RESUME_READY":
                continue
            if level in {"COURSEWORK_VERIFIED", "USER_CONFIRMED_PRACTICE", "SELF_REPORTED_LIMITED"}:
                # These are usable in a labelled tier; not a violation on their own.
                continue
            if self._contains_term(lowered, skill.normalized_name):
                out.append((skill.name, level))
        return out


def get_claim_validator() -> SafeClaimValidator:
    return SafeClaimValidator()
