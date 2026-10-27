"""Applying the functional classification to a stored job.

One place that takes a `Job`, asks the classifier and the strategy what it is,
and writes the answer onto the row. Used by the ingestion pipeline (so new jobs
arrive classified) and by the reclassification CLI (so the existing corpus
catches up), which is what keeps those two from drifting.

**What it writes:** the functional columns only — `functional_primary`,
`functional_match`, `hidden_fit`, `experience_band`, `start_fit`,
`functional_profile`, `functional_classifier_version`.

**What it never touches:** applications, resume versions, cover letters,
contacts, referrals, raw source records, provenance, or CRM state. A
reclassification changes the system's opinion of a job. It does not change
anything the user did about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ingestion import Job
from app.services.classifiers import classify_remote_status, parse_location
from app.services.functional_classifier import (
    DEPRIORITIZED,
    FUNCTIONAL_CLASSIFIER_VERSION,
    FunctionalClassifier,
    FunctionalRoleProfile,
)
from app.services.search_strategy import (
    OVERLEVELLED,
    ExperienceBand,
    StartFit,
    classify_experience_band,
    classify_start_fit,
    excluded_employment_types,
    lane_is_active,
)

#: Below this, there is nothing to classify from and the honest answer is
#: "unknown" rather than a verdict derived from a title.
MIN_DESCRIPTION_CHARS = 200


@dataclass
class FunctionalVerdict:
    profile: FunctionalRoleProfile
    band: ExperienceBand
    start: StartFit
    #: Would this appear in the active search?
    active_target: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = self.profile.as_dict()
        payload["experience"] = self.band.as_dict()
        payload["start"] = self.start.as_dict()
        payload["active_target"] = self.active_target
        payload["exclusion_reasons"] = self.reasons
        return payload


def evaluate(job: Job, *, classifier: FunctionalClassifier | None = None) -> FunctionalVerdict:
    """Classify one job. Pure with respect to the database."""
    classifier = classifier or FunctionalClassifier()
    description = job.description or ""
    profile = classifier.classify(title=job.title or "", description=description)
    band = classify_experience_band(job.title or "", description)
    start = classify_start_fit(description)

    reasons: list[str] = []
    active = bool(profile.is_functional_match)

    # A deprioritised function is not an active target. Today that means a
    # software role whose specialisation the evidence does not cover — frontend,
    # mobile, embedded — which stays classified as software and stays
    # explicable, but is not chased. D-49.
    if profile.target_priority == DEPRIORITIZED:
        active = False

    if len(description) < MIN_DESCRIPTION_CHARS:
        active = False
        reasons.append("No usable description is stored, so the function cannot be judged.")
    if not profile.is_functional_match:
        active = False
    if band.band == OVERLEVELLED:
        active = False
        reasons.append(band.detail)
    if (job.employment_type or "") in excluded_employment_types():
        active = False
        reasons.append(
            "Internships and co-ops are not part of the active search as of 2026-08-24."
        )
    if not lane_is_active(job.search_lane):
        active = False
        reasons.append(f"Lane {job.search_lane} is archived.")

    # US only. "Remote Poland" and "Credit Risk Operations Associate (CDMX)" are
    # real data roles and real recoveries; they are still not roles the candidate can
    # take, and the first audit put both in the top 25.
    if job.location_raw:
        location = parse_location(job.location_raw, description[:2000])
        remote = classify_remote_status(job.title or "", job.location_raw, description[:2000])
        if not location.is_us:
            active = False
            reasons.append(
                f"{job.location_raw} is outside the US"
                + (" (remote, but not US-based)." if remote.value == "REMOTE" else ".")
            )

    return FunctionalVerdict(
        profile=profile, band=band, start=start, active_target=active, reasons=reasons
    )


def apply_to_job(job: Job, *, classifier: FunctionalClassifier | None = None) -> FunctionalVerdict:
    """Classify and write the functional columns. Touches nothing else."""
    verdict = evaluate(job, classifier=classifier)
    job.functional_primary = verdict.profile.primary_function or None
    job.functional_match = bool(verdict.profile.is_functional_match)
    job.hidden_fit = bool(verdict.profile.hidden_fit)
    job.experience_band = verdict.band.band
    job.start_fit = verdict.start.status
    job.active_target = verdict.active_target
    job.target_priority = verdict.profile.target_priority
    job.functional_profile = verdict.as_dict()
    job.functional_classifier_version = FUNCTIONAL_CLASSIFIER_VERSION
    return verdict


def reclassify(
    session: Session,
    *,
    only_stale: bool = True,
    limit: int | None = None,
) -> dict[str, int]:
    """Bring stored jobs up to the current classifier.

    `only_stale` skips rows already carrying this classifier version, so a
    re-run after a partial pass is cheap and a re-run after no change is free.
    """
    classifier = FunctionalClassifier()
    counts: dict[str, int] = {
        "examined": 0,
        "functional_match": 0,
        "hidden_fit": 0,
        "active_target": 0,
        "no_description": 0,
    }

    stmt = select(Job).where(Job.is_demo.is_(False))
    if only_stale:
        stmt = stmt.where(
            (Job.functional_classifier_version.is_(None))
            | (Job.functional_classifier_version != FUNCTIONAL_CLASSIFIER_VERSION)
        )
    if limit:
        stmt = stmt.limit(limit)

    for job in session.execute(stmt).scalars():
        verdict = apply_to_job(job, classifier=classifier)
        counts["examined"] += 1
        if len(job.description or "") < MIN_DESCRIPTION_CHARS:
            counts["no_description"] += 1
        if verdict.profile.is_functional_match:
            counts["functional_match"] += 1
        if verdict.profile.hidden_fit:
            counts["hidden_fit"] += 1
        if verdict.active_target:
            counts["active_target"] += 1

    return counts


def why_this_matches(job: Job) -> dict[str, Any]:
    """The user-facing explanation. No formulas, no internal scores.

    Reads the stored profile rather than reclassifying, so what the page shows
    is what the system actually decided.
    """
    stored = job.functional_profile or {}
    if not stored:
        return {
            "available": False,
            "detail": "This job has not been classified yet.",
        }

    experience = stored.get("experience") or {}
    return {
        "available": True,
        # `classified_function` so a deprioritised software role still says
        # "Software Engineering" rather than falling silent. D-49.
        "primary_function": (
            stored.get("primary_function") or stored.get("classified_function") or ""
        ),
        "target_priority": stored.get("target_priority") or "",
        "specialization_fit": stored.get("specialization_fit") or "",
        "secondary_functions": stored.get("secondary_functions") or [],
        "domains": stored.get("domains") or [],
        "strong_signals": stored.get("primary_capabilities") or [],
        "supporting_signals": (stored.get("secondary_capabilities") or [])[:6],
        "experience_band": experience.get("band") or "",
        "experience_label": experience.get("label") or "",
        "experience_detail": experience.get("detail") or "",
        "employer_title": job.title,
        "title_alignment": stored.get("title_alignment") or "",
        "hidden_fit": bool(stored.get("hidden_fit")),
        "reasons": stored.get("reasons") or [],
        "concerns": stored.get("negative_function_signals") or [],
        "exclusion_reasons": stored.get("exclusion_reasons") or [],
        "start": (stored.get("start") or {}).get("detail") or "",
        "classifier_version": stored.get("classifier_version") or "",
    }
