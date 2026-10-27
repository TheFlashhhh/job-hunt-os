"""Derives a RoleTargetProfile from a parsed posting.

The problem this exists to solve is POSITIONING, not truthfulness.

A real "Analytics Engineer, Data Science" posting classifies as ``DATA_SCIENTIST``
— correctly, on its title — and the DS_ML_AI base then led the page with CNNs,
computer vision and LSTMs. Every one of those lines was true and every one was
irrelevant: the posting is about SQL transformation, ETL/ELT, data quality and
stakeholder requirements. The resume was safe and generic at the same time.

A role family answers "which base?". An ARCHETYPE answers "what is this job
actually about?", and that is what decides which safe claims lead, how skills
are grouped, and what gets pushed down the page.

**Three concepts, kept apart on purpose.**

``EVIDENCE``             what the candidate actually did — the Evidence Bank, and
                         the only thing that can put words on a resume.
``SEMANTIC POSITIONING`` how that truthful evidence is framed for this role —
                         ordering and grouping, which is all this module does.
``JD KEYWORDS``          what the employer wrote. Matched against evidence,
                         never copied onto the page.

Nothing here can introduce a capability. The profile is a set of *terms to rank
against*; the ranking runs over safe claims and resume-usable skills that the
Evidence Bank already cleared. A posting demanding Tableau, Looker, Sigma, AWS,
Airflow or RAG changes the order of what the candidate has and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.config import load_yaml_config

CONFIG_NAME = "role_targeting.yaml"

#: How hard targeting reorders. No level unlocks unsupported content.
INTENSITY_LEVELS = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE_SAFE")

_WORD_RE = re.compile(r"[a-z0-9+#./-]+")

#: Tokens too common to carry positioning signal. The JD parser keeps every
#: word of a requirement line as a keyword, so without this the "tooling the
#: posting names" list came back as `data, in, to, or, at, by, of` — true,
#: useless, and displayed to the user.
#: Kept as prose rather than a list literal — a 60-word stopword list is
#: reviewable this way and unreadable as one item per line.
_STOPWORD_TEXT = """
    and the for with you our are will have this that from your who all can not
    any may work team role job years experience ability strong excellent
    including across within other more such each into their them was were been
    but its has had how why when where what which while about above under over
    also than then they there here them these those very much many some most
    per via out off own use used using able like just get got make made need
    should would could must shall might upon both same both else ever every
    data business tools least similar comfort environment fast paced self
    starter organizing interpret market information degree domain scope needs
    solve resolve generate enable scale increase support help build design
    develop developing lead leading maintain maintaining creating solutions
    high low new key close driven cross functional non technical teams
    stakeholders scientists engineers partner partners company org orgs
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


@lru_cache(maxsize=1)
def load_targeting_config() -> dict[str, Any]:
    return load_yaml_config(CONFIG_NAME)


def reset_targeting_config_cache() -> None:
    load_targeting_config.cache_clear()


#: Two target functions have no archetype of their own, because the archetype
#: set was built for resume positioning and these are positioning-equivalent to
#: an existing one. Mapping them here is what keeps ONE targeting pipeline
#: instead of a second, disagreeing one.
FUNCTION_TO_ARCHETYPE = {
    "DATA_ANALYTICS": "PRODUCT_ANALYTICS",
    "DECISION_ANALYTICS": "DATA_SCIENCE",
}


