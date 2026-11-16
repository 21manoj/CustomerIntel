"""interventions: the playbook agent's draft

Revision ID: 0011_intervention_agent_plan
Revises: 0010_intervention_steps_autonomy
Create Date: 2026-11-16

The playbook agent (playbooks/agent.py) plans and drafts for a proposed
intervention. Its output is kept in one column of its own, so that writing a
draft cannot touch state, steps, approvals or levels. NULL for every existing row.

Guarded on has_column, as 0006, 0009 and 0010.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0011_intervention_agent_plan'
down_revision = '0010_intervention_steps_autonomy'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('interventions')}
    if 'agent_plan' not in cols:
        op.add_column('interventions', sa.Column('agent_plan', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('interventions', 'agent_plan')
