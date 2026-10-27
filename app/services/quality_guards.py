"""Prepare-Application guards — the checks a bad record must pass before it
can become tailoring advice.

Narrow and deliberately shallow: this does not re-score or re-classify a
job. It reads fields the pipeline already computed (``is_demo``,
``screen_status``, ``lifecycle_status``, ``eligibility_status``,
``location_country``, ``canonical_url``, ``sponsorship_role_signal``) plus
the Slice 5 experience-fit classifier, and turns them into either a hard
block (the job cannot become an application) or a visible warning (it can,
but the user is shown why they should be careful). Section 6/14 of the
Slice 5 spec: block only CLOSED / DEMO / DETERMINISTICALLY INELIGIBLE;
everything else is UNCERTAIN and gets a warning, never a silent pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.models.ingestion import Job
from app.services.experience_fit import ExperienceFit
from app.services.experience_fit import classify as classify_experience_fit

_NON_US_LOCATION_MARKERS = (
    "india",
    "pune",
    "chennai",
    "bangalore",
    "bengaluru",
    "hyderabad",
    "mumbai",
    "delhi",
    "uk",
    "united kingdom",
    "london",
    "europe",
    " eu ",
    "canada",
    "toronto",
    "israel",
    "germany",
    "france",
    "singapore",
    "australia",
    "china",
    "japan",
)

STALE_AFTER_DAYS = 14


@dataclass
class GuardWarning:
    code: str
    message: str


@dataclass
class GuardResult:
    blocked: bool
    block_reason: str | None = None
    warnings: list[GuardWarning] = field(default_factory=list)
    experience_fit: str = ExperienceFit.UNKNOWN.value
    experience_fit_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "warnings": [{"code": w.code, "message": w.message} for w in self.warnings],
            "experience_fit": self.experience_fit,
            "experience_fit_reason": self.experience_fit_reason,
        }


def _looks_non_us(job: Job) -> bool:
    if job.location_country not in (None, "US"):
        return True
    location_text = (job.location_raw or "").lower()
    return any(marker in location_text for marker in _NON_US_LOCATION_MARKERS)


def check(job: Job) -> GuardResult:
    warnings: list[GuardWarning] = []

    # -- hard blocks -------------------------------------------------------
    if job.is_demo:
        return GuardResult(
            blocked=True,
            block_reason="This is a development/demo record, not a real job. It cannot be prepared.",
        )
    if job.lifecycle_status == "CLOSED":
        return GuardResult(
            blocked=True,
            block_reason="This posting is recorded as closed. Preparation is blocked.",
        )
    if job.eligibility_status == "INELIGIBLE":
        return GuardResult(
            blocked=True,
            block_reason="This job was determined deterministically ineligible.",
        )

    # -- warnings (UNCERTAIN — allowed, but surfaced) ----------------------
    if job.lifecycle_status in ("POSSIBLY_CLOSED", "DISAPPEARED"):
        warnings.append(
            GuardWarning("POSSIBLY_CLOSED", "Recent polls didn't confirm this posting is still up.")
        )

    reference = job.last_seen or job.first_seen
    if reference is not None:
        ref = reference if reference.tzinfo else reference.replace(tzinfo=UTC)
        if datetime.now(UTC) - ref > timedelta(days=STALE_AFTER_DAYS):
            warnings.append(
                GuardWarning(
                    "STALE",
                    f"Not re-confirmed open in over {STALE_AFTER_DAYS} days — verify the "
                    f"canonical posting before relying on this record.",
                )
            )

    fit, fit_reason = classify_experience_fit(job)
    if fit in (ExperienceFit.STRETCH_3_PLUS, ExperienceFit.SENIOR):
        warnings.append(
            GuardWarning(
                "EXPERIENCE_STRETCH",
                f"Experience fit is {fit.value} — {fit_reason}",
            )
        )

    # The Fall/CPT lane was archived on 2026-08-24 (D-46), so a Fall posting no
    # longer raises a CPT review action — that pathway is not being pursued.
    # A historical Fall job is labelled archived instead, which is a statement
    # about the search, not about anyone's authorization.
    if job.search_lane == "FALL_2026_INTERNSHIP_COOP":
        warnings.append(
            GuardWarning(
                "ARCHIVED_LANE",
                "This is a Fall 2026 internship posting. That lane is archived — the "
                "record is kept for history and is not part of the active search.",
            )
        )
    elif job.authorization_state == "AUTHORIZATION_REQUIRES_VERIFICATION":
        warnings.append(
            GuardWarning(
                "NEEDS_ISSO_REVIEW",
                "Work-authorization state has not been verified for this role.",
            )
        )

    if job.sponsorship_role_signal in ("CITIZENSHIP_REQUIRED", "CLEARANCE_REQUIRED"):
        warnings.append(
            GuardWarning(
                "CITIZENSHIP_CLEARANCE_ISSUE",
                f"Posting signals {job.sponsorship_role_signal.replace('_', ' ').lower()} — "
                "confirm you can meet this before applying.",
            )
        )

    if _looks_non_us(job):
        warnings.append(
            GuardWarning(
                "LOCATION_CONFLICT",
                f"Location text ({job.location_raw or 'unknown'}) does not look US-based.",
            )
        )

    if not job.canonical_url:
        warnings.append(
            GuardWarning(
                "NO_CANONICAL_URL",
                "No canonical employer application URL on file — find it manually before applying.",
            )
        )

    return GuardResult(
        blocked=False,
        warnings=warnings,
        experience_fit=fit.value,
        experience_fit_reason=fit_reason,
    )
