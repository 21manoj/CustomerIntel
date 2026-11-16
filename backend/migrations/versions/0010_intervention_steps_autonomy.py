"""interventions: multi-step plans and autonomy levels

Revision ID: 0010_intervention_steps_autonomy
Revises: 0009_intervention_trigger_facts
Create Date: 2026-11-16

A playbook may now declare an ordered list of steps, and a tenant's automation
level may let policy approve more than notifications (config/playbook_governance.json
→ autonomy). The row carries the plan (steps, step_index), why its approval was
policy's or a person's (approval_policy), and the review-afterwards marker for
actions policy approved that a person still has to look at (review_required,
reviewed_at, reviewed_by). All NULL for every existing row.

Guarded on has_column, as 0006 and 0009.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0010_intervention_steps_autonomy'
down_revision = '0009_intervention_trigger_facts'
branch_labels = None
depends_on = None

COLUMNS = [
    ('steps', sa.JSON()),
    ('step_index', sa.Integer()),
    ('approval_policy', sa.JSON()),
    ('review_required', sa.Boolean()),
    ('reviewed_at', sa.DateTime()),
    ('reviewed_by', sa.String(length=120)),
]


def upgrade() -> None:
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('interventions')}
    for name, type_ in COLUMNS:
        if name not in cols:
            op.add_column('interventions', sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column('interventions', name)
