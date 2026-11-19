"""customer_configs.analysis_settings: a tenant's overrides of analysis settings

Revision ID: 0013_analysis_settings
Revises: 0012_business_events
Create Date: 2026-11-19

One JSON column, keyed by analysis. The first key is realized_retention (the
hindsight retention window, the starting-revenue basis and the thresholds below
which the figure is withheld); wizards/wizard_b_settings.py validates what goes
in against config/wizard_b.json. NULL means the config's defaults.

Guarded on has_column, as the other column revisions are.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0013_analysis_settings'
down_revision = '0012_business_events'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = [c['name'] for c in sa.inspect(op.get_bind()).get_columns('customer_configs')]
    if 'analysis_settings' not in cols:
        op.add_column('customer_configs', sa.Column('analysis_settings', sa.JSON(), nullable=True))


def downgrade() -> None:
    cols = [c['name'] for c in sa.inspect(op.get_bind()).get_columns('customer_configs')]
    if 'analysis_settings' in cols:
        op.drop_column('customer_configs', 'analysis_settings')
