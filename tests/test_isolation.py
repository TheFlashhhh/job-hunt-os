"""The test suite must never touch jobhunt.db. See the guard in conftest.py."""

from __future__ import annotations


def test_the_suite_cannot_reach_the_real_database():
    from app.config import REPO_ROOT, get_settings
    from app.db import get_engine

    url = get_settings().jobhunt_db_url
    assert "jobhunt-pytest-" in url
    assert str(REPO_ROOT / "jobhunt.db") not in str(get_engine().url)


def test_a_session_scope_outside_any_fixture_lands_in_the_guard_database():
    """What the leaking collection thread did: open a session with nothing
    monkeypatched. It must reach the throwaway database."""
    from sqlalchemy import text

    from app.db import session_scope

    with session_scope() as session:
        path = session.execute(text("PRAGMA database_list")).all()[0][2]
    assert "jobhunt-pytest-" in path
