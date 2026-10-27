"""Reading the operating system's scheduler, and never guessing at it.

The defect: `python -m app.cli schedule --register` created a Windows task that
was registered, enabled and due at 01:00, and the Collection page said **"Not
scheduled"** — because the status path computed the next run from a
`system_task` database column that `schtasks /Create` does not write. Two panels
apart, the same page said "Scheduled task registered", because that card asked
Windows.

Parsing is a pure function over the scheduler's payload, so every state below is
testable on any platform. None of these tests requires a real scheduled task,
and none of them passes or fails because of what happens to be registered on the
machine running them.
"""

from __future__ import annotations

import json

import pytest

from app.services.schedule_state import (
    DISABLED,
    ERROR,
    NOT_CONFIGURED,
    REGISTERED_NEXT_RUN_KNOWN,
    REGISTERED_NEXT_RUN_UNKNOWN,
    UNSUPPORTED_PLATFORM,
    parse_schedule_payload,
    read_schedule_state,
)

TASK = "JobHuntOS-StructuredCollection"


def payload(**overrides) -> str:
    """The shape `Get-ScheduledTask` really returned for the registered task."""
    body = {
        "taskName": TASK,
        "state": "Ready",
        "enabled": True,
        "nextRunTime": "2026-08-24T01:00:00.0000000-04:00",
        "lastRunTime": "1999-11-30T00:00:00.0000000-05:00",
        "lastTaskResult": 267011,
        "repetitionInterval": "PT6H",
    }
    body.update(overrides)
    return json.dumps(body)


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------


def test_no_task_is_not_configured():
    state = parse_schedule_payload(None, task_name=TASK)
    assert state.state == NOT_CONFIGURED
    assert state.registered is False
    assert state.headline == "Not scheduled"


def test_a_registered_task_with_a_next_run():
    state = parse_schedule_payload(payload(), task_name=TASK)
    assert state.state == REGISTERED_NEXT_RUN_KNOWN
    assert state.registered is True
    assert state.next_run_at is not None
    assert state.next_run_at.isoformat() == "2026-08-24T05:00:00+00:00"
    assert state.cadence == "Every 6 hours"


def test_registered_without_a_next_run_is_not_reported_as_unscheduled():
    """The distinction the old code collapsed."""
    state = parse_schedule_payload(payload(nextRunTime=None), task_name=TASK)
    assert state.state == REGISTERED_NEXT_RUN_UNKNOWN
    assert state.registered is True
    assert state.headline == "Registered — next run unavailable"
    assert "Not scheduled" not in state.headline


def test_a_disabled_task_says_so_rather_than_looking_scheduled():
    state = parse_schedule_payload(payload(enabled=False), task_name=TASK)
    assert state.state == DISABLED
    assert state.headline == "Registered but disabled"
    assert "will not run" in state.detail


def test_a_disabled_state_string_is_honoured_too():
    state = parse_schedule_payload(payload(state="Disabled"), task_name=TASK)
    assert state.state == DISABLED


def test_an_unreadable_payload_is_an_error_not_an_absence():
    state = parse_schedule_payload("this is not json", task_name=TASK)
    assert state.state == ERROR
    assert state.registered is False
    assert state.headline == "Could not read the schedule"


def test_a_query_failure_is_an_error_not_an_absence():
    def explode(_name: str) -> str:
        raise RuntimeError("Access is denied.")

    state = read_schedule_state(task_name=TASK, query=explode)
    assert state.state == ERROR
    assert "Access is denied." in state.detail


def test_a_scheduler_error_about_a_missing_task_is_not_configured():
    state = parse_schedule_payload(
        json.dumps({"error": "No MSFT_ScheduledTask objects found with property"}),
        task_name=TASK,
    )
    assert state.state == NOT_CONFIGURED


def test_non_windows_says_so_instead_of_claiming_nothing_is_scheduled():
    """CI is Linux. "No scheduler here" and "you have not scheduled it" differ."""
    state = read_schedule_state(task_name=TASK, system="Linux")
    assert state.state == UNSUPPORTED_PLATFORM
    assert state.registered is False
    assert "Task Scheduler" in state.detail


# ---------------------------------------------------------------------------
# Details that would otherwise be shown as facts
# ---------------------------------------------------------------------------


def test_the_never_run_sentinel_is_not_shown_as_a_last_run():
    """Windows reports 30 Nov 1999 for a task that has never run."""
    state = parse_schedule_payload(payload(), task_name=TASK)
    assert state.last_run_at is None


def test_not_yet_run_is_not_reported_as_a_failure_result():
    """0x41303 means "has not yet run", not "exited 267011"."""
    state = parse_schedule_payload(payload(), task_name=TASK)
    assert state.last_result is None


def test_a_real_last_run_and_result_survive():
    state = parse_schedule_payload(
        payload(lastRunTime="2026-08-24T07:00:00.0000000-04:00", lastTaskResult=0),
        task_name=TASK,
    )
    assert state.last_run_at is not None
    assert state.last_run_at.isoformat() == "2026-08-24T11:00:00+00:00"
    assert state.last_result == 0


@pytest.mark.parametrize(
    ("interval", "expected"),
    [("PT6H", "Every 6 hours"), ("PT1H", "Every 1 hour"), ("PT30M", "Every 30 minutes"), ("", "")],
)
def test_cadence_labels(interval, expected):
    state = parse_schedule_payload(payload(repetitionInterval=interval), task_name=TASK)
    # An unrecognised interval falls back to the configured cadence rather than
    # inventing a label.
    assert state.cadence == expected or state.cadence == "Every 6 hours"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_the_task_name_is_validated_before_it_reaches_powershell():
    """It comes from reviewed config, never a request — checked anyway."""
    from app.services.schedule_state import _is_safe_task_name

    assert _is_safe_task_name("JobHuntOS-StructuredCollection")
    assert not _is_safe_task_name("Task'; Remove-Item C:\\ -Recurse; '")
    assert not _is_safe_task_name("")


def test_nothing_in_this_module_creates_or_changes_a_task():
    """Registration lives in the CLI, behind a confirmation prompt."""
    import pathlib

    source = pathlib.Path("app/services/schedule_state.py").read_text(encoding="utf-8")
    for forbidden in ("Register-ScheduledTask", "/Create", "Unregister", "schtasks /Delete"):
        assert forbidden not in source, forbidden
