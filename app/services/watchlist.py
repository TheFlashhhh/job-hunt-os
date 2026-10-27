"""Loads ``data/config/companies.yaml`` into connector configurations.

Two populations live in that file and must never mix:

``companies``          the real watchlist. The user curates it. Anything the system
                      proposes arrives here as ``active: false`` and stays that
                      way until he approves it.
``fixture_companies``  a handful of clearly-labelled public boards used only to
                      prove connector behaviour. Everything they produce is
                      flagged ``is_fixture`` and excluded from real analytics.

The distinction is enforced here rather than left to convention: a fixture is
force-flagged on load, so it cannot become real data by someone forgetting a
field. And a company proposed by the system cannot be collected from until a
human flips ``active``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT
from app.connectors.base import SourceConfig

logger = logging.getLogger("jobhunt.watchlist")

CONFIG_PATH = REPO_ROOT / "data" / "config" / "companies.yaml"

VALID_SCOPES = {"FALL", "FULL_TIME", "BOTH"}

#: ATS types Channel A can poll today. Everything else stays on the watchlist
#: with automation disabled rather than being dropped.
AUTOMATABLE_ATS = {"GREENHOUSE", "LEVER", "ASHBY", "SMARTRECRUITERS", "WORKDAY"}


class WatchlistError(RuntimeError):
    """The watchlist file is unreadable or structurally invalid."""


@dataclass
class WatchlistLoadResult:
    companies: list[SourceConfig] = field(default_factory=list)
    fixtures: list[SourceConfig] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def all_entries(self) -> list[SourceConfig]:
        return self.companies + self.fixtures

    @property
    def active_companies(self) -> list[SourceConfig]:
        """Watchlist companies that Channel A can actually poll.

        Both flags are required: a company must be wanted *and* reachable by an
        implemented connector. Fixtures are never included.
        """
        return [c for c in self.companies if c.active and c.automated_collection_enabled]

    @property
    def watchlist_companies(self) -> list[SourceConfig]:
        """Everything on the watchlist, including the manual-only companies."""
        return [c for c in self.companies if c.active]

    @property
    def manual_only_companies(self) -> list[SourceConfig]:
        """On the watchlist, but no connector supports their ATS yet."""
        return [c for c in self.companies if c.active and not c.automated_collection_enabled]

    def summary(self) -> dict[str, Any]:
        by_ats: dict[str, int] = {}
        for entry in self.companies:
            by_ats[entry.ats_type] = by_ats.get(entry.ats_type, 0) + 1
        by_tier: dict[str, int] = {}
        for entry in self.companies:
            by_tier[entry.tier] = by_tier.get(entry.tier, 0) + 1
        return {
            "companies": len(self.companies),
            "watchlist_active": len(self.watchlist_companies),
            "automation_enabled": len(self.active_companies),
            "manual_only": len(self.manual_only_companies),
            "inactive": len(self.companies) - len(self.watchlist_companies),
            "fixtures": len(self.fixtures),
            "by_ats": dict(sorted(by_ats.items())),
            "by_tier": dict(sorted(by_tier.items())),
            "problems": self.problems,
        }


def load_watchlist(path: Path | None = None) -> WatchlistLoadResult:
    """Read the watchlist. Never raises for a single bad entry."""
    target = path or CONFIG_PATH
    result = WatchlistLoadResult()

    if not target.exists():
        result.problems.append(f"{target} does not exist.")
        return result

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WatchlistError(f"{target} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise WatchlistError(f"{target} must contain a mapping at the top level.")

    for entry in _as_list(raw.get("companies")):
        parsed = _parse_entry(entry, is_fixture=False, problems=result.problems)
        if parsed is not None:
            result.companies.append(parsed)

    # Both keys are accepted so the file can use whichever reads better; the
    # flag is forced either way.
    for key in ("fixture_companies", "demo_companies"):
        for entry in _as_list(raw.get(key)):
            parsed = _parse_entry(entry, is_fixture=True, problems=result.problems)
            if parsed is not None:
                result.fixtures.append(parsed)

    return result


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_entry(
    entry: dict[str, Any], *, is_fixture: bool, problems: list[str]
) -> SourceConfig | None:
    name = str(entry.get("company_name") or "").strip()
    if not name:
        problems.append("An entry has no company_name; skipped.")
        return None

    ats_type = str(entry.get("ats_type") or "UNKNOWN").strip().upper()
    ats_identifier = str(entry.get("ats_identifier") or "").strip()

    # A company on an unsupported or unidentified ATS is NOT skipped. It stays
    # in the search universe with automation disabled, to be covered by manual
    # discovery or a later channel. Dropping it here would silently delete a
    # deliberately-chosen employer — the exact failure this split exists to
    # prevent. Only a company that claims an automatable ATS but gives no token
    # is a genuine configuration error worth reporting.
    if ats_type in AUTOMATABLE_ATS and not ats_identifier:
        problems.append(
            f"{name}: ats_type {ats_type} requires an ats_identifier; "
            f"automation disabled until one is supplied."
        )

    scope = str(entry.get("watchlist_scope") or "BOTH").strip().upper()
    if scope not in VALID_SCOPES:
        problems.append(f"{name}: watchlist_scope {scope!r} is not one of {sorted(VALID_SCOPES)}.")
        scope = "BOTH"

    # A fixture is force-flagged. Nothing in the file can make one look real.
    fixture = bool(entry.get("is_fixture")) or bool(entry.get("is_demo")) or is_fixture

    # Automation is capability-gated, and the file cannot claim more than the
    # connector registry supports.
    declared = entry.get("automated_collection_enabled")
    supported = ats_type in AUTOMATABLE_ATS and bool(ats_identifier)
    automated = supported if declared is None else (bool(declared) and supported)

    return SourceConfig(
        company_name=name,
        company_id=str(entry.get("company_id")) if entry.get("company_id") else None,
        ats_type=ats_type,
        ats_identifier=ats_identifier,
        careers_url=entry.get("careers_url"),
        watchlist_scope=scope,
        priority=int(entry.get("priority", 3)),
        locations=list(entry.get("preferred_locations") or entry.get("locations") or []),
        target_role_families=list(entry.get("target_role_families") or []),
        connector_config=dict(entry.get("connector_config") or {}),
        active=bool(entry.get("active", False)),
        automated_collection_enabled=automated,
        tier=str(entry.get("proposal_tier") or entry.get("tier") or "TIER_B"),
        fall_signal=str(entry.get("fall_signal") or "UNKNOWN"),
        industry_category=entry.get("industry_category"),
        last_validated_at=entry.get("last_validated_at"),
        is_fixture=fixture,
        notes=entry.get("notes"),
    )


#: Collection order. Tier drives *scheduling priority only* — never eligibility
#: or fit, which are decided per job by the pipeline.
TIER_ORDER = {"TIER_A": 0, "TIER_B": 1, "TIER_C": 2}


def entries_for_lane(result: WatchlistLoadResult, lane: str | None) -> list[SourceConfig]:
    """Collectable companies whose scope covers the requested lane, tier-ordered."""
    candidates = result.active_companies
    if lane and lane != "ALL":
        wanted = "FALL" if lane.startswith("FALL") else "FULL_TIME"
        candidates = [c for c in candidates if c.watchlist_scope in (wanted, "BOTH")]
    return sorted(candidates, key=lambda c: (TIER_ORDER.get(c.tier, 1), c.company_name))
