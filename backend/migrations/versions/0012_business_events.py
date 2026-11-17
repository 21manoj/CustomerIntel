"""business_events: the business transaction log for playbooks

Revision ID: 0012_business_events
Revises: 0011_intervention_agent_plan
Create Date: 2026-11-16

One row per playbook event (proposed, approved, sent, step done, closed with
its outcome, drafted by the agent, automation level or stop switch changed),
written in the same transaction as the change. tool_audit_log keeps recording
the same calls as the IT log; this table is what a business reader filters and
exports. Existing history is rebuilt by scripts/backfill_business_events.py,
not here: a schema revision does not read tenant data.

Guarded on has_table, as the column revisions are guarded on has_column.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0012_business_events'
down_revision = '0011_intervention_agent_plan'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('business_events'):
        return
    op.create_table(
        'business_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.customer_id'), nullable=False),
        sa.Column('event_type', sa.String(40), nullable=False),
        sa.Column('actor_kind', sa.String(12), nullable=False),
        sa.Column('actor_label', sa.String(120), nullable=False),
        sa.Column('intervention_id', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('playbook_id', sa.String(80), nullable=True),
        sa.Column('playbook_version', sa.String(20), nullable=True),
        sa.Column('step_id', sa.String(80), nullable=True),
        sa.Column('step_number', sa.Integer(), nullable=True),
        sa.Column('steps_total', sa.Integer(), nullable=True),
        sa.Column('action_class', sa.String(20), nullable=True),
        sa.Column('exposure_revenue', sa.Numeric(15, 2), nullable=True),
        sa.Column('state_before', sa.String(24), nullable=True),
        sa.Column('state_after', sa.String(24), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('trigger_key', sa.String(64), nullable=True),
        sa.Column('delivery_status', sa.String(24), nullable=True),
        sa.Column('outcome_node_id', sa.Integer(), nullable=True),
        sa.Column('outcome_type', sa.String(80), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.Column('backfilled', sa.Boolean(), nullable=False),
    )
    for col in ('at', 'customer_id', 'event_type', 'intervention_id', 'account_id', 'playbook_id'):
        op.create_index(f'ix_business_events_{col}', 'business_events', [col])
    op.create_index('idx_business_event_customer_at', 'business_events', ['customer_id', 'at'])


def downgrade() -> None:
    op.drop_table('business_events')
