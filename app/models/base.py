"""Declarative base and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so Alembic autogenerate produces stable names
# and a future PostgreSQL migration does not fight anonymous constraints.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Timezone-aware UTC now. Used for every timestamp in the system."""
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Force a datetime to be timezone-aware UTC.

    SQLite has no native timestamp type, so a ``DateTime(timezone=True)``
    column round-trips as a *naive* datetime even though an aware one was
    written. Comparing a freshly-created object's timestamp (aware) with a
    reloaded one (naive) raises TypeError, and which one you get depends on
    whether the row happens to be in the identity map — so the bug appears only
    on the second request. Every comparison against :func:`utcnow` goes through
    here. Stored values are always UTC, so attaching UTC is correct, not a
    guess.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def new_id(prefix: str) -> str:
    """Readable, sortable-enough surrogate key, e.g. ``JOB-3f9a2c81``."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