@dataclass(frozen=True)
class RoleTargetProfile:
    """What this specific posting is asking for, in ranking terms.

    Every field is either derived from the posting or read from reviewed config.
    None of it is a permission: the profile is consumed by rankers that only
    ever choose among Evidence-Bank-cleared material.
    """

    role_family: str
    role_archetype: str
    archetype_label: str
    resume_base: str

    primary_competencies: tuple[str, ...] = ()
    secondary_competencies: tuple[str, ...] = ()
    deemphasize: tuple[str, ...] = ()
    deemphasized_categories: frozenset[str] = frozenset()

    #: Vocabulary the posting itself uses, split by what it is used for.
    domain_terms: tuple[str, ...] = ()
    tooling_terms: tuple[str, ...] = ()

    business_context: str = ""
    stakeholder_expectations: tuple[str, ...] = ()
    data_system_expectations: tuple[str, ...] = ()
    modeling_expectations: tuple[str, ...] = ()

    top_required_capabilities: tuple[str, ...] = ()
    top_optional_capabilities: tuple[str, ...] = ()
    #: Every MUST_HAVE / STRONG_PREFERENCE line, verbatim. The audit measures
    #: coverage per REQUIREMENT rather than per token — "how many of the things
    #: they asked for does this answer" is a unit a human can check, where a
    #: token overlap mostly measures how much English a document contains.
    required_capabilities: tuple[str, ...] = ()

    experience_bar: str = ""
    location_requirements: tuple[str, ...] = ()

    #: Claim role_families that count as on-target for this archetype, best
    #: first. Drives the role_family_relevance dimension.
    claim_role_families: tuple[str, ...] = ()

    #: The order graduate courses appear in for this archetype. ORDERING ONLY:
    #: every name is resolved against the Evidence Bank's canonical course list
    #: and silently dropped if the Bank does not carry it, so a posting can
    #: never introduce a course through this field.
    coursework_priority: tuple[str, ...] = ()

    #: Every meaningful token in the posting. The keyword_alignment dimension
    #: is measured against this, so a claim is rewarded for using the
    #: employer's own vocabulary — never for acquiring it.
    jd_tokens: frozenset[str] = frozenset()
    #: Tokens from MUST_HAVE / STRONG_PREFERENCE requirements only, weighted
    #: higher than incidental prose.
    required_tokens: frozenset[str] = frozenset()

    intensity: str = "BALANCED"
    archetype_scores: dict[str, int] = field(default_factory=dict)
    signals_matched: tuple[str, ...] = ()

    @property
    def summary_line(self) -> str:
        """One line for logs and diagnostics. Never rendered onto a resume."""
        return f"{self.role_archetype} (base {self.resume_base}, {self.intensity})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "role_family": self.role_family,
            "role_archetype": self.role_archetype,
            "archetype_label": self.archetype_label,
            "resume_base": self.resume_base,
            "primary_competencies": list(self.primary_competencies),
            "secondary_competencies": list(self.secondary_competencies),
            "deemphasize": list(self.deemphasize),
            "domain_terms": list(self.domain_terms),
            "tooling_terms": list(self.tooling_terms),
            "business_context": self.business_context,
            "stakeholder_expectations": list(self.stakeholder_expectations),
            "data_system_expectations": list(self.data_system_expectations),
            "modeling_expectations": list(self.modeling_expectations),
            "top_required_capabilities": list(self.top_required_capabilities),
            "top_optional_capabilities": list(self.top_optional_capabilities),
            "experience_bar": self.experience_bar,
            "location_requirements": list(self.location_requirements),
            "intensity": self.intensity,
            "signals_matched": list(self.signals_matched),
            "coursework_priority": list(self.coursework_priority),
        }


#: Requirement kinds that count as "required" rather than incidental.
_REQUIRED_KINDS = frozenset({"MUST_HAVE", "STRONG_PREFERENCE"})
_OPTIONAL_KINDS = frozenset({"NICE_TO_HAVE"})

#: Wording that marks each expectation bucket. Matching only — these phrases
#: are read out of the posting, never written into a document.
_STAKEHOLDER_MARKERS = (
    "stakeholder",
    "cross-functional",
    "partner team",
    "business team",
    "communicat",
    "present",
    "non-technical",
    "requirements",
    "collaborat",
)
_DATA_SYSTEM_MARKERS = (
    "pipeline",
    "etl",
    "elt",
    "warehouse",
    "data lake",
    "ingestion",
    "schema",
    "data quality",
    "data integrity",
    "transformation",
    "streaming",
    "database",
    "sql",
    "dataset",
)
_MODELING_MARKERS = (
    "model",
    "machine learning",
    "forecast",
    "regression",
    "classif",
    "neural",
    "deep learning",
    "statistical",
    "experiment",
    "a/b test",
)
_LOCATION_MARKERS = (
    "onsite",
    "on-site",
    "hybrid",
    "remote",
    "relocat",
    "in office",
    "in-office",
)


