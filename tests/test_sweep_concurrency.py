"""A write made during a sweep must get in, not starve.

This runs against a real SQLite FILE in a temp directory, with WAL and the
application's own connection settings, because the failure it guards against
only exists there: SQLite has no queue for its write lock, so a sweep that
re-takes the lock the instant it commits can starve a waiting writer for its
whole busy timeout even though every transaction is short.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from sqlalchemy.orm import sessionmaker

from app.connectors.base import CollectionResult, SourceConfig
from app.db import get_engine
from app.models import Base
from app.services.collection import CollectionService
from app.services.pipeline import RawJobInput
from app.services.watchlist import WatchlistLoadResult

BODY = (
    "We are hiring an analyst to build dashboards in SQL and Python for the "
    "operations team. Requirements: SQL, Python, communication. "
)


class _BigBoard:
    connector_id = "greenhouse"
    version = "0.1.0"
    capability = type("Cap", (), {"value": "DOCUMENTED_PUBLIC_API"})()

    def __init__(self, company: str, count: int) -> None:
        self.company = company
        self.count = count

    def collect(self, config, limit=None):  # noqa: ANN001
        records = [
            RawJobInput(
                company_name=config.company_name,
                title=f"Operations Analyst {i}",
                description=BODY * 3 + f" Posting {i}.",
                location="Boston, MA",
                external_id=f"{config.company_name}-{i}",
                connector="greenhouse",
                source_type="GREENHOUSE",
            )
            for i in range(self.count)
        ]
        return CollectionResult(
            config=config, records=records, records_received=len(records), records_valid=len(records)
        )

    def close(self) -> None:
        return None


def _sweep_and_probe(tmp_path, *, yield_seconds: float) -> tuple[list[float], int]:
    url = f"sqlite:///{(tmp_path / 'sweep.db').as_posix()}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    watchlist = WatchlistLoadResult(
        companies=[
            SourceConfig(
                company_name=name,
                ats_type="GREENHOUSE",
                ats_identifier=name.lower(),
                active=True,
                automated_collection_enabled=True,
            )
            for name in ("Alpha", "Beta", "Gamma", "Delta")
        ]
    )
    service = CollectionService(
        session, connector_factory=lambda _t, **_k: _BigBoard("x", 120), triggered_by="test"
    )
    service.commit_every_records = 10
    service.yield_after_commit_seconds = yield_seconds

    done = threading.Event()

    def sweep() -> None:
        try:
            service.collect_watchlist(watchlist=watchlist, analyze=False)
        finally:
            session.close()
            done.set()

    thread = threading.Thread(target=sweep)
    thread.start()
    # A second connection, like a CRM save from the web app, with a short wait.
    probe = sqlite3.connect(tmp_path / "sweep.db", timeout=1.0, isolation_level=None)
    waits: list[float] = []
    failures = 0
    time.sleep(0.3)
    while not done.is_set():
        started = time.monotonic()
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            waits.append(time.monotonic() - started)
        except sqlite3.OperationalError:
            failures += 1
        time.sleep(0.05)
    thread.join()
    probe.close()
    engine.dispose()
    return waits, failures


def test_a_write_during_a_sweep_gets_the_lock(tmp_path):
    waits, failures = _sweep_and_probe(tmp_path, yield_seconds=0.05)
    assert waits, "the probe never ran during the sweep"
    assert failures == 0, f"{failures} write(s) starved while the sweep ran"
    assert max(waits) < 1.0



def test_a_hung_board_is_abandoned_and_the_sweep_moves_on(session):
    """A request that never returns must not stall the sweep (or its lock)."""
    from app.models.enums import JobLifecycleStatus  # noqa: F401  (import check)

    release = threading.Event()

    class _Hangs:
        connector_id = "greenhouse"
        version = "0.1.0"
        capability = type("Cap", (), {"value": "DOCUMENTED_PUBLIC_API"})()

        def __init__(self) -> None:
            self.closed = False

        def collect(self, config, limit=None):  # noqa: ANN001
            if config.company_name == "Stuck":
                release.wait(10)
            return CollectionResult(config=config, records=[], records_received=0, records_valid=0)

        def close(self) -> None:
            self.closed = True

    watchlist = WatchlistLoadResult(
        companies=[
            SourceConfig(company_name=n, ats_type="GREENHOUSE", ats_identifier=n.lower(), active=True, automated_collection_enabled=True)
            for n in ("Stuck", "Fine")
        ]
    )
    service = CollectionService(session, connector_factory=lambda _t, **_k: _Hangs())
    service.board_deadline_seconds = 0.5
    started = time.monotonic()
    summary = service.collect_watchlist(watchlist=watchlist, include_discovered=False, analyze=False)
    release.set()
    by_name = {o.config.company_name: o for o in summary.outcomes}
    assert time.monotonic() - started < 5
    assert by_name["Stuck"].status == "FAILED"
    assert "Timed out" in by_name["Stuck"].errors[0]
    assert by_name["Fine"].ok
