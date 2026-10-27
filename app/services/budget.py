"""Budget enforcement.

Warning → notify. Hard limit → pause OPTIONAL automated work and surface
``BUDGET_LIMIT_REACHED``. Never silently exceed; never disable local
application tracking, manual import, or the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.models.misc import AIUsage, ApifyRun

BUDGET_LIMIT_REACHED = "BUDGET_LIMIT_REACHED"


class BudgetState(str, Enum):
    OK = "OK"
    WARNING = "WARNING"
    LIMIT_REACHED = "LIMIT_REACHED"


#: Work that must keep running even at a hard limit.
NEVER_PAUSED = frozenset(
    {
        "manual_import",
        "application_tracking",
        "dashboard",
        "channel_a_structured_collection",
        "evidence_bank_access",
    }
)


@dataclass
class BudgetStatus:
    provider: str
    spent_usd: float
    warning_usd: float
    hard_usd: float
    state: BudgetState
    message: str

    @property
    def should_pause_optional(self) -> bool:
        return self.state == BudgetState.LIMIT_REACHED

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "spent_usd": round(self.spent_usd, 4),
            "warning_usd": self.warning_usd,
            "hard_usd": self.hard_usd,
            "state": self.state.value,
            "message": self.message,
            "should_pause_optional": self.should_pause_optional,
        }


def _config() -> dict[str, Any]:
    return load_yaml_config("budgets.yaml").get("budgets", {})


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.config = _config()

    # -- spend ------------------------------------------------------------
    def claude_spend_this_month(self, now: datetime | None = None) -> float:
        total = self.session.execute(
            select(func.coalesce(func.sum(AIUsage.estimated_cost_usd), 0.0)).where(
                AIUsage.started_at >= _month_start(now)
            )
        ).scalar_one()
        return float(total or 0.0)

    def apify_spend_this_month(self, now: datetime | None = None) -> float:
        total = self.session.execute(
            select(func.coalesce(func.sum(ApifyRun.estimated_cost_usd), 0.0)).where(
                ApifyRun.started_at >= _month_start(now)
            )
        ).scalar_one()
        return float(total or 0.0)

    def apify_runs_today(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(
            self.session.execute(
                select(func.count()).select_from(ApifyRun).where(ApifyRun.started_at >= start)
            ).scalar_one()
        )

    # -- status -----------------------------------------------------------
    def status(self, provider: str, now: datetime | None = None) -> BudgetStatus:
        cfg = self.config.get(provider, {}) or {}
        warning = float(cfg.get("warning_usd_per_month", 0.0) or 0.0)
        hard = float(cfg.get("hard_usd_per_month", 0.0) or 0.0)
        spent = (
            self.claude_spend_this_month(now)
            if provider == "claude"
            else self.apify_spend_this_month(now)
        )

        if hard and spent >= hard:
            return BudgetStatus(
                provider,
                spent,
                warning,
                hard,
                BudgetState.LIMIT_REACHED,
                (
                    f"{BUDGET_LIMIT_REACHED}: {provider} spend ${spent:.2f} reached the "
                    f"${hard:.2f} monthly limit. Optional automated work is paused. "
                    f"Manual import, application tracking and the dashboard continue."
                ),
            )
        if warning and spent >= warning:
            return BudgetStatus(
                provider,
                spent,
                warning,
                hard,
                BudgetState.WARNING,
                (
                    f"{provider} spend ${spent:.2f} passed the ${warning:.2f} warning "
                    f"threshold. Nothing is paused; high-value interactive analysis continues."
                ),
            )
        return BudgetStatus(
            provider, spent, warning, hard, BudgetState.OK, f"{provider} spend ${spent:.2f}."
        )

    # -- gates -------------------------------------------------------------
    def may_run_optional(self, provider: str, now: datetime | None = None) -> bool:
        """False once the hard limit is reached."""
        return not self.status(provider, now).should_pause_optional

    def may_run_apify(self, now: datetime | None = None) -> tuple[bool, str]:
        """Combined monthly-spend and daily-run-count gate."""
        status = self.status("apify", now)
        if status.should_pause_optional:
            return False, status.message
        cfg = self.config.get("apify", {}) or {}
        max_runs = int(cfg.get("max_runs_per_day", 0) or 0)
        if max_runs:
            used = self.apify_runs_today(now)
            if used >= max_runs:
                return False, (
                    f"{BUDGET_LIMIT_REACHED}: {used}/{max_runs} Apify runs already used today."
                )
        return True, status.message

    def max_records_per_run(self) -> int:
        cfg = self.config.get("apify", {}) or {}
        return int(cfg.get("max_records_per_run", 250) or 250)

    @staticmethod
    def is_never_paused(work: str) -> bool:
        return work in NEVER_PAUSED

    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "claude": self.status("claude", now).as_dict(),
            "apify": self.status("apify", now).as_dict(),
            "apify_runs_today": self.apify_runs_today(now),
            "apify_max_runs_per_day": (self.config.get("apify", {}) or {}).get("max_runs_per_day"),
            "apify_max_records_per_run": self.max_records_per_run(),
        }
