"""Regression tests for the defects found in the 2026-08-19 live re-verification
(D-20 in IMPLEMENTATION_LOG.md), and for the schema-drift guard that now
catches them before any paid Actor call. No network, no Actor runs.
"""

from __future__ import annotations

from app.services.ab_harness import build_actor_input, load_profiles
from app.services.actor_schema_guard import SCHEMA_DRIFT_BLOCKED, validate_actor_input

HARVEST_ACTOR = "harvestapi/linkedin-job-search"
RAPID_ACTOR = "worldunboxer/rapid-linkedin-scraper"


def _config():
    return load_profiles()


def _fall_boston():
    return next(p for p in _config()["profiles"] if p["profile_id"] == "ab_fall_boston")


def _fall_remote():
    return next(p for p in _config()["profiles"] if p["profile_id"] == "ab_fall_remote")


# ---------------------------------------------------------------------------
# harvestapi — maxItems is multiplicative, not a flat cap
# ---------------------------------------------------------------------------


def test_harvest_naive_flat_maxitems_is_blocked_as_over_cap():
    """Reproduces the original defect directly: N titles as separate array
    entries × 2 locations × maxItems=20 (the pre-correction assumption) must
    be REJECTED by the guard, not silently allowed."""
    config = _config()
    profile = _fall_boston()
    title_count = len(profile["title_concepts"])
    naive_input = {
        "jobTitles": list(profile["title_concepts"]),  # one element PER concept — the old bug
        "locations": list(profile["locations"]),
        "maxItems": 20,
    }
    result = validate_actor_input(HARVEST_ACTOR, naive_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert result.code == SCHEMA_DRIFT_BLOCKED
    assert result.worst_case_records == title_count * 2 * 20
    assert any("exceeds the policy cap" in v for v in result.violations)


def test_harvest_corrected_input_passes_the_guard():
    config = _config()
    actor_input = build_actor_input(HARVEST_ACTOR, _fall_boston(), max_results=20, config=config)
    result = validate_actor_input(HARVEST_ACTOR, actor_input, config=config, policy_max_results=20)
    assert result.blocked is False
    assert result.worst_case_records is not None
    assert result.worst_case_records <= 20


def test_harvest_stale_enum_casing_is_rejected():
    """Live enum values are lowercase; the recorded 'Remote'/'Entry level'
    values would silently fail to filter on the real Actor."""
    config = _config()
    bad_input = {
        "jobTitles": ['"AI Intern"'],
        "locations": ["United States"],
        "maxItems": 20,
        "workplaceType": ["Remote"],  # wrong casing
    }
    result = validate_actor_input(HARVEST_ACTOR, bad_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert any("Remote" in v and "workplaceType" in v for v in result.violations)


def test_harvest_live_enum_casing_is_accepted():
    config = _config()
    good_input = {
        "jobTitles": ['"AI Intern"'],
        "locations": ["United States"],
        "maxItems": 20,
        "workplaceType": ["remote"],
    }
    result = validate_actor_input(HARVEST_ACTOR, good_input, config=config, policy_max_results=20)
    assert result.blocked is False


# ---------------------------------------------------------------------------
# worldunboxer — stale field names, missing max field, wrong types/enums
# ---------------------------------------------------------------------------


def test_worldunboxer_stale_maxentries_alias_is_rejected():
    """The original, most severe defect: `maxEntries` does not exist on the
    live schema. Sending it would silently fall back to the Actor's own
    default of 1000 results (~$0.51) instead of raising an error."""
    config = _config()
    stale_input = {
        "job_title": '"AI Intern"',
        "location": "United States",
        "maxEntries": 20,  # the stale, non-existent field name
    }
    result = validate_actor_input(RAPID_ACTOR, stale_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert result.code == SCHEMA_DRIFT_BLOCKED
    assert any("maxEntries" in v and "stale" in v for v in result.violations)
    # And separately: jobs_entries is still absent, which is its own violation.
    assert any("jobs_entries" in v and "missing" in v for v in result.violations)


def test_worldunboxer_missing_jobs_entries_cannot_silently_default_to_1000():
    """Even without the stale alias present, omitting jobs_entries entirely
    must block — never silently proceed and let the Actor default to 1000."""
    config = _config()
    incomplete_input = {"job_title": '"AI Intern"', "location": "United States"}
    result = validate_actor_input(
        RAPID_ACTOR, incomplete_input, config=config, policy_max_results=20
    )
    assert result.blocked is True
    assert any("missing" in v.lower() for v in result.violations)


def test_worldunboxer_run_level_cap_is_not_multiplied_by_titles_or_cities():
    """2026-08-19 (D-21): jobs_entries caps the merged result set directly —
    many jobs_titles entries and many cities must NOT multiply the worst
    case the way harvestapi's per-location maxItems does."""
    config = _config()
    wide_input = {
        "jobs_titles": ["A", "B", "C", "D", "E"],
        "location": "United States",
        "cities": ["Boston", "Cambridge", "Somerville"],
        "jobs_entries": 20,
    }
    result = validate_actor_input(RAPID_ACTOR, wide_input, config=config, policy_max_results=20)
    assert result.blocked is False
    assert result.worst_case_records == 20


def test_worldunboxer_run_level_cap_still_blocks_when_the_field_itself_is_over_policy():
    config = _config()
    over_cap_input = {
        "jobs_titles": ["AI Intern"],
        "location": "United States",
        "jobs_entries": 25,
    }
    result = validate_actor_input(RAPID_ACTOR, over_cap_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert result.worst_case_records == 25
    assert any("run-level cap" in v for v in result.violations)


def test_worldunboxer_explicit_jobs_entries_passes():
    config = _config()
    actor_input = build_actor_input(RAPID_ACTOR, _fall_remote(), max_results=20, config=config)
    assert actor_input["jobs_entries"] == 20
    result = validate_actor_input(RAPID_ACTOR, actor_input, config=config, policy_max_results=20)
    assert result.blocked is False


def test_worldunboxer_location_must_be_a_string_not_a_list():
    """The old mapping always sent a list for `location`, but the live field
    is a single string — sending a list would not match the real schema."""
    config = _config()
    bad_input = {
        "job_title": '"AI Intern"',
        "location": ["Boston, Massachusetts", "Cambridge, Massachusetts"],  # wrong type
        "jobs_entries": 10,
    }
    result = validate_actor_input(RAPID_ACTOR, bad_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert any("must be a string" in v for v in result.violations)


def test_worldunboxer_stale_posted_within_value_is_rejected():
    """The old mapping sent epoch-second strings like 'r2592000'; the live
    enum is human-readable ('Past Month')."""
    config = _config()
    bad_input = {
        "job_title": '"AI Intern"',
        "location": "United States",
        "jobs_entries": 20,
        "posted_within": "r2592000",
    }
    result = validate_actor_input(RAPID_ACTOR, bad_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert any("posted_within" in v for v in result.violations)


def test_worldunboxer_live_posted_within_value_is_accepted():
    config = _config()
    good_input = {
        "job_title": '"AI Intern"',
        "location": "United States",
        "jobs_entries": 20,
        "posted_within": "Past Month",
    }
    result = validate_actor_input(RAPID_ACTOR, good_input, config=config, policy_max_results=20)
    assert result.blocked is False


def test_worldunboxer_stale_workarrangement_alias_is_rejected():
    config = _config()
    bad_input = {
        "job_title": '"AI Intern"',
        "location": "United States",
        "jobs_entries": 20,
        "workArrangement": "Remote",  # real field is work_arrangement
    }
    result = validate_actor_input(RAPID_ACTOR, bad_input, config=config, policy_max_results=20)
    assert result.blocked is True
    assert any("workArrangement" in v for v in result.violations)


# ---------------------------------------------------------------------------
# Unknown actor / no contract
# ---------------------------------------------------------------------------


def test_actor_with_no_pinned_contract_is_blocked():
    config = _config()
    result = validate_actor_input(
        "someone/unlisted-actor", {"anything": 1}, config=config, policy_max_results=20
    )
    assert result.blocked is True
    assert result.code == SCHEMA_DRIFT_BLOCKED
