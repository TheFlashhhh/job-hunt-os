"""Application CRM. Status history is append-only and never overwritten."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id, utcnow
from app.models.enums import ApplicationStatus

if TYPE_CHECKING:
    from app.models.ingestion import Job


class Application(Base, TimestampMixin):
    __tablename__ = "application"

    application_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("APP")
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job.job_id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    current_status: Mapped[str] = mapped_column(
        String(32), default=ApplicationStatus.DISCOVERED.value, index=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_version_id: Mapped[str | None] = mapped_column(String(64))
    referral_used: Mapped[bool] = mapped_column(Boolean, default=False)
    referral_contact_id: Mapped[str | None] = mapped_column(String(64))
    next_action: Mapped[str | None] = mapped_column(String(500))
    next_action_due: Mapped[date | None] = mapped_column(Date)
    outcome: Mapped[str | None] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    job: Mapped[Job] = relationship(back_populates="application")
    status_history: Mapped[list[ApplicationStatusHistory]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationStatusHistory.changed_at",
    )
    notes: Mapped[list[ApplicationNote]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ApplicationStatusHistory(Base):
    """Append-only. Funnel history is never overwritten (architecture §38)."""

    __tablename__ = "application_status_history"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ASH"))
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.application_id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    changed_by: Mapped[str] = mapped_column(String(32), default="USER")
    note: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JSON)

    application: Mapped[Application] = relationship(back_populates="status_history")


class ApplicationNote(Base):
    __tablename__ = "application_note"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("NOTE"))
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.application_id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    author: Mapped[str] = mapped_column(String(32), default="USER")

    application: Mapped[Application] = relationship(back_populates="notes")
