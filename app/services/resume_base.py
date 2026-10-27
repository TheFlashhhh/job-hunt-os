"""Builds a COMPLETE base resume for a role family, before any job tailoring.

The defect this exists to fix: the generator treated accepted tailoring
recommendations as the entire document, so accepting two recommendations
produced a two-bullet page with half of it blank. Recommendations are patches.
This module produces the thing they patch.

**Selection, never authorship.** Every bullet is the verbatim `text` of a safe
claim from the Evidence Bank. Every organization, title, location and date
comes from `experiences.yaml` / `projects.yaml`. Nothing here writes resume
prose, so nothing here can invent a credential.

**Ownership is resolved per claim, not per recommendation.** A safe claim maps
to exactly one organization; a recommendation may cite evidence from several.
Grouping by the recommendation's flattened evidence list is what rendered the
OpenText SQL bullet under both OpenText and CHAPA.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.config import REPO_ROOT
from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.services.candidate_profile import load_candidate_profile
from app.services.resume_targeting import (
    BulletScore,
    ResumeTargetingEngine,
    TargetingExplanation,
)
from app.services.role_targeting import RoleTargetProfile, RoleTargetProfileBuilder

BASES_PATH = REPO_ROOT / "data" / "config" / "resume_bases.yaml"

#: Best first. Used to rank claims competing for a limited bullet budget.
_STRENGTH_RANK = {"STRONG": 0, "SOLID": 1, "MODEST": 2}


@dataclass
class ResumeBullet:
    """One rendered line, and the single entry that owns it."""

    text: str
    claim_id: str
    #: The organization or project this bullet belongs under. Exactly one.
    owner: str
    strength: str = "SOLID"
    #: True when personal ownership is unresolved and the project-level
    #: alternative was rendered instead of a first-person claim.
    ownership_downgraded: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    #: Job-specific utility, present only on a targeted build. Diagnostics for
    #: the review screen and the audit — never rendered onto the document.
    score: BulletScore | None = None


@dataclass
class ResumeEntry:
    """An experience or project block: heading, context line, bullets."""

    key: str
    organization: str
    title: str | None = None
    location: str | None = None
    dates: str | None = None
    context: str | None = None
    kind: str = "EXPERIENCE"
    bullets: list[ResumeBullet] = field(default_factory=list)
    #: A project's PUBLIC repository, rendered as a link. Only a public
    #: https://github.com/ URL from projects.yaml reaches here - never a local
    #: path or a private remote (``public_repository_url``).
    url: str | None = None

    @property
    def heading(self) -> str:
        return self.organization

    @property
    def subheading(self) -> str:
        """Title · location · dates — whichever the Evidence Bank actually has.

        A heading with no title or dates reads as a section label rather than a
        job, which is what "OPENTEXT" alone looked like on the broken output.
        """
        parts = [p for p in (self.title, self.location, self.dates) if p]
        return " | ".join(parts)


@dataclass
class ResumeEducation:
    """One school, with its parts kept separate.

    The renderer sets institution against dates and degree against location on
    two aligned rows, so it needs the fields individually rather than
    pre-joined into a sentence.
    """

    institution: str
    degree: str
    location: str = ""
    dates: str = ""
    gpa: str = ""
    coursework: str = ""

    @property
    def degree_line(self) -> str:
        return f"{self.degree}, GPA: {self.gpa}" if self.gpa else self.degree


@dataclass
class SkillGroup:
    label: str
    skills: list[str]
    #: Why each skill is here (resume_targeting SKILL_* tiers, D-70). Page
    #: fitting reads it; nothing renders it. Empty means "core, unexplained".
    tiers: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        return f"{self.label}: {', '.join(self.skills)}"


@dataclass
class BaseResume:
    name: str
    location: str
    contact_line: str
    education_lines: list[str]
    entries: list[ResumeEntry]
    skill_groups: list[SkillGroup]
    base_name: str
    #: Structured form of the same schools education_lines describes.
    education: list[ResumeEducation] = field(default_factory=list)
    #: 2-3 compact lines. Part of the BASE, not of tailoring.
    summary: str = ""
    #: Present only when built against a RoleTargetProfile. Feeds the "why this
    #: resume looks different" panel and the targeting audit.
    targeting: TargetingExplanation | None = None
    role_archetype: str = ""

    @property
    def total_bullets(self) -> int:
        return sum(len(e.bullets) for e in self.entries)

    @property
    def experiences(self) -> list[ResumeEntry]:
        return [e for e in self.entries if e.kind == "EXPERIENCE"]

    @property
    def projects(self) -> list[ResumeEntry]:
        return [e for e in self.entries if e.kind == "PROJECT"]

    def entry_for_owner(self, owner: str) -> ResumeEntry | None:
        for entry in self.entries:
            if entry.organization == owner:
                return entry
        return None


def load_base_config() -> dict[str, Any]:
    return yaml.safe_load(BASES_PATH.read_text(encoding="utf-8")) or {}


class ResumeBaseBuilder:
    """Assembles a complete, submittable base resume from the Evidence Bank."""

    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        self.bank = bank or get_evidence_bank()
        self.config = load_base_config()
        self.layout = self.config.get("layout", {})

    # -- public ----------------------------------------------------------
    def build(self, base_name: str, *, target: RoleTargetProfile | None = None) -> BaseResume:
        """A complete base resume, optionally positioned for one posting.

        Without ``target`` this is the untargeted base: the role family's own
        ordering, unchanged. With one, the same Evidence-Bank material is
        selected, ordered and grouped for that specific job — a ranking layer
        on top of the safety pipeline, never in place of it.
        """
        spec = self._spec(base_name)
        candidate = self.bank.candidate

        # A targeted build uses the winning archetype's course order; an
        # untargeted one uses the base's. Either way the list only ORDERS
        # courses the Evidence Bank already holds.
        coursework_priority: Sequence[str] = spec.get("coursework_priority") or ()
        if target is not None and target.coursework_priority:
            coursework_priority = target.coursework_priority

        if target is not None:
            entries, engine, explanation = self._targeted_entries(spec, target)
            summary = engine.summary(
                degree_short=self._degree_short(),
                institution=self._institution(),
                aliases=self.config.get("summary_skill_aliases") or {},
            ) or self._summary(spec)
            skill_groups = _tiered_groups(engine) or self._skill_groups(spec)
        else:
            entries = self._untargeted_entries(spec)
            summary = self._summary(spec)
            skill_groups = self._skill_groups(spec)
            explanation = None

        return BaseResume(
            name=candidate.get("name", ""),
            location=candidate.get("location", ""),
            contact_line=self._contact_line(candidate),
            education_lines=self._education_lines(),
            education=self._education(coursework_priority),
            summary=summary,
            entries=entries,
            skill_groups=skill_groups,
            base_name=base_name,
            targeting=explanation,
            role_archetype=target.role_archetype if target else "",
        )

    def _untargeted_entries(self, spec: dict[str, Any]) -> list[ResumeEntry]:
        entries: list[ResumeEntry] = []
        used_claims: set[str] = set()
        budget = int(self.layout.get("max_total_bullets", 14))
        per_entry = int(self.layout.get("max_bullets_per_entry", 3))

        experience_ids = sorted(spec.get("experiences") or [], key=self._start_date, reverse=True)
        for experience_id in experience_ids:
            entry = self._experience_entry(experience_id)
            if entry is None:
                continue
            depth = (self._depth_config().get(experience_id) or {}).get("default") or {}
            entry.bullets = self._bullets_for(
                entry.organization, spec, used_claims, limit=int(depth.get("max", per_entry))
            )
            if entry.bullets:
                entries.append(entry)

        projects: list[ResumeEntry] = []
        for project_id in spec.get("projects") or []:
            entry = self._project_entry(project_id)
            if entry is None:
                continue
            entry.bullets = self._bullets_for(
                entry.organization, spec, used_claims, limit=per_entry
            )
            if entry.bullets:
                projects.append(entry)

        # Untargeted, the family's declared project order IS the preference,
        # so the cap keeps the leading ones.
        entries.extend(self._cap_projects(entries, projects))
        self._trim_to_budget(entries, budget)
        return entries

    # -- targeted selection ----------------------------------------------
    def _targeted_entries(
        self, spec: dict[str, Any], target: RoleTargetProfile
    ) -> tuple[list[ResumeEntry], ResumeTargetingEngine, TargetingExplanation]:
        """Select and order bullets by JOB VALUE rather than family membership.

        The budget is allocated in two passes, and the two-pass shape is the
        point of §11: every entry first earns a floor so no real internship
        vanishes, then the REMAINING room goes to the highest-scoring lines
        wherever they live. That is what lets the two most relevant entries
        carry four bullets each while a less relevant one carries two, instead
        of every entry getting a flat three regardless of the job.
        """
        engine = ResumeTargetingEngine(self.bank, target)

        # Every experience is CORE: paid work holds its slot on every page and
        # never competes with a project for one (D-71). What varies by role is
        # how many lines an experience carries, not whether it appears.
        entries: list[ResumeEntry] = []
        for experience_id in spec.get("experiences") or []:
            entry = self._experience_entry(experience_id)
            if entry is not None:
                entries.append(entry)
        project_entries: list[ResumeEntry] = []
        for project_id in spec.get("projects") or []:
            entry = self._project_entry(project_id)
            if entry is not None:
                project_entries.append(entry)

        recency = self._recency_by_owner([*entries, *project_entries])
        pools: dict[str, list[BulletScore]] = {}
        for entry in [*entries, *project_entries]:
            pools[entry.organization] = self._score_pool(
                entry.organization, engine, recency.get(entry.organization, 0.5)
            )

        # Decide WHICH projects compete before spending the budget on them.
        project_entries = self._shortlist_projects(
            project_entries, pools, reserved=len(entries)
        )
        surviving = {e.organization for e in [*entries, *project_entries]}
        pools = {owner: pool for owner, pool in pools.items() if owner in surviving}

        floors: dict[str, int] = {}
        caps: dict[str, int] = {}
        for entry in entries:
            depth = self._experience_depth(entry.key, target.role_archetype, pools[entry.organization])
            if depth is not None:
                floors[entry.organization], caps[entry.organization] = depth
        chosen = self._allocate(pools, engine, recency, floors=floors, caps=caps)

        # Attach, then drop entries with nothing to say.
        for entry in [*entries, *project_entries]:
            entry.bullets = [
                ResumeBullet(
                    text=score.text,
                    claim_id=score.claim_id,
                    owner=entry.organization,
                    strength=score.strength,
                    ownership_downgraded=score.ownership_downgraded,
                    evidence_ids=list(
                        getattr(self.bank.safe_claims.get(score.claim_id), "evidence_ids", ())
                    ),
                    score=score,
                )
                for score in chosen.get(entry.organization, [])
            ]

        experiences = [e for e in entries if e.bullets]
        projects = [e for e in project_entries if e.bullets]

        # Experience stays reverse-chronological — a shuffled work history
        # reads as an error. Projects are ordered by how well they answer THIS
        # posting, which is §10's "actual relevance scoring".
        experiences.sort(key=lambda e: self._start_date(e.key), reverse=True)
        projects.sort(key=lambda e: -_entry_value(e))
        # Targeted, the cap keeps the projects that best answer THIS posting.
        projects = self._cap_projects(experiences, projects)

        selected = [b.score for e in [*experiences, *projects] for b in e.bullets if b.score]
        skill_groups = engine.skill_groups()
        explanation = engine.explain(
            selected=selected,
            skill_groups=skill_groups,
            lead_projects=[e.organization for e in projects],
        )
        # Say how deep a role-dependent experience went, so a one-line entry
        # reads as a decision rather than as something the page lost.
        for entry in experiences:
            if entry.key in self._depth_config():
                record = self.bank.experiences.get(entry.key) or {}
                count = len(entry.bullets)
                explanation.notes.append(
                    f"{record.get('title', '')} ({entry.organization}) carries "
                    f"{count} line{'s' if count != 1 else ''} for this role."
                )
        return [*experiences, *projects], engine, explanation

    def _depth_config(self) -> dict[str, Any]:
        return self.config.get("experience_bullet_depth") or {}

    def _experience_depth(
        self, experience_id: str, archetype: str, pool: list[BulletScore]
    ) -> tuple[int, int] | None:
        """(floor, cap) for an experience whose depth depends on the role, or
        None for one that takes the layout's usual floor and cap.

        ``engaged_max`` lifts the cap when the posting itself pulls on the
        entry's material - a backend posting that asks for LLM evaluation earns
        the teaching assistantship a second bullet; an ordinary one does not.
        Measured as the strongest requirement/keyword match across the entry's
        lines and read from scores, never from the posting's text directly, so
        it tracks the same signal the rest of the ranking uses.
        """
        spec = self._depth_config().get(experience_id)
        if not spec:
            return None
        depth = {**(spec.get("default") or {}), **((spec.get("archetypes") or {}).get(archetype) or {})}
        floor = int(depth.get("min", 1))
        cap = max(floor, int(depth.get("max", floor)))
        engaged_max = int(depth.get("engaged_max", floor))
        if engaged_max > floor and len(pool) > floor:
            engagement = max(
                (
                    max(s.dimensions.get("requirement_match", 0.0), s.dimensions.get("keyword_alignment", 0.0))
                    for s in pool
                ),
                default=0.0,
            )
            if engagement >= float(spec.get("engagement_threshold", 0.3)):
                # The posting asked for this work, so the extra line is owed,
                # not merely allowed: it must not lose to a project's third.
                floor = cap = max(cap, engaged_max)
        return floor, cap

    def _score_pool(
        self, owner: str, engine: ResumeTargetingEngine, recency: float
    ) -> list[BulletScore]:
        """Every usable safe claim this entry owns, best first."""
        pool: list[BulletScore] = []
        for claim in self.bank.safe_claims.values():
            if self._claim_owner(claim.claim_id) != owner:
                continue
            text, downgraded = self._claim_text(claim)
            if not text:
                continue
            pool.append(
                engine.score_claim(
                    claim, owner=owner, text=text, downgraded=downgraded, recency=recency
                )
            )
        pool.sort(key=lambda s: -s.total)
        return pool

    def _allocate(
        self,
        pools: dict[str, list[BulletScore]],
        engine: ResumeTargetingEngine,
        recency: dict[str, float],
        *,
        floors: dict[str, int] | None = None,
        caps: dict[str, int] | None = None,
    ) -> dict[str, list[BulletScore]]:
        """Spend the bullet budget. ``floors``/``caps`` override the layout's
        per-entry floor and targeted cap for the owners they name."""
        budget = int(self.layout.get("max_total_bullets", 15))
        floor = int(self.layout.get("min_bullets_per_entry", 2))
        cap = int(self.layout.get("max_bullets_per_entry_targeted", 4))
        floors = floors or {}
        caps = caps or {}

        chosen: dict[str, list[BulletScore]] = {owner: [] for owner in pools}
        covered: set[str] = set()
        used: set[str] = set()
        spent = 0

        def take(owner: str) -> bool:
            """Pull this entry's best remaining claim, rescored against what
            has already been said. Rescoring matters: uniqueness is the only
            dimension that depends on the selections around it."""
            nonlocal spent
            best: BulletScore | None = None
            for candidate in pools[owner]:
                if candidate.claim_id in used:
                    continue
                rescored = engine.score_claim(
                    self.bank.safe_claims[candidate.claim_id],
                    owner=owner,
                    text=candidate.text,
                    downgraded=candidate.ownership_downgraded,
                    recency=recency.get(owner, 0.5),
                    covered_tokens=covered,
                )
                if best is None or rescored.total > best.total:
                    best = rescored
            if best is None:
                return False
            chosen[owner].append(best)
            used.add(best.claim_id)
            covered.update(w.lower() for w in best.text.split())
            spent += 1
            return True

        # Pass 1 — every entry earns its floor.
        for owner in pools:
            for _ in range(floors.get(owner, floor)):
                if spent >= budget or not take(owner):
                    break

        # Pass 2 — the rest goes wherever the next line is worth most.
        while spent < budget:
            best_owner: str | None = None
            best_value = float("-inf")
            for owner, taken in chosen.items():
                if len(taken) >= caps.get(owner, cap):
                    continue
                remaining = [c for c in pools[owner] if c.claim_id not in used]
                if not remaining:
                    continue
                if remaining[0].total > best_value:
                    best_value = remaining[0].total
                    best_owner = owner
            if best_owner is None or not take(best_owner):
                break

        return chosen

    def _recency_by_owner(self, entries: list[ResumeEntry]) -> dict[str, float]:
        """Newest entry 1.0, oldest 0.2, linear on the year in its date string.

        Deliberately coarse. Recency is a tiebreak between comparably relevant
        lines, not a ranking of its own — an older internship with directly
        matching evidence must still beat a recent project that does not match.
        """
        years: dict[str, int] = {}
        for entry in entries:
            match = re.findall(r"(19|20)\d{2}", entry.dates or "")
            if match:
                years[entry.organization] = max(
                    int(y) for y in re.findall(r"((?:19|20)\d{2})", entry.dates or "")
                )
        if not years:
            return {}
        newest, oldest = max(years.values()), min(years.values())
        span = max(1, newest - oldest)
        return {
            owner: round(0.2 + 0.8 * ((year - oldest) / span), 3) for owner, year in years.items()
        }

    def _degree_short(self) -> str:
        education = self._education()
        if not education:
            return ""
        short = re.sub(r"^Master of Science in\s+", "MS ", education[0].degree)
        return re.sub(r"^Bachelor of Technology in\s+", "BTech ", short)

    def _institution(self) -> str:
        education = self._education()
        return education[0].institution if education else ""

    def available_bases(self) -> list[str]:
        return [b["name"] for b in self.config.get("bases", [])]

    # -- internals -------------------------------------------------------
    def _spec(self, base_name: str) -> dict[str, Any]:
        for base in self.config.get("bases", []):
            if base.get("name") == base_name:
                return base
        # An unknown family must not yield an empty document.
        bases = self.config.get("bases", [])
        if not bases:
            raise ValueError("resume_bases.yaml defines no bases.")
        return bases[0]

    def _contact_line(self, candidate: dict[str, Any]) -> str:
        """Delegated to `candidate_profile`, which owns the header everywhere.

        This used to format the line itself. So did the DOCX renderer, and so
        did the API. One wrong LinkedIn URL in the Bank then had to be fixed in
        several places at once — see D-43.
        """
        return load_candidate_profile(self.bank).contact_line

    def _education_lines(self) -> list[str]:
        lines: list[str] = []
        for edu in self.bank.education:
            degree = edu.get("degree", "")
            institution = edu.get("institution", "")
            location = edu.get("location", "")
            line = f"{degree}, {institution}"
            if location:
                line += f" ({location})"
            details = []
            if edu.get("gpa"):
                details.append(f"GPA {edu['gpa']}")
            dates = edu.get("academic_completion") or edu.get("end_date_expected")
            if dates:
                details.append(str(dates))
            if details:
                line += " — " + ", ".join(details)
            lines.append(line)
        return lines

    def _course_labels(self, education_id: str) -> list[str]:
        """Every course this school may print, in Evidence Bank order.

        Courses are canonical in ``evidence.yaml`` — one coursework evidence item
        per school, carrying official titles, course codes and the aliases that
        are permitted when space is tight. They are deliberately NOT duplicated
        into ``candidate.yaml``: a second copy is how a resume and its Evidence
        Bank start disagreeing, which is the one failure this whole layer exists
        to prevent.

        A school whose coursework is marked ``CONDITIONAL_SPACE_PERMITTING``
        yields nothing. On a one-page resume that space does not exist, and ten
        undergraduate course titles under a graduate degree crowd out work that
        actually earns interviews.
        """
        for record in self.bank.evidence.values():
            raw = record.raw
            if raw.get("category") != "coursework":
                continue
            if raw.get("education_id") != education_id:
                continue
            if not record.resume_ready:
                continue
            if raw.get("resume_use") == "CONDITIONAL_SPACE_PERMITTING":
                continue
            names: list[str] = []
            for course in raw.get("courses") or []:
                alias = str(course.get("informal_alias") or "").strip()
                # An alias is used only where the Bank explicitly permits one.
                label = alias if (alias and course.get("alias_permitted_when")) else ""
                label = label or str(course.get("name") or "").strip()
                if label:
                    names.append(label)
            return names
        return []

    def _coursework_for(self, education_id: str, priority: Sequence[str] = ()) -> str:
        """The Relevant Coursework line for one school, ordered for this target.

        ``priority`` names courses best-first — from the winning archetype on a
        targeted build, from the base otherwise. It is **ordering only**. Each
        name is resolved back to a course the Evidence Bank already carries, and
        a name the Bank does not carry is dropped rather than printed: a posting
        that asks for a subject the candidate never took must not be able to reach this
        line, and the Bank's own canonical title (or its explicitly permitted
        alias) is always what renders.

        Courses the priority list omits keep their Bank order behind the ones it
        names, so a stale config loses ranking quality and never truth. The line
        is then capped — seven titles wrap onto a second row and cost a bullet,
        and a course list that names everything ranks nothing.
        """
        labels = self._course_labels(education_id)
        if not labels:
            return ""

        by_norm = {_norm(label): label for label in labels}
        ordered: list[str] = []
        for wanted in priority:
            match = by_norm.get(_norm(str(wanted)))
            if match is not None and match not in ordered:
                ordered.append(match)
        ordered.extend(label for label in labels if label not in ordered)

        cap = int(self.layout.get("max_coursework_items", 5))
        if cap > 0:
            ordered = ordered[:cap]
        return ", ".join(ordered)

    def _education(self, priority: Sequence[str] = ()) -> list[ResumeEducation]:
        """Structured schools, newest first, from candidate.yaml + the Bank."""
        out: list[ResumeEducation] = []
        for edu in self.bank.education:
            start = _year(edu.get("start_date"))
            end_raw = edu.get("end_date") or edu.get("end_date_expected")
            end = _year(end_raw)
            # An expected date is labelled as expected. Presenting a future
            # conferral as though it had already happened would be a false
            # claim on the one document an employer reads most closely.
            if not edu.get("end_date") and edu.get("end_date_expected"):
                end = f"Expected {end}"
            dates = " - ".join(p for p in (start, end) if p)
            out.append(
                ResumeEducation(
                    institution=str(edu.get("institution") or ""),
                    degree=str(edu.get("degree") or ""),
                    location=str(edu.get("location") or ""),
                    dates=dates,
                    gpa=str(edu.get("gpa") or ""),
                    coursework=self._coursework_for(
                        str(edu.get("education_id") or ""), priority
                    ),
                )
            )
        return out

    def _summary(self, spec: dict[str, Any]) -> str:
        """A 2-3 line professional summary for this role family.

        The sentence FRAME lives in resume_bases.yaml — human-authored,
        reviewed config, exactly like the entry selection in the same file.
        Every FACT substituted into it comes from the Evidence Bank: the degree
        and institution from candidate.yaml, the skill list from skills.yaml
        filtered to resume-usable levels.

        Nothing here generates a claim. A frame that reads "experienced in
        {skills}" can only ever name skills the Bank already cleared, so the
        JD cannot introduce a capability the candidate does not have.
        """
        template = str(spec.get("summary_template") or "").strip()
        if not template:
            return ""

        education = self._education()
        first = education[0] if education else None
        skills = self._summary_skills(spec)
        if not skills:
            return ""

        # "Master of Science in Data Science" -> "MS Data Science"
        degree_short = ""
        if first:
            degree_short = re.sub(r"^Master of Science in\s+", "MS ", first.degree)
            degree_short = re.sub(r"^Bachelor of Technology in\s+", "BTech ", degree_short)

        return template.format(
            degree_short=degree_short,
            institution=first.institution if first else "",
            skills=", ".join(skills),
        ).strip()

    def _summary_skills(self, spec: dict[str, Any]) -> list[str]:
        """Resume-usable skills this family leads with, strongest first."""
        wanted = list(spec.get("summary_skills") or [])
        aliases = self.config.get("summary_skill_aliases") or {}
        out: list[str] = []
        for name in wanted:
            skill = self.bank.get_skill_by_name(name)
            # A skill the Bank does not clear for a resume never reaches the
            # summary, however prominently the JD asks for it. The alias is
            # applied only AFTER that gate, and only to shorten the display.
            if skill is None or not skill.resume_usable:
                continue
            display = str(aliases.get(skill.name, skill.name))
            if display not in out:
                out.append(display)
        return out

    def _experience_entry(self, experience_id: str) -> ResumeEntry | None:
        record = self.bank.experiences.get(experience_id)
        if record is None:
            return None
        return ResumeEntry(
            key=experience_id,
            organization=record.get("organization", ""),
            # title_immutable exists because renaming this role is prohibited.
            title=record.get("title"),
            location=record.get("location"),
            dates=_date_range(record.get("start_date"), record.get("end_date")),
            kind="EXPERIENCE",
        )

    def _start_date(self, experience_id: str) -> str:
        """``YYYY-MM`` start of an experience; sorts chronologically as text."""
        record = self.bank.experiences.get(experience_id) or {}
        return str(record.get("start_date") or "")

    def _project_entry(self, project_id: str) -> ResumeEntry | None:
        record = self.bank.projects.get(project_id)
        if record is None:
            return None
        return ResumeEntry(
            key=project_id,
            organization=record.get("name", ""),
            title=record.get("subtitle") or record.get("project_type"),
            context=record.get("context"),
            dates=record.get("term"),
            kind="PROJECT",
            url=_public_url(record.get("public_repository_url")),
        )

    def _bullets_for(
        self,
        owner: str,
        spec: dict[str, Any],
        used_claims: set[str],
        *,
        limit: int,
    ) -> list[ResumeBullet]:
        """The strongest role-relevant safe claims owned by this entry."""
        wanted = list(spec.get("claim_role_families") or [])
        candidates: list[tuple[int, int, ResumeBullet]] = []

        for claim in self.bank.safe_claims.values():
            if claim.claim_id in used_claims:
                continue
            if self._claim_owner(claim.claim_id) != owner:
                continue

            # Rank by best matching role family, then by claim strength.
            family_rank = len(wanted)
            for index, family in enumerate(wanted):
                if family in claim.role_families:
                    family_rank = index
                    break
            if family_rank == len(wanted) and wanted:
                # Not relevant to this base. Kept as a ranked fallback rather
                # than discarded: dropping every claim an entry owns removes a
                # real internship from the resume, which reads worse than
                # carrying one less-targeted line for it.
                family_rank = len(wanted)

            text, downgraded = self._claim_text(claim)
            if not text:
                continue
            bullet = ResumeBullet(
                text=text,
                claim_id=claim.claim_id,
                owner=owner,
                strength=claim.strength,
                ownership_downgraded=downgraded,
                evidence_ids=list(claim.evidence_ids),
            )
            candidates.append((family_rank, _STRENGTH_RANK.get(claim.strength, 3), bullet))

        candidates.sort(key=lambda item: (item[0], item[1]))
        chosen = [bullet for _, _, bullet in candidates[:limit]]
        for bullet in chosen:
            used_claims.add(bullet.claim_id)
        return chosen

    def _claim_owner(self, claim_id: str) -> str | None:
        """The single entry a claim belongs under.

        Resolved from the claim's own evidence, which in this Evidence Bank
        maps to exactly one organization. Recommendations cite evidence from
        several organizations, which is why ownership must not be read from
        them.
        """
        claim = self.bank.safe_claims.get(claim_id)
        if claim is None:
            return None
        for evidence_id in claim.evidence_ids:
            record = self.bank.get_evidence(evidence_id)
            if record is not None and record.organization_or_project:
                return record.organization_or_project
        return None

    def _claim_text(self, claim: Any) -> tuple[str, bool]:
        """Verbatim claim text, downgraded when ownership is unresolved."""
        if claim.gated:
            alternative = (claim.project_level_alternative or "").strip()
            if alternative:
                return alternative, True
            # No safe wording available — omit rather than overclaim.
            return "", False
        return (claim.text or "").strip(), False

    def _shortlist_projects(
        self,
        projects: list[ResumeEntry],
        pools: dict[str, list[BulletScore]],
        *,
        reserved: int,
    ) -> list[ResumeEntry]:
        """Which projects get to compete for the bullet budget at all.

        The entry cap drops projects at the END; the budget is allocated at the
        start. Once the candidate pool grows past the cap, the floor pass spends
        bullets on entries that are about to be discarded, and WHICH ones get
        starved depends on their order in `resume_bases.yaml` rather than on the
        posting. A simulation-heavy job description could lose the simulation
        project purely for being listed last.

        So the shortlist runs first, ranked on what each entry is GUARANTEED to
        carry — the sum of its best `min_bullets_per_entry` scores — rather than
        on its single best line. An entry with one brilliant bullet and nothing
        behind it should not take a header's worth of page from one with two
        strong ones.
        """
        cap = int(self.layout.get("max_entries", 0) or 0)
        if cap <= 0:
            return projects
        room = max(cap - reserved, 0)
        if len(projects) <= room:
            return projects
        floor = max(1, int(self.layout.get("min_bullets_per_entry", 2)))

        def potential(entry: ResumeEntry) -> float:
            return sum(score.total for score in pools.get(entry.organization, [])[:floor])

        return sorted(projects, key=lambda e: -potential(e))[:room]

    def _cap_projects(
        self, experiences: list[ResumeEntry], projects: list[ResumeEntry]
    ) -> list[ResumeEntry]:
        """Cap how many ENTRIES reach the page, dropping the weakest projects.

        The bullet budget alone cannot hold one page. Each entry also costs two
        header rows and its spacing, so growing the candidate pool — as adding
        Delta3 and Reading the Game did — pushes a 15-bullet resume onto a
        second page even though the bullet count never moved.

        Only projects are ever dropped. A real internship disappearing because
        a class project scored well would be a worse resume, not a shorter one.
        """
        cap = int(self.layout.get("max_entries", 0) or 0)
        if cap <= 0:
            return projects
        room = max(cap - len(experiences), 0)
        return projects[:room]

    def _trim_to_budget(self, entries: list[ResumeEntry], budget: int) -> None:
        """Drop the weakest trailing bullets until the page fits.

        Entries are never emptied: an entry with no bullets is a heading with
        nothing under it, which reads worse than a shorter resume.
        """
        while sum(len(e.bullets) for e in entries) > budget:
            trimmable = [e for e in entries if len(e.bullets) > 1]
            if not trimmable:
                return
            weakest = max(
                trimmable,
                key=lambda e: _STRENGTH_RANK.get(e.bullets[-1].strength, 3),
            )
            weakest.bullets.pop()

    def _skill_groups(self, spec: dict[str, Any]) -> list[SkillGroup]:
        """The family's relevant safe skill set, grouped, with no posting.

        The same selection a Resume Library base gets: the base's library
        archetype decides which cleared skills are relevant to the family and
        in what order (D-69), so an untargeted build cannot list every skill
        in the Bank - nor drop a relevant one to a count cap.
        """
        engine = ResumeTargetingEngine(self.bank, RoleTargetProfileBuilder().for_base(spec))
        return _tiered_groups(engine) or self._category_skill_groups(spec)

    def _category_skill_groups(self, spec: dict[str, Any]) -> list[SkillGroup]:
        """Every resume-usable skill in the base's own categories, grouped.

        Reached only for a base whose archetype defines no skill groups. Starts
        from every resume-usable skill in the Evidence Bank rather than only
        those a JD happened to mention — a resume listing "Python, SQL"
        because those were the matched keywords is not a resume.
        """
        groups: list[SkillGroup] = []
        claimed: set[str] = set()

        for group_spec in spec.get("skill_groups") or []:
            wanted = set(group_spec.get("categories") or [])
            names: list[str] = []
            for skill in self.bank.skills.values():
                if not skill.resume_usable or skill.name in claimed:
                    continue
                if wanted & set(skill.categories):
                    names.append(skill.name)
            if not names:
                continue
            # Strongest evidence first, then alphabetical for stability.
            names.sort(
                key=lambda n: (
                    0
                    if self.bank.skills_by_name[_norm(n)].evidence_level == "WORK_PROJECT_VERIFIED"
                    else 1,
                    n.lower(),
                )
            )
            claimed.update(names)
            groups.append(SkillGroup(label=group_spec.get("label", "Skills"), skills=names))
        return groups


def _tiered_groups(engine: ResumeTargetingEngine) -> list[SkillGroup]:
    """The engine's skill groups, each carrying why every skill qualified."""
    groups = engine.skill_groups()
    return [
        SkillGroup(
            label=label,
            skills=names,
            tiers={name: engine.skill_tiers.get(name, "") for name in names},
        )
        for label, names in groups
    ]


