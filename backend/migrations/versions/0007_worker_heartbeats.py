"""worker_heartbeats — background-worker liveness (backend scaling plan step 3)

Revision ID: 0007_worker_heartbeats
Revises: 0006_customer_expires_at
Create Date: 2026-09-14

One row per named worker (today: only 'signal_enrichment'), upserted on every
poll pass. Added alongside splitting the signal-enrichment worker into its
own compose service: once it's a separate container, nothing reported
whether it was still alive -- no message queue or Redis exists in this stack
to ask instead, so a DB row is the natural place. /health reads it and
reports staleness relative to poll_interval_seconds.

Guarded on has_table, matching every other migration here: the pre-Alembic
path (utils/schema.migrate stamps a create_all()-built database at 0001,
then upgrades) already holds this table on any database created after this
revision landed in models.py.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0007_worker_heartbeats'
down_revision = '0006_customer_expires_at'
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'worker_heartbeats' not in tables:
        op.create_table(
            'worker_heartbeats',
            sa.Column('worker_name', sa.String(length=50), primary_key=True),
            sa.Column('last_pass_at', sa.DateTime(), nullable=False),
            sa.Column('last_processed_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('consecutive_errors', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('poll_interval_seconds', sa.Integer(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table('worker_heartbeats')
