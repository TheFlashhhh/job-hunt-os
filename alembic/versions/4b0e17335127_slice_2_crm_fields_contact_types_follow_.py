"""slice 2: crm fields, contact types, follow-up kinds, parser metadata

Revision ID: 4b0e17335127
Revises: 5dab355df433
Create Date: 2026-08-13 23:58:01.029466

Autogenerate proposed these columns as NOT NULL with no default, which fails
against a database that already has rows. Each added column therefore carries a
``server_default`` for the backfill, and ``contact.relationship_type`` is
backfilled before it is tightened to NOT NULL.

The server defaults are left in place rather than dropped afterwards: SQLite
cannot drop a column default without rebuilding the table, and the defaults are
the same values the ORM would write anyway.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b0e17335127"
down_revision: Union[str, Sequence[str], None] = "5dab355df433"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing contacts predate the ContactType enum; give them OTHER before
    # the column becomes NOT NULL.
    op.execute("UPDATE contact SET relationship_type = 'OTHER' WHERE relationship_type IS NULL")

    with op.batch_alter_table("contact", schema=None) as batch_op:
        batch_op.alter_column(
            "relationship_type",
            existing_type=sa.VARCHAR(length=120),
            type_=sa.String(length=64),
            nullable=False,
            server_default="OTHER",
        )
        batch_op.create_index(
            batch_op.f("ix_contact_relationship_type"), ["relationship_type"], unique=False
        )

    with op.batch_alter_table("follow_up", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=32), nullable=False, server_default="OTHER")
        )
        batch_op.add_column(
            sa.Column("priority", sa.String(length=16), nullable=False, server_default="NORMAL")
        )
        batch_op.create_index(batch_op.f("ix_follow_up_kind"), ["kind"], unique=False)

    # Requirements stored before the parser rebuild were all bullet-derived.
    with op.batch_alter_table("job_requirement", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("extraction", sa.String(length=16), nullable=False, server_default="BULLET")
        )
        batch_op.add_column(
            sa.Column(
                "extraction_confidence", sa.Float(), nullable=False, server_default="1.0"
            )
        )

    with op.batch_alter_table("resume_recommendation", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("rank", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("requirement_text", sa.Text(), nullable=True))
        batch_op.alter_column(
            "kind",
            existing_type=sa.VARCHAR(length=64),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.create_index(
            batch_op.f("ix_resume_recommendation_kind"), ["kind"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("resume_recommendation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_resume_recommendation_kind"))
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(length=32),
            type_=sa.VARCHAR(length=64),
            existing_nullable=False,
        )
        batch_op.drop_column("requirement_text")
        batch_op.drop_column("rank")

    with op.batch_alter_table("job_requirement", schema=None) as batch_op:
        batch_op.drop_column("extraction_confidence")
        batch_op.drop_column("extraction")

    with op.batch_alter_table("follow_up", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_follow_up_kind"))
        batch_op.drop_column("priority")
        batch_op.drop_column("kind")

    with op.batch_alter_table("contact", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contact_relationship_type"))
        batch_op.alter_column(
            "relationship_type",
            existing_type=sa.String(length=64),
            type_=sa.VARCHAR(length=120),
            nullable=True,
            server_default=None,
        )
