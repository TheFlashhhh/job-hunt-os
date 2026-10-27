"""Is collection actually scheduled, and when does it next run?

`python -m app.cli schedule --register` creates a Windows Task Scheduler entry.
That writes state to the operating system and nothing to this application's
database — so the Collection page, which computed "next scheduled" from a
`system_task` row nobody ever filled in, reported **"Not scheduled"** for a task
that was registered, enabled, and due to run at 01:00. The Scheduling card two
panels down said "Scheduled task registered" at the same time, because it asked
Windows.

The fix is to ask the operating system, which is where the answer lives.

**Read-only, and never from user input.** The task name is application-controlled
(`collection_policy.yaml`), the query is a fixed argument vector with no shell,
and nothing here creates, changes or deletes a task. Registration stays in the
CLI, behind a confirmation prompt.

**"Registered but we could not read the next run time" is not "not scheduled".**
Those are different facts and the state machine keeps them apart, because
telling someone nothing is scheduled when something is would send them to
re-register a task they already have.
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import load_yaml_config
from app.models.base import as_utc

logger = logging.getLogger("jobhunt.schedule")

NOT_CONFIGURED = "NOT_CONFIGURED"
REGISTERED_NEXT_RUN_KNOWN = "REGISTERED_NEXT_RUN_KNOWN"
REGISTERED_NEXT_RUN_UNKNOWN = "REGISTERED_NEXT_RUN_UNKNOWN"
DISABLED = "DISABLED"
ERROR = "ERROR"
UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"

#: Windows reports this for a task that has never run. It is a sentinel, not a
#: date, and showing it as "last run: 30 Nov 1999" would be worse than nothing.
_NEVER_RUN_SENTINEL_YEAR = 1999

#: 0x41303 — "the task has not yet run". Not a failure.
_NOT_YET_RUN_RESULT = 267011

_QUERY_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class ScheduleState:
    """What the operating system says about the collection task."""

    state: str
    task_name: str
    scheduler: str = "Windows Task Scheduler"
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_result: int | None = None
    cadence: str = ""
    detail: str = ""

    @property
    def registered(self) -> bool:
        return self.state in (
            REGISTERED_NEXT_RUN_KNOWN,
            REGISTERED_NEXT_RUN_UNKNOWN,
            DISABLED,
        )

    @property
    def headline(self) -> str:
        """What the Collection page shows where a next run time would go."""
        if self.state == REGISTERED_NEXT_RUN_KNOWN and self.next_run_at is not None:
            return self.next_run_at.astimezone().strftime("%Y-%m-%d %H:%M local")
        if self.state == REGISTERED_NEXT_RUN_UNKNOWN:
            return "Registered — next run unavailable"
        if self.state == DISABLED:
            return "Registered but disabled"
        if self.state == ERROR:
            return "Could not read the schedule"
        if self.state == UNSUPPORTED_PLATFORM:
            return "No scheduler on this platform"
        return "Not scheduled"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "task_name": self.task_name,
            "scheduler": self.scheduler,
            "registered": self.registered,
            "headline": self.headline,
            "next_run_at": _iso(self.next_run_at),
            "next_run_local": (
                self.next_run_at.astimezone().strftime("%Y-%m-%d %H:%M")
                if self.next_run_at
                else None
            ),
            "last_run_at": _iso(self.last_run_at),
            "last_result": self.last_result,
            "cadence": self.cadence,
            "detail": self.detail,
        }


#: Injected in tests. Returns the raw JSON payload the query produced, or None
#: when the task does not exist.
QueryFn = Callable[[str], str | None]


def read_schedule_state(
    *, task_name: str | None = None, query: QueryFn | None = None, system: str | None = None
) -> ScheduleState:
    """Ask the scheduler about the collection task. Never modifies anything."""
    policy = load_yaml_config("collection_policy.yaml")
    name = task_name or str(
        (policy.get("windows_task") or {}).get("task_name")
        or "JobHuntOS-StructuredCollection"
    )
    cadence = _cadence_label(policy)

    if query is None:
        current = system or platform.system()
        if current != "Windows":
            return ScheduleState(
                state=UNSUPPORTED_PLATFORM,
                task_name=name,
                scheduler=f"{current} — no Task Scheduler",
                cadence=cadence,
                detail=(
                    "Scheduling is Windows Task Scheduler on this machine. On another "
                    "platform, use its own scheduler to run "
                    "`python -m app.cli collect`."
                ),
            )
        query = _query_windows

    try:
        payload = query(name)
    except Exception as exc:  # noqa: BLE001 — a schedule read must never 500 a page
        logger.warning("Could not read scheduled task %s: %s", name, exc)
        return ScheduleState(state=ERROR, task_name=name, cadence=cadence, detail=str(exc))

    return parse_schedule_payload(payload, task_name=name, cadence=cadence)


def parse_schedule_payload(
    payload: str | None, *, task_name: str, cadence: str = ""
) -> ScheduleState:
    """Turn the scheduler's JSON into a state. Pure, so it is testable anywhere."""
    if not payload or not payload.strip():
        return ScheduleState(
            state=NOT_CONFIGURED,
            task_name=task_name,
            cadence=cadence,
            detail="No scheduled task with this name exists.",
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return ScheduleState(
            state=ERROR,
            task_name=task_name,
            cadence=cadence,
            detail=f"Scheduler returned something unreadable: {exc}",
        )

    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ScheduleState(
            state=ERROR, task_name=task_name, cadence=cadence, detail="Unexpected payload shape."
        )
    if data.get("error"):
        message = str(data["error"])
        return ScheduleState(
            state=NOT_CONFIGURED if _means_no_such_task(message) else ERROR,
            task_name=task_name,
            cadence=cadence,
            detail=message,
        )

    next_run = _parse_stamp(data.get("nextRunTime"))
    last_run = _parse_stamp(data.get("lastRunTime"))
    if last_run is not None and last_run.year <= _NEVER_RUN_SENTINEL_YEAR:
        last_run = None

    result = data.get("lastTaskResult")
    last_result = int(result) if isinstance(result, (int, float)) else None
    if last_result == _NOT_YET_RUN_RESULT:
        last_result = None

    interval = _interval_label(str(data.get("repetitionInterval") or ""))
    enabled = data.get("enabled")
    state_text = str(data.get("state") or "").strip().lower()

    if enabled is False or state_text == "disabled":
        return ScheduleState(
            state=DISABLED,
            task_name=task_name,
            next_run_at=next_run,
            last_run_at=last_run,
            last_result=last_result,
            cadence=interval or cadence,
            detail="The task exists but is disabled, so it will not run.",
        )

    if next_run is None:
        return ScheduleState(
            state=REGISTERED_NEXT_RUN_UNKNOWN,
            task_name=task_name,
            last_run_at=last_run,
            last_result=last_result,
            cadence=interval or cadence,
            detail=(
                "The task is registered and enabled; the scheduler did not report a "
                "next run time."
            ),
        )

    return ScheduleState(
        state=REGISTERED_NEXT_RUN_KNOWN,
        task_name=task_name,
        next_run_at=next_run,
        last_run_at=last_run,
        last_result=last_result,
        cadence=interval or cadence,
    )


#: How the scheduler says "there is no such task". Captured from the real
#: message rather than guessed: PowerShell answers a missing task with
#: "No MSFT_ScheduledTask objects found with property 'TaskName' equal to …",
#: which contains neither the word "not" nor "exist".
_NO_SUCH_TASK_MARKERS = (
    "objects found with property",
    "cannot find",
    "does not exist",
    "not found",
    "no mapping between",
)


def _means_no_such_task(message: str) -> bool:
    """An absent task is a different answer from a broken query.

    Getting this wrong in either direction is bad: reporting ERROR for a task
    nobody registered hides a normal state behind an alarm, and reporting
    NOT_CONFIGURED for a permissions failure tells the user to register a task
    they already have.
    """
    lowered = message.lower()
    return any(marker in lowered for marker in _NO_SUCH_TASK_MARKERS)


# ---------------------------------------------------------------------------
# The Windows query
# ---------------------------------------------------------------------------

#: `Get-ScheduledTask` returns objects with stable property names, and the
#: timestamps are formatted to ISO-8601 with an offset here rather than left to
#: `ConvertTo-Json`, which renders a PS 5.1 DateTime as `/Date(…)/`. Parsing
#: `schtasks /FO LIST` instead would mean parsing localised field names and a
#: locale-dependent date order — this machine prints `24-08-2026`.
_POWERSHELL_QUERY = """
$ErrorActionPreference = 'Stop'
try {{
    $task = Get-ScheduledTask -TaskName '{name}'
    $info = Get-ScheduledTaskInfo -TaskName '{name}'
    [pscustomobject]@{{
        taskName = $task.TaskName
        state = [string]$task.State
        enabled = [bool]$task.Settings.Enabled
        nextRunTime = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('o') }} else {{ $null }}
        lastRunTime = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('o') }} else {{ $null }}
        lastTaskResult = $info.LastTaskResult
        repetitionInterval = [string]$task.Triggers[0].Repetition.Interval
    }} | ConvertTo-Json -Compress
}} catch {{
    [pscustomobject]@{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
}}
"""


def _query_windows(task_name: str) -> str | None:
    """Read one application-controlled task. Fixed argv, no shell, no writes."""
    if not _is_safe_task_name(task_name):
        raise ValueError(f"Refusing to query an unexpected task name: {task_name!r}")

    completed = subprocess.run(  # noqa: S603 — fixed argv, application-controlled name
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _POWERSHELL_QUERY.format(name=task_name),
        ],
        capture_output=True,
        text=True,
        timeout=_QUERY_TIMEOUT_SECONDS,
        check=False,
    )
    output = (completed.stdout or "").strip()
    if not output:
        detail = (completed.stderr or "").strip()
        raise RuntimeError(detail or "The scheduler query returned nothing.")
    return output


def _is_safe_task_name(name: str) -> bool:
    """The name comes from reviewed config, never from a request.

    Checked anyway: this string is interpolated into a PowerShell command, and
    a config file is still a file somebody could edit.
    """
    return bool(name) and all(ch.isalnum() or ch in "-_ " for ch in name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _interval_label(iso_duration: str) -> str:
    """`PT6H` → `Every 6 hours`. Anything unrecognised yields nothing."""
    text = iso_duration.strip().upper()
    if not text.startswith("PT"):
        return ""
    body = text[2:]
    if body.endswith("H") and body[:-1].isdigit():
        hours = int(body[:-1])
        return f"Every {hours} hour{'s' if hours != 1 else ''}"
    if body.endswith("M") and body[:-1].isdigit():
        minutes = int(body[:-1])
        return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
    return ""


def _cadence_label(policy: dict[str, Any]) -> str:
    structured = (policy.get("cadences") or {}).get("STRUCTURED_ATS_REFRESH") or {}
    hours = structured.get("interval_hours")
    return f"Every {int(hours)} hours" if hours else ""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
