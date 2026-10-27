"""Ranks and groups Evidence-Bank material against one posting.

This is the layer that was missing. The base resume knew which *family* of roles
it was for; nothing knew what a specific posting was about. So a real
Analytics Engineer resume opened its Skills section with CNNs, computer vision
and LSTMs — safe, true, and useless for that job.

Everything here is RANKING. Three hard boundaries hold:

* A bullet is the verbatim text of a safe claim, chosen from the same set the
  untargeted builder draws from. Targeting changes the order and the selection,
  never a word of the text.
* A skill reaches the page only if ``skill.resume_usable`` is true. The JD is
  consulted to decide *where in the list* a skill goes, never *whether* it
  exists. Tableau, Power BI, Looker, Sigma, AWS, Airflow and RAG cannot be
  promoted onto a resume by any posting, because the Bank does not clear them.
* Ownership is untouched. A gated claim still renders through its
  ``project_level_alternative`` or not at all.

One more input sits alongside the JD: the candidate's own preference about how
often they want to see a project. A project may carry
``resume_selection_priority`` in the Evidence Bank, and the multiplier each
label maps to is configuration (``selection_priority_modifiers``). It scales an
entry's bullet utility and nothing else — a de-prioritised project keeps every
claim it had and still wins a posting that genuinely matches it. No project name
appears here; the preference travels as data.

A SAFE CLAIM may carry the same label (2026-09-24). It expresses an order of
preference WITHIN an entry - "lead OpenText with the engineering work, then the
testing" - using the same multipliers and the same fade, so a posting that asks
for exactly what a NORMAL claim says can still put that claim first.

The utility score is deterministic and its dimensions are separated so a
selection can be explained — "this beat that on requirement match" is a
reviewable statement; a single opaque number is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.service import EvidenceBankService, SkillRecord
from app.services.bullet_quality import get_bullet_quality_scorer
from app.services.role_targeting import _STOPWORDS as _JD_STOPWORDS
from app.services.role_targeting import RoleTargetProfile, load_targeting_config

_WORD_RE = re.compile(r"[a-z0-9+#./-]+")
_NUMBER_RE = re.compile(r"\d")

#: Claim strength → its contribution to evidence_strength.
_STRENGTH_SCORE = {"STRONG": 1.0, "SOLID": 0.65, "MODEST": 0.35}

#: A gated claim renders as project-level wording, which is weaker on a resume
#: than a first-person one. Scored lower so an equally relevant ungated claim
#: wins — but never excluded, because project-level truth still beats silence.
_OWNERSHIP_DOWNGRADED = 0.55

#: Why a skill is on the page (D-70). Page fitting reads these: a DIRECT_JD or
#: PINNED skill is never trimmed, an inferred one outlasts a core one, and a
#: core one goes before the strongest project content does.
SKILL_DIRECT = "DIRECT_JD"
SKILL_INFERRED = "STRONG_INFERENCE"
SKILL_CORE = "CORE_ROLE"
SKILL_PINNED = "CORE_PINNED"


@dataclass
class BulletScore:
    """One safe claim's job-specific utility, with its reasons kept."""

    claim_id: str
    owner: str
    text: str
    total: float
    dimensions: dict[str, float] = field(default_factory=dict)
    ownership_downgraded: bool = False
    strength: str = "SOLID"

    @property
    def top_reasons(self) -> list[str]:
        """The two dimensions that most explain this score."""
        ranked = sorted(self.dimensions.items(), key=lambda kv: -kv[1])
        return [name for name, value in ranked[:2] if value > 0.15]


@dataclass
class TargetingExplanation:
    """What tailoring did, in the user's language rather than the scorer's."""

    archetype: str
    archetype_label: str
    intensity: str
    resume_base: str
    prioritized: list[str] = field(default_factory=list)
    deemphasized: list[str] = field(default_factory=list)
    lead_projects: list[str] = field(default_factory=list)
    unsupported_jd_terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "archetype": self.archetype,
            "archetype_label": self.archetype_label,
            "intensity": self.intensity,
            "resume_base": self.resume_base,
            "prioritized": self.prioritized,
            "deemphasized": self.deemphasized,
            "lead_projects": self.lead_projects,
            "unsupported_jd_terms": self.unsupported_jd_terms,
            "notes": self.notes,
        }


