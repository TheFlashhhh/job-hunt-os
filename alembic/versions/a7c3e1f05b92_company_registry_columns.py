"""company registry columns

Discovery Coverage V2: the ``company`` table becomes the operational company
registry. ``companies.yaml`` stays the curated core seed and is still read
directly; these columns hold what the application decides at runtime — how a
company entered, whether it is polled, how sure we are of its careers source,
and what its last poll saw.

All columns are nullable and additive. The only data written is a backfill of
companies already monitored from the browser, read from the
``source_metadata.watchlist_source`` marker those rows already carry.

Revision ID: a7c3e1f05b92
Revises: 6df99d803d36
Create Date: 2026-10-16 05:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e1f05b92'
down_revision: Union[str, Sequence[str], None] = '6df99d803d36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('company', schema=None) as batch_op:
        batch_op.add_column(sa.Column('registry_source', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('monitoring_status', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('source_verification', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('discovered_from_job_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('last_jobs_seen', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('last_new_jobs', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_company_registry_source'), ['registry_source'], unique=False)
        batch_op.create_index(batch_op.f('ix_company_monitoring_status'), ['monitoring_status'], unique=False)

    # Companies monitored from the browser before the registry existed. Their
    # board was read from a real posting, so the source is CONFIRMED; whether
    # they are polled follows the existing on_watchlist flag.
    op.execute(
        """
        UPDATE company
           SET registry_source = 'DISCOVERED',
               source_verification = 'CONFIRMED',
               monitoring_status = CASE WHEN on_watchlist = 1 AND active = 1
                                        THEN 'ACTIVE' ELSE 'PAUSED' END
         WHERE json_extract(source_metadata, '$.watchlist_source') = 'DISCOVERED'
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('company', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_company_monitoring_status'))
        batch_op.drop_index(batch_op.f('ix_company_registry_source'))
        batch_op.drop_column('last_new_jobs')
        batch_op.drop_column('last_jobs_seen')
        batch_op.drop_column('discovered_from_job_id')
        batch_op.drop_column('source_verification')
        batch_op.drop_column('monitoring_status')
        batch_op.drop_column('registry_source')
