"""Channel B A/B measurement harness.

Answers the only question that justifies Channel B's cost and risk:

    how many USEFUL UNIQUE jobs does broad discovery add beyond Channel A?

Not "how many jobs did Apify find". A LinkedIn Actor returning 60 jobs that the
Greenhouse watchlist already had, plus 40 sales roles, has added nothing.

The definition of a useful unique job is fixed **here, before any run**, so it
cannot be adjusted after seeing results:

    new to the OS
      AND prefilter PLAUSIBLE or UNCERTAIN
      AND lane is FALL_2026_INTERNSHIP_COOP or FULL_TIME_2027
      AND not INELIGIBLE and not a closed listing

Everything else in the scorecard is descriptive. This is the number that decides
whether Channel B stays.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.connectors.apify import ApifyConnector
from app.connectors.base import SourceConfig
from app.models.enums import SearchLane
from app.models.ingestion import Job
from app.services.pipeline import JobPipeline

PROFILES_PATH = REPO_ROOT / "data" / "config" / "channel_b_profiles.yaml"

#: Locked before the experiment runs.
USEFUL_DEFINITION = (
    "new to the OS AND prefilter PLAUSIBLE or UNCERTAIN AND lane is "
    "FALL_2026_INTERNSHIP_COOP or FULL_TIME_2027 AND eligibility is not "
    "INELIGIBLE AND the listing is not closed"
)


@dataclass
class ActorScorecard:
    actor_id: str

    # A — coverage
    results_returned: int = 0
    raw_valid: int = 0
    identity_unique: int = 0
    already_known_channel_a: int = 0
    new_to_os: int = 0

    # B — relevance
    plausible: int = 0
    uncertain: int = 0
    irrelevant: int = 0
    fall_reviewable: int = 0
    fulltime_reviewable: int = 0

    # C — data quality (counts of records having the field)
    has_job_id: int = 0
    has_description: int = 0
    description_chars_total: int = 0
    has_posted_date: int = 0
    has_location: int = 0
    has_employment_type: int = 0
    has_apply_url: int = 0
    has_company: int = 0
    has_salary: int = 0

    # D — canonical quality
    employer_apply_url: int = 0
    canonical_verified: int = 0

    # E — operational
    runtime_seconds: float = 0.0
    failures: int = 0
    schema_anomalies: list[str] = field(default_factory=list)
    respected_max_results: bool = True

    # F — economic. Never estimated: only what the platform actually reports.
    actual_cost_usd: float | None = None

    # The decisive metric.
    useful_unique_jobs: int = 0

    @property
    def cost_per_raw_record(self) -> float | None:
        if self.actual_cost_usd is None or not self.results_returned:
            return None
        return round(self.actual_cost_usd / self.results_returned, 6)

    @property
    def cost_per_new_job(self) -> float | None:
        if self.actual_cost_usd is None or not self.new_to_os:
            return None
        return round(self.actual_cost_usd / self.new_to_os, 6)

    @property
    def cost_per_useful_job(self) -> float | None:
        if self.actual_cost_usd is None or not self.useful_unique_jobs:
            return None
        return round(self.actual_cost_usd / self.useful_unique_jobs, 6)

    def as_dict(self) -> dict[str, Any]:
        payload = {k: v for k, v in self.__dict__.items()}
        payload["description_completeness_pct"] = (
            round(100 * self.has_description / self.raw_valid, 1) if self.raw_valid else None
        )
        payload["mean_description_chars"] = (
            round(self.description_chars_total / self.has_description)
            if self.has_description
            else 0
        )
        payload["cost_per_raw_record"] = self.cost_per_raw_record
        payload["cost_per_new_job"] = self.cost_per_new_job
        payload["cost_per_useful_job"] = self.cost_per_useful_job
        return payload


def load_profiles() -> dict[str, Any]:
    return yaml.safe_load(PROFILES_PATH.read_text(encoding="utf-8")) or {}


def _boolean_or_query(title_concepts: list[str]) -> str:
    """Combine every title concept into ONE boolean-OR query string.

    Used only for ``title_mode: boolean_or_single_query`` actors — keeps a
    profile to exactly one search "tile" regardless of how many
    title_concepts it lists. Not used for ``native_multi_title`` actors,
    which search each title_concept as its own array entry natively.
    """
    return " OR ".join(f'"{concept}"' for concept in title_concepts)


def _location_multiplier(mapping: dict[str, Any], profile: dict[str, Any]) -> int:
    """How many independent location-scoped searches this input will trigger.

    Purely descriptive for ``run_level``-scope actors (worst-case sizing does
    not use it there) — kept for the search-expansion diagnostic and for
    ``per_title_query_and_location``-scope actors, where it IS load-bearing.
    """
    location_mode = mapping.get("location_mode")
    locations = list(profile.get("locations") or [])
    if location_mode == "single_location_or_country_plus_cities":
        # A profile location like "Boston, Massachusetts" becomes city "Boston"
        # under a broad country/location scope; "United States" alone has no
        # comma and is used directly as the location with no cities loop.
        cities = [loc.split(",")[0].strip() for loc in locations if "," in loc]
        return max(1, len(cities))
    # Array-mode location fields (harvestapi): each array entry is its own
    # "location input" per the Actor's own maxItems description.
    return max(1, len(locations))


def _title_tiles(mapping: dict[str, Any], profile: dict[str, Any]) -> int:
    """How many independent title-scoped searches this input will trigger."""
    if mapping.get("title_mode") == "native_multi_title":
        return max(1, len(profile.get("title_concepts") or []))
    return 1  # boolean_or_single_query always collapses to one tile


def worst_case_records(actor_id: str, profile: dict[str, Any], *, config: dict[str, Any]) -> int:
    """The mathematical upper bound on returned records for this input.

    Scope-dependent, per ``max_field_scope`` in the actor's mapping:

    * ``per_title_query_and_location``: title tiles × location multiplier ×
      the max-results field value (harvestapi — its own field description is
      explicitly multiplicative).
    * ``run_level``: just the max-results field value on its own (worldunboxer
      — ``jobs_entries`` caps the final merged result set per its field
      description, with no per-title/per-location qualifier).

    Used both to size that field and, independently, by
    ``app.services.actor_schema_guard`` to verify a constructed input never
    exceeds policy before any Actor launches.
    """
    mapping = (config.get("actor_input_mappings") or {}).get(actor_id)
    if not mapping:
        raise ValueError(f"No input mapping for {actor_id}.")
    if mapping.get("max_field_scope") == "run_level":
        return 1
    return _title_tiles(mapping, profile) * _location_multiplier(mapping, profile)


def search_expansion_combos(
    actor_id: str, profile: dict[str, Any], *, config: dict[str, Any]
) -> int:
    """How many independent internal searches this input triggers.

    A diagnostic, distinct from ``worst_case_records``: a run-level max-field
    scope still fans out one search per title/city combination internally —
    it just caps the MERGED output, not each combination separately. Useful
    for interpreting data quality/coverage, never for the cost/result cap.
    """
    mapping = (config.get("actor_input_mappings") or {}).get(actor_id)
    if not mapping:
        raise ValueError(f"No input mapping for {actor_id}.")
    return _title_tiles(mapping, profile) * _location_multiplier(mapping, profile)


def build_actor_input(
    actor_id: str, profile: dict[str, Any], *, max_results: int, config: dict[str, Any]
) -> dict[str, Any]:
    """Map a logical profile onto one Actor's real, live-verified input schema.

    Unsupported filters are omitted rather than forced. Pushing a filter an
    Actor does not understand would change what was actually asked while
    looking like a fair comparison.

    ``max_results`` is the POLICY CAP (e.g. 20). The actual per-Actor
    max-results field is sized so ``worst_case_records`` never exceeds it —
    for a ``per_title_query_and_location``-scope actor that means dividing
    down by the location multiplier; for a ``run_level``-scope actor the
    field is simply set to the policy cap directly. See ``worst_case_records``.
    """
    mapping = (config.get("actor_input_mappings") or {}).get(actor_id)
    if not mapping:
        raise ValueError(f"No input mapping for {actor_id}.")

    title_concepts = list(profile.get("title_concepts") or [])
    if mapping.get("title_mode") == "native_multi_title":
        title_value: Any = title_concepts
    else:
        title_query = _boolean_or_query(title_concepts)
        title_value = [title_query] if mapping.get("title_field_type") == "array" else title_query

    scope = mapping.get("max_field_scope")
    if scope == "run_level":
        per_query_cap = max_results
    else:
        multiplier = _location_multiplier(mapping, profile)
        per_query_cap = max(1, max_results // multiplier)

    actor_input: dict[str, Any] = {mapping["title_field"]: title_value}

    location_mode = mapping.get("location_mode")
    locations = list(profile.get("locations") or [])
    if location_mode == "single_location_or_country_plus_cities":
        cities = [loc.split(",")[0].strip() for loc in locations if "," in loc]
        if cities:
            # Broad country/location scope with a per-city loop, per the
            # Actor's own field description ("Set the Location field to the
            # relevant country to ensure accurate city matching").
            actor_input[mapping["location_field"]] = "United States"
            actor_input[mapping["cities_field"]] = cities
        elif locations:
            actor_input[mapping["location_field"]] = locations[0]
    else:
        actor_input[mapping["location_field"]] = locations

    actor_input[mapping["max_field"]] = per_query_cap

    recency = profile.get("recency")
    if recency and mapping.get("recency_field"):
        value = (mapping.get("recency_values") or {}).get(recency)
        if value is not None:
            actor_input[mapping["recency_field"]] = value

    workplace = profile.get("workplace")
    if workplace and workplace != "ANY" and mapping.get("workplace_field"):
        value = (mapping.get("workplace_values") or {}).get(workplace)
        if value is not None:
            wrapped = [value] if mapping.get("workplace_field_type") == "array" else value
            actor_input[mapping["workplace_field"]] = wrapped

    experience = profile.get("experience")
    if experience and mapping.get("experience_field"):
        value = (mapping.get("experience_values") or {}).get(experience)
        if value is not None:
            wrapped = [value] if mapping.get("experience_field_type") == "array" else value
            actor_input[mapping["experience_field"]] = wrapped

    sort_field = mapping.get("sort_field")
    if sort_field and mapping.get("sort_default"):
        actor_input[sort_field] = mapping["sort_default"]

    return actor_input


def actor_start_events(entry: dict[str, Any], *, memory_mb: int | None = None) -> int:
    """How many "Actor start"-type events one run of this Actor charges.

    Found live 2026-08-19 (D-22): worldunboxer's real canary run charged
    TWO Actor-start events, not the one this project had assumed — the event
    count scales at one-per-GB of configured run memory, exactly as each
    such Actor's own pricing description states ("Number of events charged
    depends on Actor memory... minimum one event"), and the run used
    2048MB. An Actor whose start event is flat (confirmed via its own live
    pricingInfos showing no per-GB language, e.g. automation-lab's "Run
    started" event) always charges exactly 1, regardless of memory.
    """
    if not entry.get("pricing_actor_start_scales_with_memory"):
        return 1
    mb = memory_mb if memory_mb is not None else int(entry.get("default_run_memory_mb") or 1024)
    return max(1, -(-mb // 1024))  # ceil division, no float rounding surprises


def projected_cost(
    actor_ids: list[str], *, queries: int, max_results: int, memory_mb: int | None = None
) -> dict[str, Any]:
    """Upper-bound spend from registry pricing. Re-verify against live metadata.

    Includes each Actor's per-run "start" event charge, scaled by memory for
    Actors whose start event is documented as memory-scaled (see
    ``actor_start_events``) — a flat one-event assumption previously
    understated worldunboxer's real cost by ~62% (D-22).
    """
    from app.services.actor_registry import ActorRegistry

    registry = ActorRegistry()
    breakdown: dict[str, float] = {}
    for actor_id in actor_ids:
        entry = registry.check(actor_id).record
        per_1k = float(entry.get("pricing_usd_per_1k_results") or 0.0)
        per_run_start = float(entry.get("pricing_per_run_start_usd") or 0.0)
        start_events = actor_start_events(entry, memory_mb=memory_mb)
        breakdown[actor_id] = round(
            per_1k * queries * max_results / 1000 + per_run_start * start_events * queries, 6
        )
    return {
        "per_actor": breakdown,
        "total": round(sum(breakdown.values()), 6),
        "records_upper_bound": len(actor_ids) * queries * max_results,
    }


class ABHarness:
    """Runs dataset records through the real pipeline and scores each Actor."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.pipeline = JobPipeline(session)
        self.connector = ApifyConnector()

    def _known_job_ids(self) -> set[str]:
        return {j.job_id for j in self.session.execute(select(Job)).scalars()}

    def score_records(
        self,
        actor_id: str,
        records: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        max_results: int = 20,
    ) -> ActorScorecard:
        """Ingest one Actor's dataset records and measure what they added."""
        card = ActorScorecard(actor_id=actor_id, results_returned=len(records))
        if len(records) > max_results:
            card.respected_max_results = False
            card.schema_anomalies.append(
                f"returned {len(records)} records against a cap of {max_results}"
            )

        config = SourceConfig(
            company_name="Channel B Discovery",
            ats_type="APIFY",
            ats_identifier="broad-discovery",
            connector_config={"actor_id": actor_id, "run_id": run_id or ""},
        )

        known_before = self._known_job_ids()
        started = time.monotonic()

        for record in records:
            self._score_data_quality(record, card)
            normalized = self.connector.normalize_raw_record(record, config)
            if normalized is None:
                card.failures += 1
                continue
            card.raw_valid += 1

            outcome = self.pipeline.ingest(normalized)
            if outcome.job is None:
                card.failures += 1
                continue
            self.session.flush()

            job = outcome.job
            if job.job_id in known_before:
                card.already_known_channel_a += 1
            else:
                card.new_to_os += 1
                known_before.add(job.job_id)

            outcome_name = job.prescreen_outcome or ""
            if outcome_name.endswith("PLAUSIBLE"):
                card.plausible += 1
            elif outcome_name.endswith("UNCERTAIN"):
                card.uncertain += 1
            elif outcome_name.endswith("IRRELEVANT"):
                card.irrelevant += 1

            reviewable = outcome_name.endswith(("PLAUSIBLE", "UNCERTAIN"))
            if reviewable and job.search_lane == SearchLane.FALL_2026_INTERNSHIP_COOP.value:
                card.fall_reviewable += 1
            if reviewable and job.search_lane == SearchLane.FULL_TIME_2027.value:
                card.fulltime_reviewable += 1

            if job.canonical_verification == "CANONICAL_VERIFIED":
                card.canonical_verified += 1

            if self._is_useful(job, reviewable=reviewable, is_new=job.job_id not in known_before):
                pass  # counted below against the pre-run snapshot

        card.identity_unique = card.already_known_channel_a + card.new_to_os
        card.runtime_seconds = round(time.monotonic() - started, 2)
        card.useful_unique_jobs = self._count_useful(actor_id, run_id)
        return card

    def _count_useful(self, actor_id: str, run_id: str | None) -> int:
        """Apply the locked definition against what this Actor actually added."""
        from app.models.ingestion import RawJobSourceRecord

        raws = (
            self.session.execute(
                select(RawJobSourceRecord).where(RawJobSourceRecord.apify_actor_id == actor_id)
            )
            .scalars()
            .all()
        )
        if run_id:
            raws = [r for r in raws if r.apify_run_id == run_id]

        useful = 0
        seen: set[str] = set()
        for raw in raws:
            if not raw.job_id or raw.job_id in seen:
                continue
            seen.add(raw.job_id)
            job = self.session.get(Job, raw.job_id)
            if job is None:
                continue
            # Only jobs whose FIRST discovery was this Actor count as "new".
            if job.discovery_source not in ("APIFY", "LINKEDIN_APIFY"):
                continue
            if not (job.prescreen_outcome or "").endswith(("PLAUSIBLE", "UNCERTAIN")):
                continue
            if job.search_lane not in (
                SearchLane.FALL_2026_INTERNSHIP_COOP.value,
                SearchLane.FULL_TIME_2027.value,
            ):
                continue
            if job.eligibility_status == "INELIGIBLE":
                continue
            if job.lifecycle_status == "CLOSED":
                continue
            useful += 1
        return useful

    def _is_useful(self, job: Job, *, reviewable: bool, is_new: bool) -> bool:
        return (
            is_new
            and reviewable
            and job.search_lane
            in (
                SearchLane.FALL_2026_INTERNSHIP_COOP.value,
                SearchLane.FULL_TIME_2027.value,
            )
            and job.eligibility_status != "INELIGIBLE"
            and job.lifecycle_status != "CLOSED"
        )

    @staticmethod
    def _score_data_quality(record: dict[str, Any], card: ActorScorecard) -> None:
        def present(*keys: str) -> bool:
            return any(
                isinstance(record.get(k), str | int | float) and str(record.get(k)).strip()
                for k in keys
            )

        if present("id", "jobId", "job_id"):
            card.has_job_id += 1
        description = ""
        for key in ("descriptionText", "description", "job_description"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                description = value
                break
        if description:
            card.has_description += 1
            card.description_chars_total += len(description)
        if present("postedDate", "postedTime", "posted_date", "publishedAt"):
            card.has_posted_date += 1
        if present("location", "job_location", "jobLocation"):
            card.has_location += 1
        if present("employmentType", "employment_type"):
            card.has_employment_type += 1
        if present("applyUrl", "applicationUrl", "jobUrl", "job_url", "url"):
            card.has_apply_url += 1
        if present("companyName", "company", "company_name"):
            card.has_company += 1
        if present("salary", "salaryRange"):
            card.has_salary += 1
        # An employer-side URL is what makes canonical resolution possible;
        # a linkedin.com URL alone does not.
        for key in ("applyUrl", "applicationUrl", "companyUrl"):
            value = record.get(key)
            if isinstance(value, str) and value and "linkedin.com" not in value.lower():
                card.employer_apply_url += 1
                break


def compare(cards: list[ActorScorecard]) -> dict[str, Any]:
    """Cross-actor summary. Volume alone never decides."""
    return {
        "useful_definition": USEFUL_DEFINITION,
        "actors": {c.actor_id: c.as_dict() for c in cards},
        "totals": {
            "results_returned": sum(c.results_returned for c in cards),
            "new_to_os": sum(c.new_to_os for c in cards),
            "already_known_channel_a": sum(c.already_known_channel_a for c in cards),
            "useful_unique_jobs": sum(c.useful_unique_jobs for c in cards),
            "actual_cost_usd": (
                round(sum(c.actual_cost_usd or 0.0 for c in cards), 6)
                if any(c.actual_cost_usd is not None for c in cards)
                else None
            ),
        },
    }
