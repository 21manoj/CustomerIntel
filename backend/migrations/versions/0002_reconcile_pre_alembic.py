"""reconcile a pre-Alembic database with the models

Revision ID: 0002_reconcile_pre_alembic
Revises: 0001_baseline
Create Date: 2026-09-05

The EC2 database was built by create_all() plus two boot-time ALTER helpers
(now deleted). Compared with the models it differed in four places, found by
scripts/schema_check.py before the first Alembic deploy: two columns the helper
created as JSONB where the model says JSON, and two indexes the helper never
created. Every statement here is a no-op on a database created at 0001.
"""
from __future__ import annotations

from alembic import op

revision = '0002_reconcile_pre_alembic'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE qualitative_signals ALTER COLUMN extractions TYPE JSON USING extractions::json')
    op.execute('ALTER TABLE qualitative_signals ALTER COLUMN attributes TYPE JSON USING attributes::json')
    op.execute('CREATE INDEX IF NOT EXISTS ix_qualitative_signals_content_hash ON qualitative_signals (content_hash)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_kpi_measurements_upload_id ON kpi_measurements (upload_id)')


def downgrade() -> None:
    pass    # nothing to undo: the upgrade only brought a drifted database to what 0001 already describes
