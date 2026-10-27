"""Running a collection, from the browser or the CLI, one at a time.

Collection reaches out to fifty-odd other people's servers and writes to the
real database. That is why it lived behind the CLI. The cost of that was real
too: the only way to answer "is this list current?" was to open a terminal, so
in practice the data went stale.

This module keeps the safety and drops the terminal requirement:

**One at a time, across processes.** A ``system_task`` row is the lock, so a
second click — or a CLI run started in another window, or a Task Scheduler
wake-up landing on top of a manual run — sees ``RUNNING`` and is refused rather
than double-polling every board. A row, not an in-process lock: the browser and
the scheduled task are different processes.

**A stale lock expires.** A process killed mid-sweep would otherwise leave the
button dead forever. A ``RUNNING`` row older than the configured ceiling is
treated as abandoned and taken over, and that takeover is recorded.

**The browser does not block on it.** The last full sweep took just over fifty
minutes, so ``start()`` returns as soon as the lock is claimed and the page
polls status. The CLI, which has a terminal to sit in, takes the same lock with
``claim()`` and runs the sweep itself so it can print per-company progress.

Nothing here changes what collection *does*. It is the same ``CollectionService``
with the same connectors and the same content-hash gate.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.db import session_scope
from app.models.base import as_utc, utcnow
from app.models.misc import SystemTask
from app.services.collection_status import STRUCTURED_TASK, load_policy

logger = logging.getLogger("jobhunt.collection.runner")

IDLE = "IDLE"
RUNNING = "RUNNING"
FAILED = "FAILED"

#: A run still marked RUNNING after this long is assumed to have died with its
#: process. Generous: the last full sweep took 50 minutes.
STALE_LOCK_HOURS = 3.0

#: Guards the *claim*, not the collection. Two requests arriving in the same
#: millisecond in one process must not both read IDLE.
_CLAIM_LOCK = threading.Lock()


@dataclass
class StartResult:
    started: bool
    reason: str = ""
    detail: str = ""
    task: dict[str, Any] | None = None


def task_state() -> dict[str, Any]:
    with session_scope() as session:
        return _task_dict(session.get(SystemTask, STRUCTURED_TASK))


def start(
    *,
    lane: str | None = None,
    company_name: str | None = None,
    triggered_by: str = "browser",
    limit: int | None = None,
) -> StartResult:
    """Claim the lock and run a sweep on a background thread."""
    claimed, refusal = _claim(triggered_by=triggered_by, lane=lane, company_name=company_name)
    if not claimed:
        return refusal

    threading.Thread(
        target=_run_and_release,
        kwargs={
            "lane": lane,
            "company_name": company_name,
            "triggered_by": triggered_by,
            "limit": limit,
        },
        name="jobhunt-collection",
        daemon=True,
    ).start()
    return refusal


def claim(
    *, triggered_by: str, lane: str | None = None, company_name: str | None = None
) -> tuple[bool, StartResult]:
    """Take the shared lock for a run this caller will drive itself.

    The CLI uses this rather than :func:`start`: it has a terminal to sit in, so
    it runs the sweep inline and prints per-company progress. What it must not
    do is poll every board while the browser is already doing exactly that.
    """
    return _claim(triggered_by=triggered_by, lane=lane, company_name=company_name)


def release(
    *, triggered_by: str, summary: dict[str, Any] | None = None, error: str | None = None
) -> None:
    """Hand the lock back. Always called, including after a failure."""
    _release(triggered_by=triggered_by, summary=summary, error=error)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def _claim(
    *, triggered_by: str, lane: str | None, company_name: str | None
) -> tuple[bool, StartResult]:
    with _CLAIM_LOCK, session_scope() as session:
        task = session.get(SystemTask, STRUCTURED_TASK)
        if task is None:
            task = SystemTask(id=STRUCTURED_TASK, name=STRUCTURED_TASK, status=IDLE, enabled=True)
            session.add(task)
            session.flush()

        if task.status == RUNNING and not _lock_is_stale(task):
            started = as_utc(task.last_attempted_run)
            by = (task.detail or {}).get("triggered_by") if task.detail else None
            return False, StartResult(
                started=False,
                reason="ALREADY_RUNNING",
                detail=(
                    "A collection is already running"
                    + (f" (started by {by}" if by else " (")
                    + (f" at {started:%Y-%m-%d %H:%M} UTC)" if started else ")")
                    + ". Only one runs at a time, so the same boards are not polled twice."
                ),
                task=_task_dict(task),
            )

        takeover = task.status == RUNNING
        task.status = RUNNING
        task.last_attempted_run = utcnow()
        task.detail = {
            "triggered_by": triggered_by,
            "lane": lane,
            "company": company_name,
            "took_over_stale_lock": takeover,
        }
        session.flush()
        return True, StartResult(started=True, task=_task_dict(task))


def _release(
    *, triggered_by: str, summary: dict[str, Any] | None, error: str | None
) -> None:
    with session_scope() as session:
        task = session.get(SystemTask, STRUCTURED_TASK)
        if task is None:
            return
        finished = utcnow()
        if error is None and summary is not None:
            task.status = IDLE
            if summary.get("status") in ("COMPLETED", "PARTIAL"):
                task.last_successful_run = finished
            task.detail = {
                "triggered_by": triggered_by,
                "finished_at": finished.isoformat(),
                "result": {
                    key: summary.get(key)
                    for key in (
                        "status",
                        "companies",
                        "jobs_new",
                        "jobs_changed",
                        "jobs_unchanged",
                        "jobs_failed",
                        "jobs_screened_out",
                    )
                },
                "errors": (summary.get("errors") or [])[:20],
            }
        else:
            task.status = FAILED
            task.detail = {
                "triggered_by": triggered_by,
                "finished_at": finished.isoformat(),
                "error": error,
            }
        session.flush()


def _lock_is_stale(task: SystemTask) -> bool:
    started = as_utc(task.last_attempted_run)
    if started is None:
        return True
    ceiling = float(
        (load_policy().get("freshness") or {}).get("stale_lock_hours") or STALE_LOCK_HOURS
    )
    return utcnow() - started > timedelta(hours=ceiling)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _collect(
    *, lane: str | None, company_name: str | None, triggered_by: str, limit: int | None
) -> tuple[dict[str, Any] | None, str | None]:
    from app.services.collection import CollectionService

    try:
        with session_scope() as session:
            result = CollectionService(session, triggered_by=triggered_by).collect_watchlist(
                lane=lane,
                company_name=company_name,
                limit=limit,
            )
            return result.as_dict(), None
    except Exception as exc:  # noqa: BLE001 — a failed sweep must release the lock
        logger.exception("Collection run failed")
        return None, str(exc)


def _run_and_release(
    *, lane: str | None, company_name: str | None, triggered_by: str, limit: int | None
) -> None:
    summary, error = _collect(
        lane=lane, company_name=company_name, triggered_by=triggered_by, limit=limit
    )
    _release(triggered_by=triggered_by, summary=summary, error=error)


def _task_dict(task: SystemTask | None) -> dict[str, Any]:
    if task is None:
        return {"status": IDLE, "running": False, "detail": None}
    return {
        "status": task.status,
        "running": task.status == RUNNING,
        "enabled": task.enabled,
        "schedule": task.schedule,
        "last_attempted_run": _iso(as_utc(task.last_attempted_run)),
        "last_successful_run": _iso(as_utc(task.last_successful_run)),
        "detail": task.detail,
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None
