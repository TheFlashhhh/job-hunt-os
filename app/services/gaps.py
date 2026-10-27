"""Skill-gap baseline.

Aggregates what the jobs the candidate *actually pursued* keep asking for and the
Evidence Bank cannot support.

The population matters more than the arithmetic. Counting every discovered job
would measure the job market; counting only jobs that reached SHORTLISTED or
APPLIED measures the market the candidate is choosing to compete in, which is the only
one worth reorganising a semester around. Anything below SHORTLISTED is a
posting he looked at, not a decision he made.

This module produces evidence for a later decision. It deliberately does not
create learning tasks, recommend courses, or set goals — turning an observation
into an obligation is the user's call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.analysis import EvidenceMatch, JobRequirement
from app.models.crm import Application
from app.models.enums import ApplicationStatus, RequirementKind, SupportStatus
from app.models.ingestion import Job

#: The only application states that count toward the baseline.
COUNTED_STATUSES: frozenset[str] = frozenset(
    {ApplicationStatus.SHORTLISTED.value, ApplicationStatus.APPLIED.value}
)

#: Downstream states imply the job was applied to, so they count too — an
#: interview is stronger evidence of intent than a shortlist, not weaker.
_IMPLIES_APPLIED: frozenset[str] = frozenset(
    {
        ApplicationStatus.RECRUITER_SCREEN.value,
        ApplicationStatus.OA.value,
        ApplicationStatus.INTERVIEW_1.value,
        ApplicationStatus.INTERVIEW_2.value,
        ApplicationStatus.FINAL.value,
        ApplicationStatus.OFFER.value,
    }
)

_REQUIRED_KINDS = {RequirementKind.MUST_HAVE.value, RequirementKind.STRONG_PREFERENCE.value}

#: Below this many jobs, a "recurring" gap is one coincidence.
SMALL_SAMPLE_THRESHOLD = 5


@dataclass
class SkillGap:
    skill: str
    job_count: int = 0
    weighted_job_count: float = 0.0
    required_count: int = 0
    preferred_count: int = 0
    role_families: dict[str, int] = field(default_factory=dict)
    lanes: dict[str, int] = field(default_factory=dict)
    evidence_level: str = "NONE"
    evidence_strength: float = 0.0
    representative_jobs: list[dict[str, str]] = field(default_factory=list)

    @property
    def required(self) -> bool:
        return self.required_count >= self.preferred_count

    def as_dict(self) -> dict[str, object]:
        return {
            "skill": self.skill,
            "job_count": self.job_count,
            "weighted_job_count": round(self.weighted_job_count, 1),
            "required_count": self.required_count,
            "preferred_count": self.preferred_count,
            "mostly": "REQUIRED" if self.required else "PREFERRED",
            "role_families": self.role_families,
            "lanes": self.lanes,
            "evidence_level": self.evidence_level,
            "evidence_strength": self.evidence_strength,
            "representative_jobs": self.representative_jobs,
        }


@dataclass
class GapBaseline:
    gaps: list[SkillGap] = field(default_factory=list)
    jobs_considered: int = 0
    small_sample: bool = True
    population: str = "SHORTLISTED and APPLIED only"

    def as_dict(self) -> dict[str, object]:
        return {
            "jobs_considered": self.jobs_considered,
            "small_sample": self.small_sample,
            "population": self.population,
            "note": (
                "Observational only. No learning tasks are created from this — "
                "it is input to a decision, not the decision."
            ),
            "gaps": [g.as_dict() for g in self.gaps],
        }


class SkillGapService:
    def __init__(self, session: Session, bank: EvidenceBankService | None = None) -> None:
        self.session = session
        self.bank = bank or get_evidence_bank()

    def counted_jobs(self, *, lane: str | None = None) -> list[Job]:
        """Jobs whose application reached a state that reflects real intent."""
        stmt = (
            select(Job)
            .join(Application, Application.job_id == Job.job_id)
            .where(Job.is_demo.is_(False), Application.is_demo.is_(False))
        )
        if lane and lane != "ALL":
            stmt = stmt.where(Job.search_lane == lane)

        out: list[Job] = []
        for job in self.session.execute(stmt).scalars():
            application = job.application
            if application is None:
                continue
            if self._counts(application):
                out.append(job)
        return out

    def _counts(self, application: Application) -> bool:
        if application.current_status in COUNTED_STATUSES:
            return True
        if application.current_status in _IMPLIES_APPLIED:
            return True
        # A job that was shortlisted and later rejected still expressed intent.
        return any(
            row.to_status in COUNTED_STATUSES | _IMPLIES_APPLIED
            for row in application.status_history
        )

    def baseline(self, *, lane: str | None = None, limit: int = 25) -> GapBaseline:
        jobs = self.counted_jobs(lane=lane)
        result = GapBaseline(
            jobs_considered=len(jobs),
            small_sample=len(jobs) < SMALL_SAMPLE_THRESHOLD,
        )
        if lane and lane != "ALL":
            result.population = f"{lane}: SHORTLISTED and APPLIED only"

        gaps: dict[str, SkillGap] = {}
        for job in jobs:
            for skill_name, kind in self._unsupported_skills(job):
                gap = gaps.get(skill_name)
                if gap is None:
                    skill = self.bank.get_skill_by_name(skill_name)
                    gap = SkillGap(
                        skill=skill_name,
                        evidence_level=skill.evidence_level if skill else "NONE",
                        evidence_strength=(self.bank.skill_weight(skill.name) if skill else 0.0),
                    )
                    gaps[skill_name] = gap

                gap.job_count += 1
                gap.weighted_job_count += self._job_weight(job)
                if kind in _REQUIRED_KINDS:
                    gap.required_count += 1
                else:
                    gap.preferred_count += 1

                family = job.role_family or "UNKNOWN"
                gap.role_families[family] = gap.role_families.get(family, 0) + 1
                gap.lanes[job.search_lane] = gap.lanes.get(job.search_lane, 0) + 1

                if len(gap.representative_jobs) < 4:
                    gap.representative_jobs.append(
                        {
                            "job_id": job.job_id,
                            "company": job.company_name,
                            "title": job.title,
                        }
                    )

        result.gaps = sorted(
            gaps.values(),
            key=lambda g: (-g.weighted_job_count, -g.required_count, g.skill),
        )[:limit]
        return result

    def _job_weight(self, job: Job) -> float:
        """A high-priority job's gaps matter more than a marginal job's.

        Weighting by the priority score the system already computed avoids
        inventing a second opinion about which jobs matter.
        """
        analysis = job.latest_analysis
        score = getattr(getattr(analysis, "priority_score", None), "total", None)
        if not score:
            return 1.0
        return round(0.5 + float(score) / 100.0, 3)

    def _unsupported_skills(self, job: Job) -> list[tuple[str, str]]:
        """(skill name, requirement kind) for every unsupported named skill."""
        analysis = job.latest_analysis
        if analysis is None:
            return []
        rows = self.session.execute(
            select(JobRequirement, EvidenceMatch)
            .join(EvidenceMatch, EvidenceMatch.requirement_id == JobRequirement.requirement_id)
            .where(JobRequirement.analysis_id == analysis.analysis_id)
        ).all()

        found: dict[str, str] = {}
        for requirement, match in rows:
            if SupportStatus(match.support_status) not in {
                SupportStatus.UNSUPPORTED,
                SupportStatus.UNKNOWN,
            }:
                continue
            for keyword in requirement.keywords or []:
                skill = self.bank.get_skill_by_name(str(keyword))
                if skill is None:
                    continue
                if self.bank.skill_weight(skill.name) >= 0.5 and skill.resume_usable:
                    continue
                # A MUST_HAVE beats a NICE_TO_HAVE if the same skill appears twice.
                if skill.name not in found or requirement.kind in _REQUIRED_KINDS:
                    found[skill.name] = requirement.kind
        return list(found.items())