class ResumeTargetingEngine:
    """Scores claims, groups skills and frames a summary for one profile."""

    def __init__(
        self,
        bank: EvidenceBankService,
        profile: RoleTargetProfile,
        *,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.bank = bank
        self.profile = profile
        self.config = config or load_targeting_config()
        levels = (self.config.get("targeting_intensity") or {}).get("levels") or {}
        self.weights: dict[str, float] = {
            key: float(value)
            for key, value in (
                levels.get(profile.intensity) or levels.get("BALANCED") or {}
            ).items()
        }
        self._business_terms = tuple(self.config.get("business_impact_terms") or ())
        self._technical_terms = tuple(self.config.get("technical_depth_terms") or ())
        self._quality = get_bullet_quality_scorer(bank)
        self._spec = self._archetype_spec()
        self._selection_modifiers = self._load_selection_modifiers()
        #: name -> SKILL_* tier for every skill the last skill_groups() rendered.
        self.skill_tiers: dict[str, str] = {}

    def _load_selection_modifiers(self) -> dict[str, float]:
        """Owner name → the user's selection-preference multiplier.

        Keyed by the project's own ``name``, which is what an entry reports as
        its owner. Projects with no preference, or a label the config does not
        define, simply do not appear — the lookup then returns 1.0 and nothing
        about the scoring changes.
        """
        table = self.config.get("selection_priority_modifiers") or {}
        modifiers: dict[str, float] = {}
        for project in self.bank.projects.values():
            label = project.get("resume_selection_priority")
            name = str(project.get("name") or "").strip().lower()
            if not label or not name or label not in table:
                continue
            modifier = float(table[label])
            if modifier != 1.0:
                modifiers[name] = modifier
        return modifiers

    def selection_modifier(self, owner: str) -> float:
        """The candidate's standing preference for this entry, as a multiplier."""
        return self._selection_modifiers.get(owner.strip().lower(), 1.0)

    def claim_selection_modifier(self, claim: Any) -> float:
        """The candidate's standing preference for one claim, as a multiplier.

        Read from the claim's own ``resume_selection_priority``. Unlabelled, or
        a label the config does not define, is 1.0 - nothing changes.
        """
        raw = getattr(claim, "raw", None) or {}
        label = raw.get("resume_selection_priority")
        table = self.config.get("selection_priority_modifiers") or {}
        if not label or label not in table:
            return 1.0
        return float(table[label])

    def _archetype_spec(self) -> dict[str, Any]:
        for spec in self.config.get("archetypes") or []:
            if spec.get("name") == self.profile.role_archetype:
                return dict(spec)
        return {}

    # -- bullets ---------------------------------------------------------
    def score_claim(
        self,
        claim: Any,
        *,
        owner: str,
        text: str,
        downgraded: bool,
        recency: float = 0.5,
        covered_tokens: set[str] | None = None,
    ) -> BulletScore:
        """Job-specific utility for one safe claim.

        ``covered_tokens`` carries what earlier selections already said, so the
        fourth bullet about the same pipeline scores lower than the first line
        about a different capability. Without it a targeted resume degenerates
        into five restatements of whichever project matched best.
        """
        tokens = _tokens(text)
        covered = covered_tokens or set()

        required_overlap = len(tokens & self.profile.required_tokens)
        jd_overlap = len(tokens & self.profile.jd_tokens)

        dimensions = {
            "requirement_match": min(1.0, required_overlap / 6.0),
            "keyword_alignment": min(1.0, jd_overlap / 12.0),
            "role_family_relevance": self._family_relevance(claim),
            "evidence_strength": _STRENGTH_SCORE.get(getattr(claim, "strength", ""), 0.35),
            "metric_strength": self._metric_strength(text),
            "ownership_confidence": _OWNERSHIP_DOWNGRADED if downgraded else 1.0,
            "recency": max(0.0, min(1.0, recency)),
            "uniqueness": self._uniqueness(tokens, covered),
            "business_impact": _term_density(text, self._business_terms),
            "technical_depth": _term_density(text, self._technical_terms),
            # X-Y-Z: does the line say what was achieved, or only what was
            # touched? A preference, never a filter — a bullet the heuristic
            # misreads loses a fraction of a point and still competes. See
            # app/services/bullet_quality.py.
            "bullet_quality": self._quality.score(text, claim=claim).score,
        }

        total = sum(self.weights.get(name, 0.0) * value for name, value in dimensions.items())

        # Space is the scarce resource on a one-page resume: a 240-character
        # bullet has to earn the room two shorter ones would have used.
        space_cost = min(1.0, max(0.0, (len(text) - 120) / 160.0))
        dimensions["space_cost"] = -space_cost
        total -= self.weights.get("space_cost", 0.3) * space_cost

        # De-emphasis is part of tailoring, not a filter: a claim about a
        # capability this role does not want still scores, just lower. If the
        # posting itself asks for it the penalty does not apply.
        penalty = self._deemphasis_penalty(tokens)
        if penalty:
            dimensions["deemphasis_penalty"] = -penalty
            total -= penalty

        # The candidate's own standing preference about this project, applied
        # LAST so it scales the finished utility rather than one dimension of
        # it, and FADED by how hard this posting pulls for this line. A
        # preference is what to do when the job description has no opinion; a
        # job description that names the work is the opinion, and it wins.
        # Recorded as the adjustment it made, so a selection stays reviewable:
        # "this lost on preference" is a statement someone can disagree with.
        # A CLAIM-level preference orders an entry's lines where that line is
        # on-family for this page. Scaled by role_family_relevance, a HIGHER
        # label on backend work leads a software resume and barely moves a
        # data-science one - "lead OpenText with the engineering" must not
        # become "give OpenText more of a data-science page".
        claim_modifier = 1.0 + (self.claim_selection_modifier(claim) - 1.0) * dimensions[
            "role_family_relevance"
        ]
        modifier = self.selection_modifier(owner) * claim_modifier
        if modifier != 1.0:
            engagement = max(
                dimensions["requirement_match"], dimensions["keyword_alignment"]
            )
            effective = 1.0 - (1.0 - modifier) * (1.0 - engagement)
            adjusted = total * effective
            dimensions["selection_priority"] = round(adjusted - total, 3)
            total = adjusted

        return BulletScore(
            claim_id=getattr(claim, "claim_id", ""),
            owner=owner,
            text=text,
            total=round(total, 4),
            dimensions={k: round(v, 3) for k, v in dimensions.items()},
            ownership_downgraded=downgraded,
            strength=getattr(claim, "strength", "SOLID"),
        )

    def _family_relevance(self, claim: Any) -> float:
        wanted = self.profile.claim_role_families
        if not wanted:
            return 0.5
        families = set(getattr(claim, "role_families", ()) or ())
        best = None
        for index, family in enumerate(wanted):
            if family in families:
                best = index
                break
        if best is None:
            return 0.0
        # Linear decay: first-choice family 1.0, last 0.2.
        span = max(1, len(wanted) - 1)
        return round(1.0 - 0.8 * (best / span), 3)

    @staticmethod
    def _metric_strength(text: str) -> float:
        figures = len(re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", text))
        if figures == 0:
            return 0.0
        return 1.0 if figures >= 2 else 0.6

    @staticmethod
    def _uniqueness(tokens: set[str], covered: set[str]) -> float:
        if not tokens:
            return 0.0
        fresh = tokens - covered
        return round(len(fresh) / len(tokens), 3)

    def _deemphasis_penalty(self, tokens: set[str]) -> float:
        penalty = 0.0
        for phrase in self.profile.deemphasize:
            for word in _tokens(phrase):
                if word in tokens and word not in self.profile.jd_tokens:
                    penalty += 0.35
                    break
        return round(min(penalty, 1.4), 3)

    # -- skills ----------------------------------------------------------
    def skill_groups(self) -> list[tuple[str, list[str]]]:
        """Role-appropriate skill categories, in role-appropriate order.

        Not a reorder of one fixed list: the archetype decides the *category
        structure* too, which is why an Analytics Engineering resume shows
        "Programming & Querying / Data Engineering / Databases / Analytics"
        rather than the DS base's "Languages / Machine Learning / …".

        The set of skills is fixed by the Evidence Bank before this runs. A JD
        term can move a skill up its group, or admit a cleared skill the role
        would not otherwise show; it can never add one the Bank does not clear.

        **There is no count cap** (D-69), and **a category is not a reason**
        (D-70). A group's categories decide where a skill would sit and make it
        a CANDIDATE; :meth:`skill_tier` decides whether it renders. A group of
        nine materially relevant skills renders nine; a group whose candidates
        are merely adjacent to the role renders none. The tier of every
        rendered skill is kept in :attr:`skill_tiers` for page fitting and for
        the explanation.
        """
        self.skill_tiers = {}
        specs: list[dict[str, Any]] = list(self._spec.get("skill_groups") or [])
        if not specs:
            return []

        usable = [s for s in self.bank.skills.values() if s.resume_usable]
        core = self._core_skills()
        claimed: set[str] = set()
        groups: list[tuple[str, list[str], bool]] = []

        for spec in specs:
            label = str(spec.get("label") or "Skills")
            include = [str(n) for n in (spec.get("include") or [])]
            categories = set(spec.get("categories") or ())
            excluded = set(spec.get("exclude_categories") or ())

            names: list[str] = []
            # Explicit names are claimed first so a skill that belongs to
            # several categories lands where the archetype wants it. A pinned
            # skill is the archetype's own floor and is never trimmed to fit.
            for wanted in include:
                skill = self.bank.get_skill_by_name(wanted)
                if skill is None or not skill.resume_usable or skill.name in claimed:
                    continue
                names.append(skill.name)
                claimed.add(skill.name)
                self.skill_tiers[skill.name] = (
                    SKILL_DIRECT if self._mentioned_in_jd(skill.name) else SKILL_PINNED
                )

            for skill in usable:
                if skill.name in claimed:
                    continue
                skill_categories = set(skill.categories)
                if not (categories & skill_categories):
                    continue
                if excluded & skill_categories:
                    continue
                tier = self.skill_tier(skill, core)
                if tier is None:
                    # SUPPORTING: true and adjacent, not worth the space here.
                    continue
                names.append(skill.name)
                claimed.add(skill.name)
                self.skill_tiers[skill.name] = tier

            if not names:
                continue

            # A group counts as de-emphasised when MOST of what it holds is
            # de-emphasised, not only when all of it is. It sinks to the bottom
            # of the block, where page fitting reaches it first.
            deemphasized = bool(
                categories
                and len(categories & set(self.profile.deemphasized_categories)) * 2
                >= len(categories)
            )
            names = self._order_skills(
                names,
                pinned=set(include),
                preferred=[str(n) for n in (spec.get("prefer") or [])],
            )
            groups.append((label, names, deemphasized))

        # De-emphasised groups sink to the bottom. They are not deleted: the
        # Evidence Bank still holds them and a later role will lead with them.
        ordered = [g for g in groups if not g[2]] + [g for g in groups if g[2]]
        return [(label, names) for label, names, _ in ordered]

    def _core_skills(self) -> set[str]:
        """The archetype's curated core: ``core_skills`` plus what it pins and summarises.

        Curated by name on purpose. "Belongs to a Data Science category" is
        true of fifty skills; "a strong general Data Science resume would list
        it" is true of about fifteen, and only a person can say which.
        """
        names: set[str] = set()
        for spec in self._spec.get("skill_groups") or []:
            names.update(str(n).lower() for n in (spec.get("include") or []))
        names.update(str(n).lower() for n in (self._spec.get("core_skills") or []))
        names.update(str(n).lower() for n in (self._spec.get("summary_skills") or []))
        return names

    def _inferred_from_jd(self, skill_name: str) -> bool:
        """The posting describes a capability this demonstrated skill implements.

        ``skill_inference`` in role_targeting.yaml maps capability phrases
        ("tree-based models", "RESTful services") to the Bank skills that
        implement them (XGBoost, Random Forest; REST APIs, Flask). A phrase
        matches when every one of its words is in the posting. The rule names
        the skill, so this can only ever admit a skill the Bank already clears.
        """
        if not self.profile.jd_tokens:
            return False
        wanted = skill_name.lower()
        for rule in self.config.get("skill_inference") or []:
            if wanted not in {str(s).lower() for s in rule.get("skills") or []}:
                continue
            for phrase in rule.get("signals") or []:
                words = _content_words(str(phrase))
                if words and words <= self._jd_words():
                    return True
        return False

    def skill_tier(self, skill: Any, core: set[str] | None = None) -> str | None:
        """DIRECT_JD, STRONG_INFERENCE, CORE_ROLE - or None, which is not rendered.

        None covers SUPPORTING (a candidate by category, adjacent to the role)
        and IRRELEVANT alike: neither earns space without the posting.
        """
        if self._mentioned_in_jd(skill.name):
            return SKILL_DIRECT
        if self._inferred_from_jd(skill.name):
            return SKILL_INFERRED
        core = self._core_skills() if core is None else core
        if skill.name.lower() in core:
            return SKILL_CORE
        return None

    def _order_skills(
        self, names: list[str], *, pinned: set[str], preferred: list[str] | None = None
    ) -> list[str]:
        """Pinned, then the posting, then the role, then the evidence, then A-Z.

        1. pinned by the group (``include``) - Python and SQL lead their line;
        2. named by the posting, the most-repeated across its requirements first;
        3. inferred from the posting;
        4. core to the role, in the archetype's ``prefer`` and ``summary_skills``
           order - its own statement of what matters most;
        5. evidence strength, WORK_PROJECT_VERIFIED first;
        6. the name, only so the order is deterministic.

        The last skill in a group is therefore the one page fitting may take
        first. Order never decides membership.
        """
        order = [*(preferred or []), *(self._spec.get("summary_skills") or [])]
        ranks: dict[str, int] = {}
        for index, name in enumerate(order):
            ranks.setdefault(str(name).lower(), index)
        tier_rank = {SKILL_DIRECT: 0, SKILL_INFERRED: 1}

        def sort_key(name: str) -> tuple[int, int, int, int, int, int, str]:
            skill = self.bank.get_skill_by_name(name)
            mentioned = 0 if self._mentioned_in_jd(name) else 1
            repeated = -self._requirement_mentions(name)
            tier = tier_rank.get(self.skill_tiers.get(name, ""), 2)
            rank = ranks.get(name.lower(), len(ranks) + 1)
            verified = 0 if skill and skill.evidence_level == "WORK_PROJECT_VERIFIED" else 1
            return (
                0 if name in pinned else 1,
                mentioned,
                repeated,
                tier,
                rank,
                verified,
                name.lower(),
            )

        return sorted(names, key=sort_key)

    def _requirement_mentions(self, skill_name: str) -> int:
        """How many of the posting's requirement lines name this skill."""
        parts = [words for p in re.split(r"[/,]", skill_name) if (words := _content_words(p))]
        return sum(
            1
            for line in self.profile.required_capabilities
            if any(part <= _content_words(line) for part in parts)
        )

    def _mentioned_in_jd(self, skill_name: str) -> bool:
        """True when the posting uses any word of this skill's name.

        Compound Bank names ("Spark / PySpark", "ETL / ELT / data pipelines")
        never appear verbatim in a JD, so the parts are tested individually.
        """
        jd = self._jd_words()
        for part in re.split(r"[/,]", skill_name):
            words = _content_words(part)
            if words and words <= jd:
                return True
        return False

    def _jd_words(self) -> frozenset[str]:
        """The posting's tokens, normalised the way skill names are.

        ``jd_tokens`` drops stopwords ("data", "build", "stakeholders") and
        keeps trailing punctuation ("sql." at the end of a sentence). Matching
        raw skill words against it meant "Data quality monitoring" could never
        match a posting that says exactly that, and a sentence-final "SQL" did
        not count as asked for (D-70).
        """
        return frozenset(t.rstrip(".,;:") for t in self.profile.jd_tokens)

    # -- summary ---------------------------------------------------------
    def summary(
        self,
        *,
        degree_short: str,
        institution: str,
        aliases: dict[str, str] | None = None,
        max_skills: int = 8,
    ) -> str:
        """A role-specific professional summary, built from cleared facts only.

        The sentence frame is authored config, reviewed like every other
        selection in ``role_targeting.yaml``. The facts substituted into it come
        from the Evidence Bank: the degree and institution from candidate.yaml,
        the skills from skills.yaml filtered to resume-usable levels.

        The frame is per-ARCHETYPE rather than per-base, which is the whole
        point — the old single DS_ML_AI summary appeared verbatim on an
        analytics-engineering application.
        """
        template = str(self._spec.get("summary_template") or "").strip()
        if not template:
            return ""

        skills = self.summary_skills(aliases=aliases, limit=max_skills)
        if not skills:
            return ""

        return " ".join(
            template.format(
                degree_short=degree_short,
                institution=institution,
                skills=", ".join(skills),
            ).split()
        )

    def summary_skills(self, *, aliases: dict[str, str] | None = None, limit: int = 8) -> list[str]:
        """Resume-usable skills this archetype leads with, JD-relevant first."""
        aliases = aliases or {}
        wanted = [str(n) for n in (self._spec.get("summary_skills") or [])]

        cleared: list[tuple[int, int, str]] = []
        for index, name in enumerate(wanted):
            skill = self.bank.get_skill_by_name(name)
            # A skill the Bank does not clear never reaches the summary, however
            # prominently the posting asks for it. The alias is applied only
            # AFTER this gate and only to shorten the display.
            if skill is None or not skill.resume_usable:
                continue
            mentioned = 0 if self._mentioned_in_jd(skill.name) else 1
            cleared.append((mentioned, index, str(aliases.get(skill.name, skill.name))))

        cleared.sort()
        out: list[str] = []
        for _, _, display in cleared:
            if display not in out:
                out.append(display)
        return out[:limit]

    # -- explanation -----------------------------------------------------
    def explain(
        self,
        *,
        selected: list[BulletScore],
        skill_groups: list[tuple[str, list[str]]],
        lead_projects: list[str],
    ) -> TargetingExplanation:
        """The "why this resume looks different" panel, without the formulas."""
        # Only skills that are actually on-target for this role. Taking the
        # first few of each leading group instead listed "C# · JavaScript" as
        # PRIORITIZED for an analytics-engineering job — true of where they sit
        # on the page, and nonsense as an explanation of the tailoring.
        wanted = {str(n).lower() for n in (self._spec.get("summary_skills") or [])}
        prioritized: list[str] = []
        for _label, names in skill_groups:
            for name in names:
                if name.lower() not in wanted and not self._mentioned_in_jd(name):
                    continue
                short = _short_skill(name)
                if short not in prioritized:
                    prioritized.append(short)
        for competency in self.profile.primary_competencies:
            title = competency if competency.isupper() else competency.title()
            if title not in prioritized:
                prioritized.append(title)

        deemphasized = [_pretty_category(c) for c in sorted(self.profile.deemphasized_categories)]

        notes: list[str] = []
        if self.profile.business_context:
            notes.append(self.profile.business_context)
        gated = [s for s in selected if s.ownership_downgraded]
        if gated:
            notes.append(
                f"{len(gated)} selected line(s) use project-level wording because personal "
                f"ownership is unresolved in the Evidence Bank."
            )
        # Naming it keeps the preference visible. A project quietly losing to a
        # setting the user forgot they made is the failure mode here.
        preferred = sorted(
            {
                s.owner
                for s in selected
                if s.dimensions.get("selection_priority", 0.0)
                and self.selection_modifier(s.owner) < 1.0
            }
        )
        if preferred:
            notes.append(
                "Ranked lower by your standing project preference, and still selected: "
                + ", ".join(preferred)
                + "."
            )

        return TargetingExplanation(
            archetype=self.profile.role_archetype,
            archetype_label=self.profile.archetype_label,
            intensity=self.profile.intensity,
            resume_base=self.profile.resume_base,
            prioritized=prioritized[:8],
            deemphasized=deemphasized[:6],
            lead_projects=lead_projects[:4],
            unsupported_jd_terms=self.unsupported_jd_terms(),
            notes=notes,
        )

    def unsupported_jd_terms(self, *, limit: int = 10) -> list[str]:
        """Tools the posting names that the Evidence Bank refuses to support.

        Surfaced so the omission is visible and deliberate. This list is the
        inverse of a keyword-stuffing feature: it exists to show what was
        correctly left OFF the page.
        """
        out: list[str] = []
        for term in self.profile.tooling_terms:
            skill: SkillRecord | None = self.bank.get_skill_by_name(term)
            if skill is None:
                continue
            if not skill.resume_usable and skill.name not in out:
                out.append(skill.name)
        return out[:limit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2}


def _content_words(text: str) -> set[str]:
    """Tokens as the posting's own token set holds them: no stopwords, no trailing punctuation."""
    return {t.rstrip(".,;:") for t in _tokens(text)} - _JD_STOPWORDS


def _term_density(text: str, terms: tuple[str, ...]) -> float:
    if not terms:
        return 0.0
    lowered = text.lower()
    hits = sum(1 for term in terms if term in lowered)
    return min(1.0, hits / 3.0)


def _short_skill(name: str) -> str:
    """ "Spark / PySpark" → "Spark". Display only; the Bank name is unchanged."""
    return name.split("/")[0].split("-")[0].strip() or name


#: Category slugs whose title-cased form reads wrong.
_CATEGORY_LABELS = {
    "NLP": "NLP",
    "AI_LLM": "LLM / AI tooling",
    "RESPONSIBLE_AI": "Responsible AI",
    "SQL": "SQL",
    "API": "APIs",
}


def _pretty_category(slug: str) -> str:
    return _CATEGORY_LABELS.get(slug, slug.replace("_", " ").title())
