"""Database engine and session management.

SQLite for the local MVP. The ORM layer is written so a PostgreSQL migration
stays practical: no SQLite-specific column types, no raw SQL in services.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Enable WAL, foreign keys and a busy timeout on SQLite connections."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # SQLite allows one writer. Without this, a concurrent write raises
        # "database is locked" immediately instead of waiting its turn. Thirty
        # seconds rather than ten because a collection sweep writes in batches:
        # a request arriving mid-batch should wait for the batch, not fail.
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


#: SQLAlchemy's default pool is 5 connections with 10 overflow. Rendering one
#: job page fires a dozen parallel API calls from the Next.js server, and a
#: POST alongside that walked straight into
#: "QueuePool limit of size 5 overflow 10 reached" — a 500 in the browser on a
#: database this machine owns exclusively. SQLite connections are cheap and
#: local, so the pool is sized for the page's real fan-out.
_SQLITE_POOL_SIZE = 20
_SQLITE_MAX_OVERFLOW = 30


def _sqlite_engine_kwargs(url: str) -> dict[str, object]:
    if not url.startswith("sqlite"):
        return {}
    return {
        "pool_size": _SQLITE_POOL_SIZE,
        "max_overflow": _SQLITE_MAX_OVERFLOW,
        "pool_timeout": 30,
        # Recycle rather than hold a connection open indefinitely.
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        # uvicorn serves requests from a thread pool; each gets its own
        # connection from the pool above, so this is safe here.
        "connect_args": {"check_same_thread": False},
    }


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine, _SessionFactory
    if url is not None:
        engine = create_engine(url, echo=echo, future=True, **_sqlite_engine_kwargs(url))
        if engine.dialect.name == "sqlite":
            event.listen(engine, "connect", _configure_sqlite)
        return engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.jobhunt_db_url,
            echo=echo,
            future=True,
            **_sqlite_engine_kwargs(settings.jobhunt_db_url),
        )
        if _engine.dialect.name == "sqlite":
            event.listen(_engine, "connect", _configure_sqlite)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope around a series of operations."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_engine() -> None:
    """Test helper — drop cached engine/session factory."""
    global _engine, _SessionFactory
    _engine = None
    _SessionFactory = None
