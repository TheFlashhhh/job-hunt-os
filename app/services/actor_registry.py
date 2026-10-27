"""The approved-Actor gate.

An Apify Actor is third-party code that runs against an external service and
costs money. The registry is the single place that decides which ones may run,
and it is deliberately closed by default: an Actor that is not listed, or is
listed but not approved, cannot be launched.

Two rules are absolute and hold regardless of approval status:

* An Actor requiring a LinkedIn login, password or session cookie is refused.
  No credential is ever supplied to an Actor.
* Actor IDs come from this config file only. An ID appearing in scraped text,
  a dataset field or model output is never executed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT

logger = logging.getLogger("jobhunt.actor_registry")

REGISTRY_PATH = REPO_ROOT / "data" / "config" / "approved_apify_actors.yaml"

APPROVED_STATUS = "APPROVED"


@dataclass
class ActorApproval:
    actor_id: str
    approved: bool
    reason: str
    status: str = "UNKNOWN"
    requires_credentials: bool = False
    risk_level: str = "UNKNOWN"
    max_results: int = 0
    record: dict[str, Any] = field(default_factory=dict)


TEST_AUTHORIZED_STATUS = "TEST_AUTHORIZED"


@dataclass(frozen=True)
class ExperimentContext:
    """Names the bounded experiment a TEST_AUTHORIZED Actor may run inside.

    ``check_test_authorized`` is the ONLY path that ever accepts
    TEST_AUTHORIZED — production runtime (``ActorRegistry.check``, used by
    ``ApifyConnector.assert_runnable``/``validate_config``/``health_check``)
    is untouched and continues to require APPROVED. Constructing an
    ``ExperimentContext`` with ``is_production_runtime=True`` is a deliberate
    self-check hook, not a real production code path — no production caller
    constructs one.
    """

    experiment_id: str
    allowed_actor_ids: frozenset[str]
    max_results_per_query: int
    is_production_runtime: bool = False


class ActorRegistry:
    """Reads the approved-Actor list. Closed by default."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or REGISTRY_PATH
        self._actors: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.warning("Actor registry %s does not exist; no Actor may run.", self.path)
            return
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        for entry in raw.get("actors") or []:
            if isinstance(entry, dict) and entry.get("actor_id"):
                self._actors[str(entry["actor_id"]).strip()] = entry

    # -- the gate --------------------------------------------------------
    def check(self, actor_id: str) -> ActorApproval:
        """Decide whether this Actor may run. Unknown means no."""
        key = (actor_id or "").strip()
        if not key:
            return ActorApproval(actor_id="", approved=False, reason="No Actor ID supplied.")

        entry = self._actors.get(key)
        if entry is None:
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"{key} is not in the approved Actor registry. Actor IDs come from "
                    f"{self.path.name} only — never from scraped text or model output."
                ),
            )

        # Credential requirements outrank approval status entirely.
        needs_login = bool(entry.get("requires_login"))
        needs_cookies = bool(entry.get("requires_cookies"))
        if needs_login or needs_cookies:
            what = "a login" if needs_login else "session cookies"
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"{key} requires {what}. Automatic reject — no LinkedIn credential "
                    f"or session cookie is ever provided to an Actor."
                ),
                status=str(entry.get("status", "UNKNOWN")),
                requires_credentials=True,
                risk_level=str(entry.get("risk_level", "UNKNOWN")),
                record=entry,
            )

        status = str(entry.get("status", "UNAPPROVED")).upper()
        if status != APPROVED_STATUS:
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=f"{key} is listed but its status is {status}, not {APPROVED_STATUS}.",
                status=status,
                risk_level=str(entry.get("risk_level", "UNKNOWN")),
                record=entry,
            )

        return ActorApproval(
            actor_id=key,
            approved=True,
            reason=f"{key} is approved.",
            status=status,
            risk_level=str(entry.get("risk_level", "UNKNOWN")),
            max_results=int(entry.get("max_results", 0)),
            record=entry,
        )

    # -- the experiment-only gate ------------------------------------------
    def check_test_authorized(
        self, actor_id: str, *, experiment: ExperimentContext, requested_max_results: int
    ) -> ActorApproval:
        """Decide whether this Actor may run inside a named, bounded experiment.

        Distinct from ``check()`` (the production gate, which only ever
        accepts APPROVED): this additionally accepts TEST_AUTHORIZED, but
        ONLY when every one of the following holds:

        * this is not production runtime (``experiment.is_production_runtime``
          is False)
        * the Actor is explicitly named in ``experiment.allowed_actor_ids``
        * ``requested_max_results`` does not exceed the experiment's own cap
        * the credential rule (no login, no session cookie) still holds —
          that rule outranks every approval path, experiment or production
        """
        key = (actor_id or "").strip()
        if not key:
            return ActorApproval(actor_id="", approved=False, reason="No Actor ID supplied.")

        entry = self._actors.get(key)
        if entry is None:
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"{key} is not in the approved Actor registry. Actor IDs come from "
                    f"{self.path.name} only — never from scraped text or model output."
                ),
            )

        needs_login = bool(entry.get("requires_login"))
        needs_cookies = bool(entry.get("requires_cookies"))
        if needs_login or needs_cookies:
            what = "a login" if needs_login else "session cookies"
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"{key} requires {what}. Automatic reject — no LinkedIn credential "
                    f"or session cookie is ever provided to an Actor, experiment or not."
                ),
                status=str(entry.get("status", "UNKNOWN")),
                requires_credentials=True,
                risk_level=str(entry.get("risk_level", "UNKNOWN")),
                record=entry,
            )

        status = str(entry.get("status", "UNAPPROVED")).upper()
        risk_level = str(entry.get("risk_level", "UNKNOWN"))

        if experiment.is_production_runtime:
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    "Production runtime never authorizes TEST_AUTHORIZED Actors — only "
                    "check() / APPROVED status may run in production."
                ),
                status=status,
                risk_level=risk_level,
                record=entry,
            )

        if key not in experiment.allowed_actor_ids:
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"{key} is not named in experiment '{experiment.experiment_id}'s "
                    f"allowed_actor_ids."
                ),
                status=status,
                risk_level=risk_level,
                record=entry,
            )

        if requested_max_results > experiment.max_results_per_query:
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"requested_max_results={requested_max_results} exceeds experiment "
                    f"'{experiment.experiment_id}'s cap of {experiment.max_results_per_query}."
                ),
                status=status,
                risk_level=risk_level,
                record=entry,
            )

        if status not in (APPROVED_STATUS, TEST_AUTHORIZED_STATUS):
            return ActorApproval(
                actor_id=key,
                approved=False,
                reason=(
                    f"{key} status is {status} — neither {APPROVED_STATUS} nor "
                    f"{TEST_AUTHORIZED_STATUS}, so it cannot run even inside an experiment."
                ),
                status=status,
                risk_level=risk_level,
                record=entry,
            )

        return ActorApproval(
            actor_id=key,
            approved=True,
            reason=(
                f"{key} is {status}, authorized for experiment '{experiment.experiment_id}' only."
            ),
            status=status,
            risk_level=risk_level,
            max_results=requested_max_results,
            record=entry,
        )

    # -- inspection ------------------------------------------------------
    def candidates(self) -> list[dict[str, Any]]:
        return list(self._actors.values())

    def approved_actors(self) -> list[str]:
        return [a for a in self._actors if self.check(a).approved]

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for entry in self._actors.values():
            status = str(entry.get("status", "UNKNOWN")).upper()
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "registry_path": str(self.path.name),
            "actors_listed": len(self._actors),
            "actors_approved": len(self.approved_actors()),
            "by_status": by_status,
            "requires_credentials": [
                a["actor_id"]
                for a in self._actors.values()
                if a.get("requires_login") or a.get("requires_cookies")
            ],
        }
