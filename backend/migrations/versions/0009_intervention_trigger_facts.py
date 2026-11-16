"""interventions.trigger_facts — the KPI readings / profile dates a fact trigger fired on

Revision ID: 0009_intervention_trigger_facts
Revises: 0008_platform_console
Create Date: 2026-11-16

Playbook triggers can now fire on a KPI threshold or a date in the account
profile as well as on communications (config/playbook_governance.json →
fact_triggers). The facts cited by such a proposal are stored here, next to
trigger_episode_ids. NULL for every existing row and every evidence-only row.

Guarded on has_column, as 0006.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0009_intervention_trigger_facts'
down_revision = '0008_platform_console'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('interventions')}
    if 'trigger_facts' not in cols:
        op.add_column('interventions', sa.Column('trigger_facts', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('interventions', 'trigger_facts')