#: The only links a project heading may carry: public GitHub repositories.
_PUBLIC_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/?$")


def _public_url(value: Any) -> str | None:
    """The project's public repository URL, or None.

    Anything that is not a plain https://github.com/<owner>/<repo> URL - a
    local path, a file:// URL, an SSH remote, a deep link into a private fork -
    is dropped rather than printed onto an applicant-facing page.
    """
    text = str(value or "").strip()
    return text.rstrip("/") if _PUBLIC_REPO_RE.match(text) else None


def _entry_value(entry: ResumeEntry) -> float:
    """How well this entry answers the posting, for project ordering.

    The SUM of its selected bullet scores rather than the best one: an entry
    carrying three strongly matching lines should outrank one carrying a single
    slightly better line, because it will occupy more of the page.
    """
    return sum(b.score.total for b in entry.bullets if b.score)


def _norm(name: str) -> str:
    return name.strip().lower()


def _date_range(start: str | None, end: str | None) -> str | None:
    if start and end:
        return f"{_pretty(start)} – {_pretty(end)}"
    return _pretty(start) or _pretty(end)


_MONTHS = {
    "01": "Jan",
    "02": "Feb",
    "03": "Mar",
    "04": "Apr",
    "05": "May",
    "06": "Jun",
    "07": "Jul",
    "08": "Aug",
    "09": "Sep",
    "10": "Oct",
    "11": "Nov",
    "12": "Dec",
}


def _pretty(value: str | None) -> str | None:
    """2023-09 -> Sep 2023, PRESENT -> Present. Anything else passes through."""
    if not value:
        return None
    text = str(value)
    if text.upper() == "PRESENT":
        return "Present"
    if len(text) == 7 and text[4] == "-" and text[5:7] in _MONTHS:
        return f"{_MONTHS[text[5:7]]} {text[:4]}"
    return text


def _year(value: Any) -> str:
    """'2025-09' -> 'Sep 2025'. Returns the input unchanged if unparseable."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})$", text)
    if not match:
        return text
    months = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    year, month = match.group(1), int(match.group(2))
    return f"{months[month - 1]} {year}" if 1 <= month <= 12 else year
