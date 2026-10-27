"""Pre-run schema-drift guard for Channel B Actor inputs.

Found live 2026-08-19 (D-20 in IMPLEMENTATION_LOG.md): the recorded
``actor_input_mappings`` had stale field names and enum values that would
NOT have raised an error — Apify Actor input schemas accept unknown
properties silently — so a stale field would have quietly changed what was
actually asked, or worse, silently fallen back to an Actor's own (much
larger) default. `worldunboxer/rapid-linkedin-scraper`'s recorded max-results
field name did not exist at all; sending it would have fallen back to the
Actor's own default of 1000 results, roughly $0.51 on a single call against a
$0.25 experiment gate.

This module makes that class of failure loud instead of silent, BEFORE any
paid Actor call — it validates a fully-constructed ``actor_input`` dict (the
exact JSON about to be sent) against the live-verified contract recorded in
``channel_b_profiles.yaml``'s ``actor_input_mappings``. It does not call the
network; live re-verification against ``fetch-actor-details`` is a separate,
manual pre-run step (the field-name/enum contract here is a pinned snapshot,
re-verified and updated by hand — see D-20).

Only fields that affect spend, result volume, targeting or eligibility are
checked. A harmless new optional Actor field is not a blocker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_DRIFT_BLOCKED = "SCHEMA_DRIFT_BLOCKED"


@dataclass
class SchemaDriftResult:
    actor_id: str
    blocked: bool
    violations: list[str] = field(default_factory=list)
    worst_case_records: int | None = None

    @property
    def code(self) -> str | None:
        return SCHEMA_DRIFT_BLOCKED if self.blocked else None


def _mapping_for(actor_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
    return (config.get("actor_input_mappings") or {}).get(actor_id)


def _count(value: Any) -> int:
    if isinstance(value, list):
        return max(1, len(value))
    return 1


def validate_actor_input(
    actor_id: str,
    actor_input: dict[str, Any],
    *,
    config: dict[str, Any],
    policy_max_results: int,
) -> SchemaDriftResult:
    """Validate a constructed Actor input against the pinned live contract.

    Returns a result with ``blocked=True`` and the reasons if the input would
    spend more than the policy allows, target something other than what was
    asked, or use a field name/enum value known to be stale. The caller must
    refuse to launch the Actor when ``blocked`` is True.
    """
    mapping = _mapping_for(actor_id, config)
    if mapping is None:
        return SchemaDriftResult(
            actor_id=actor_id,
            blocked=True,
            violations=[f"No live-verified input contract exists for {actor_id}."],
        )

    violations: list[str] = []

    # -- stale field-name aliases -----------------------------------------
    stale = set(mapping.get("known_stale_aliases") or [])
    used_stale = stale & set(actor_input)
    for name in sorted(used_stale):
        violations.append(
            f"'{name}' is a known-stale field name for {actor_id} — it does not exist on the "
            f"live schema and would be silently ignored rather than applied."
        )

    # -- max-results field must be present, explicit, and an int ----------
    max_field = mapping.get("max_field")
    max_value: int | None = None
    if not max_field or max_field not in actor_input:
        violations.append(
            f"Max-results field '{max_field}' is missing from the input — the Actor would fall "
            f"back to its own (larger) default rather than the intended cap."
        )
    else:
        raw = actor_input[max_field]
        if not isinstance(raw, int) or isinstance(raw, bool):
            violations.append(f"Max-results field '{max_field}' must be an int, got {raw!r}.")
        else:
            max_value = raw

    # -- title field type -----------------------------------------------
    title_field = mapping.get("title_field")
    title_type = mapping.get("title_field_type")
    title_tiles = 1
    if title_field and title_field in actor_input:
        value = actor_input[title_field]
        if title_type == "array" and not isinstance(value, list):
            violations.append(f"Title field '{title_field}' must be an array, got {type(value)}.")
        elif title_type == "string" and not isinstance(value, str):
            violations.append(f"Title field '{title_field}' must be a string, got {type(value)}.")
        elif title_type == "array":
            title_tiles = _count(value)

    # -- location / cities field type and worst-case multiplier -----------
    location_field = mapping.get("location_field")
    location_type = mapping.get("location_field_type")
    cities_field = mapping.get("cities_field")
    multiplier = 1
    if location_type == "array" and location_field:
        value = actor_input.get(location_field)
        if value is not None and not isinstance(value, list):
            violations.append(f"Location field '{location_field}' must be an array.")
        multiplier = _count(value)
    elif location_type == "string" and location_field:
        value = actor_input.get(location_field)
        if value is not None and not isinstance(value, str):
            violations.append(f"Location field '{location_field}' must be a string, not a list.")
        if cities_field and cities_field in actor_input:
            cities_value = actor_input[cities_field]
            if not isinstance(cities_value, list):
                violations.append(f"Cities field '{cities_field}' must be an array.")
            multiplier = _count(cities_value)

    worst_case = None
    if max_value is not None:
        scope = mapping.get("max_field_scope")
        if scope == "run_level":
            # The max-results field caps the final MERGED result set directly
            # — title/location fan-out does not multiply it. See the
            # 2026-08-19 revision note in channel_b_profiles.yaml.
            worst_case = max_value
            if worst_case > policy_max_results:
                violations.append(
                    f"Worst-case result count {worst_case} ({max_field}={max_value}, run-level "
                    f"cap) exceeds the policy cap of {policy_max_results}."
                )
        else:
            worst_case = title_tiles * multiplier * max_value
            if worst_case > policy_max_results:
                violations.append(
                    f"Worst-case result count {worst_case} (title tiles={title_tiles} × "
                    f"location/city multiplier={multiplier} × {max_field}={max_value}) exceeds "
                    f"the policy cap of {policy_max_results}."
                )

    # -- enum values --------------------------------------------------------
    allowed_enums = mapping.get("allowed_enum_values") or {}
    for enum_field, allowed_values in allowed_enums.items():
        if enum_field not in actor_input:
            continue
        raw_value = actor_input[enum_field]
        values_to_check = raw_value if isinstance(raw_value, list) else [raw_value]
        for candidate in values_to_check:
            if candidate not in allowed_values:
                violations.append(
                    f"'{candidate}' is not a live-verified allowed value for '{enum_field}' on "
                    f"{actor_id} (allowed: {allowed_values})."
                )

    return SchemaDriftResult(
        actor_id=actor_id,
        blocked=bool(violations),
        violations=violations,
        worst_case_records=worst_case,
    )