class RoleTargetProfileBuilder:
    """Builds a :class:`RoleTargetProfile` from a job and its requirements.

    Fully deterministic: same posting, same profile. That matters because the
    profile decides what a resume leads with, and a tailoring layer that gave a
    different answer on every run would make the document unreviewable.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_targeting_config()
        self.archetypes: list[dict[str, Any]] = list(self.config.get("archetypes") or [])
        self._by_name = {a["name"]: a for a in self.archetypes}

    # -- public ----------------------------------------------------------
    def build(
        self,
        *,
        title: str,
        description: str,
        role_family: str,
        requirements: list[Any] | None = None,
        intensity: str | None = None,
        location_raw: str | None = None,
        functional_primary: str = "",
    ) -> RoleTargetProfile:
        text = f"{title}\n{description}".lower()
        scores = self._score_archetypes(
            title.lower(), text, role_family, functional_primary=functional_primary
        )
        archetype, matched = self._choose(scores)
        spec = self._by_name.get(archetype, {})

        requirements = requirements or []
        required_texts = [r.text for r in requirements if getattr(r, "kind", "") in _REQUIRED_KINDS]
        optional_texts = [r.text for r in requirements if getattr(r, "kind", "") in _OPTIONAL_KINDS]

        jd_tokens = _tokens(text)
        required_tokens: set[str] = set()
        for requirement in requirements:
            if getattr(requirement, "kind", "") in _REQUIRED_KINDS:
                required_tokens |= _tokens(requirement.text)
                # Keywords go through the same filter as the prose. Without it
                # a requirement contributed "as", "at", "in", "of", "or", "to"
                # to the set the coverage metric is measured against, and
                # coverage then largely reported how much English a document
                # contained.
                required_tokens |= _tokens(
                    " ".join(str(k) for k in (getattr(requirement, "keywords", None) or []))
                )

        resolved_intensity = (intensity or self._default_intensity()).upper()
        if resolved_intensity not in INTENSITY_LEVELS:
            resolved_intensity = "BALANCED"

        return RoleTargetProfile(
            role_family=role_family,
            role_archetype=archetype,
            archetype_label=str(spec.get("label") or archetype.replace("_", " ").title()),
            resume_base=str(spec.get("resume_base") or "DS_ML_AI"),
            primary_competencies=tuple(spec.get("primary_competencies") or ()),
            secondary_competencies=tuple(spec.get("secondary_competencies") or ()),
            deemphasize=tuple(spec.get("deemphasize") or ()),
            deemphasized_categories=frozenset(spec.get("deemphasized_categories") or ()),
            domain_terms=self._matched_terms(text, spec.get("jd_signals") or {}),
            tooling_terms=self._tooling_terms(requirements, jd_tokens),
            business_context=str(spec.get("business_context") or "").strip(),
            stakeholder_expectations=_expectations(requirements, _STAKEHOLDER_MARKERS),
            data_system_expectations=_expectations(requirements, _DATA_SYSTEM_MARKERS),
            modeling_expectations=_expectations(requirements, _MODELING_MARKERS),
            top_required_capabilities=tuple(required_texts[:5]),
            top_optional_capabilities=tuple(optional_texts[:5]),
            required_capabilities=tuple(required_texts),
            experience_bar=self._experience_bar(description, spec),
            location_requirements=self._location_requirements(location_raw, description),
            claim_role_families=tuple(spec.get("claim_role_families") or ()),
            coursework_priority=tuple(spec.get("coursework_priority") or ()),
            jd_tokens=frozenset(jd_tokens),
            required_tokens=frozenset(required_tokens),
            intensity=resolved_intensity,
            archetype_scores=scores,
            signals_matched=matched,
        )

    def for_base(self, base_spec: dict[str, Any]) -> RoleTargetProfile:
        """Position a base resume with NO posting - the Resume Library's build.

        The base's ``library_archetype`` supplies the summary frame, skill
        groups and de-emphasis; the base's own ``claim_role_families`` and
        course order apply. Every job-description field is empty, so the
        requirement and keyword dimensions score zero for every claim and the
        ranking falls to role relevance, evidence strength, X-Y-Z quality and
        the user's standing preferences - the strongest general version of the
        family, built by the same code a job resume is.
        """
        base_name = str(base_spec.get("name") or "")
        archetype = str(base_spec.get("library_archetype") or "")
        spec = self._by_name.get(archetype)
        if spec is None:
            # Fall back to the first archetype that points at this base.
            spec = next(
                (a for a in self.archetypes if a.get("resume_base") == base_name), {}
            )
            archetype = str(spec.get("name") or "")
        return RoleTargetProfile(
            role_family="",
            role_archetype=archetype,
            archetype_label=str(spec.get("label") or archetype.replace("_", " ").title()),
            resume_base=base_name,
            primary_competencies=tuple(spec.get("primary_competencies") or ()),
            secondary_competencies=tuple(spec.get("secondary_competencies") or ()),
            deemphasize=tuple(spec.get("deemphasize") or ()),
            deemphasized_categories=frozenset(spec.get("deemphasized_categories") or ()),
            business_context=str(spec.get("business_context") or "").strip(),
            claim_role_families=tuple(
                base_spec.get("claim_role_families") or spec.get("claim_role_families") or ()
            ),
            coursework_priority=tuple(
                base_spec.get("coursework_priority") or spec.get("coursework_priority") or ()
            ),
            intensity=self._default_intensity(),
        )

    def archetype_labels_for_base(self, base_name: str) -> list[str]:
        """Every archetype that positions postings onto this base, in config order."""
        return [
            str(a.get("label") or a.get("name"))
            for a in self.archetypes
            if a.get("resume_base") == base_name
        ]

    def spec_for(self, archetype: str) -> dict[str, Any]:
        return self._by_name.get(archetype, {})

    def weights(self, intensity: str) -> dict[str, float]:
        levels = (self.config.get("targeting_intensity") or {}).get("levels") or {}
        chosen = levels.get(intensity) or levels.get("BALANCED") or {}
        return {k: float(v) for k, v in chosen.items()}

    # -- internals -------------------------------------------------------
    def _default_intensity(self) -> str:
        return str((self.config.get("targeting_intensity") or {}).get("default") or "BALANCED")

    def _score_archetypes(
        self,
        title: str,
        text: str,
        role_family: str,
        *,
        functional_primary: str = "",
    ) -> dict[str, int]:
        """Weighted signal counting. Title signals dominate by design.

        A title signal is worth up to 6 and the role-family affinity only 2, so
        a posting that says "Analytics Engineer" in its title is positioned as
        analytics engineering even when the classifier labelled it
        DATA_SCIENTIST because of a "Data Science" suffix.

        The functional classification is worth more than either, because it is
        the only one of the three that read the description. Without it a
        "Strategy & Operations Associate" scored nothing anywhere and fell to
        the default base — the exact case this whole pass exists to fix.
        """
        affinity = (self.config.get("role_family_affinity") or {}).get(role_family)
        affinity_weight = int(self.config.get("role_family_affinity_weight") or 2)
        # Deliberately BELOW the maximum title signal (6). The functional hint
        # must be decisive when nothing else scores — that is the hidden-fit
        # case, where the title says "Strategy Associate" and matches no
        # archetype at all — without overriding a posting that says
        # "Data Scientist" in its title. At 7 it did override one.
        functional_weight = int(self.config.get("functional_primary_weight") or 4)

        scores: dict[str, int] = {}
        for spec in self.archetypes:
            name = str(spec["name"])
            score = 0
            for phrase, weight in (spec.get("title_signals") or {}).items():
                if str(phrase).lower() in title:
                    score += int(weight)
            for phrase, weight in (spec.get("jd_signals") or {}).items():
                if str(phrase).lower() in text:
                    score += int(weight)
            if affinity == name:
                score += affinity_weight
            # The functional names reuse the archetype vocabulary where they
            # overlap; the two that do not map through FUNCTION_TO_ARCHETYPE.
            if functional_primary and FUNCTION_TO_ARCHETYPE.get(
                functional_primary, functional_primary
            ) == name:
                score += functional_weight
            scores[name] = score
        return scores

    def _choose(self, scores: dict[str, int]) -> tuple[str, tuple[str, ...]]:
        floor = int(self.config.get("minimum_archetype_score") or 5)
        fallback = str(self.config.get("fallback_archetype") or "DATA_SCIENCE")

        # Ties break on config order, which runs most specific first.
        ordered = [spec["name"] for spec in self.archetypes]
        best = max(ordered, key=lambda name: (scores.get(name, 0), -ordered.index(name)))
        if scores.get(best, 0) < floor:
            return fallback, ()

        spec = self._by_name[best]
        matched = tuple(
            f"{phrase} (+{weight})"
            for phrase, weight in {
                **(spec.get("title_signals") or {}),
                **(spec.get("jd_signals") or {}),
            }.items()
        )
        return best, matched

    def _matched_terms(self, text: str, signals: dict[str, Any]) -> tuple[str, ...]:
        """The archetype's own vocabulary that this posting actually uses."""
        hits = [str(p) for p, _ in sorted(signals.items(), key=lambda kv: -int(kv[1]))]
        return tuple(p for p in hits if p.lower() in text)[:12]

    def _tooling_terms(self, requirements: list[Any], jd_tokens: set[str]) -> tuple[str, ...]:
        """Technology-shaped keywords the posting names, most-cited first.

        Read out of the parsed requirements rather than guessed at, and used
        only for ranking. A tool named here that the Evidence Bank does not
        support is still refused by every downstream gate.
        """
        counts: dict[str, int] = {}
        for requirement in requirements:
            for keyword in getattr(requirement, "keywords", None) or []:
                token = str(keyword).strip().lower()
                if len(token) < 3 or token in _STOPWORDS or token.isdigit():
                    continue
                counts[token] = counts.get(token, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return tuple(term for term, _ in ordered[:25])

    def _experience_bar(self, description: str, spec: dict[str, Any]) -> str:
        match = re.search(
            r"(\d+)\s*[-–to]{1,3}\s*(\d+)\+?\s*years|\b(\d+)\+?\s*years",
            description,
            re.I,
        )
        if match:
            stated = match.group(0).strip()
            return f"Posting states: {stated}."
        return str(spec.get("experience_bar_note") or "Not stated in the posting.")

    def _location_requirements(self, location_raw: str | None, description: str) -> tuple[str, ...]:
        out: list[str] = []
        if location_raw:
            out.append(location_raw.strip()[:200])
        lowered = description.lower()
        for marker in _LOCATION_MARKERS:
            if marker in lowered:
                out.append(marker)
        return tuple(dict.fromkeys(out))[:6]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_profile_for_job(
    session: Any, job: Any, *, intensity: str | None = None
) -> RoleTargetProfile:
    """The profile for one stored job, using its latest parsed requirements.

    Lives here rather than in the API layer so every caller — endpoint, CLI,
    test, audit — positions a job the same way. A posting that has not been
    analyzed yet still gets a profile from its title and description; it simply
    has no requirement-level signal to work with.
    """
    from sqlalchemy import select

    from app.models.analysis import JobRequirement

    requirements: list[Any] = []
    analysis = job.latest_analysis
    if analysis is not None:
        requirements = list(
            session.execute(
                select(JobRequirement)
                .where(JobRequirement.analysis_id == analysis.analysis_id)
                .order_by(JobRequirement.order_index)
            )
            .scalars()
            .all()
        )

    # `role_family` comes from a TITLE pattern list, and for 95% of the corpus
    # it is UNRELATED. The functional classification read the description, so
    # when it has an opinion it is the better one — a "Strategy Associate" whose
    # JD is SQL, experimentation and product metrics should be positioned as
    # PRODUCT_ANALYTICS, not fall back to a generic base because the title says
    # "Strategy". D-46.
    #
    # This is still ranking only. Nothing here can put a skill or a claim on a
    # document; it decides which of the candidate's real material leads.
    # `role_family` is still passed through unchanged: it feeds the
    # role_family_affinity lookup, whose keys are role families. Replacing it
    # with the function name silently disabled that lookup and re-positioned a
    # "Data Scientist" posting as analytics engineering. The functional
    # classification is an ADDITIONAL signal, not a substitute.
    return RoleTargetProfileBuilder().build(
        title=job.title or "",
        description=job.description or "",
        role_family=job.role_family or "",
        requirements=requirements,
        intensity=intensity,
        location_raw=job.location_raw,
        functional_primary=job.functional_primary or "",
    )


def meaningful_tokens(text: str) -> set[str]:
    """Words that carry positioning signal, with the stopword list applied.

    Public because outreach drafting ranks Evidence Bank claims against a
    posting the same way targeting does. Both are RANKING; neither can add
    anything the Bank has not already cleared.
    """
    return {
        word for word in _WORD_RE.findall(text.lower()) if len(word) > 2 and word not in _STOPWORDS
    }


#: Internal alias, kept so the existing call sites read as they did.
_tokens = meaningful_tokens


def _expectations(requirements: list[Any], markers: tuple[str, ...]) -> tuple[str, ...]:
    """Requirement lines that fall into one expectation bucket, verbatim."""
    out: list[str] = []
    for requirement in requirements:
        text = str(getattr(requirement, "text", "") or "")
        lowered = text.lower()
        if any(marker in lowered for marker in markers):
            trimmed = text.strip()
            if trimmed and trimmed not in out:
                out.append(trimmed[:220])
    return tuple(out[:5])
