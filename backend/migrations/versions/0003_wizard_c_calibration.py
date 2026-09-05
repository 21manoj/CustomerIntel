"""Wizard C — calibration proposals + who set the weights

Revision ID: 0003_wizard_c_calibration
Revises: 0002_reconcile_pre_alembic
Create Date: 2026-09-05

Adds weight_calibrations (one row per Wizard C proposal) and
customer_configs.weights_origin (vertical_default | customer_config |
wizard_c — who set pillar_weights / kpi_weights, read by the scorer and
stamped on HealthScore.weight_source). Back-fills the origin of existing
rows: customized_by set → a person did it; otherwise weights present → the
only writer in this build was create_customer's tier default.

Idempotent: a database that create_all() built from the current models (the
pre-Alembic path utils/schema.migrate stamps at the baseline) already has the
column and the table; each statement checks before it acts.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0003_wizard_c_calibration'
down_revision = '0002_reconcile_pre_alembic'
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if 'weights_origin' not in {c['name'] for c in insp.get_columns('customer_configs')}:
        op.add_column('customer_configs', sa.Column('weights_origin', sa.String(length=24), nullable=True))
    op.execute("UPDATE customer_configs SET weights_origin = 'customer_config' "
               "WHERE weights_origin IS NULL AND customized_by IS NOT NULL AND (pillar_weights IS NOT NULL OR kpi_weights IS NOT NULL)")
    op.execute("UPDATE customer_configs SET weights_origin = 'vertical_default' "
               "WHERE weights_origin IS NULL AND (pillar_weights IS NOT NULL OR kpi_weights IS NOT NULL)")
    if 'weight_calibrations' in insp.get_table_names():
        return
    op.create_table('weight_calibrations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('vertical', sa.String(length=50), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('method_version', sa.String(length=20), nullable=False),
    sa.Column('catalog_version', sa.String(length=20), nullable=True),
    sa.Column('config_snapshot', sa.JSON(), nullable=False),
    sa.Column('outcome_counts', sa.JSON(), nullable=False),
    sa.Column('outcome_node_ids', sa.JSON(), nullable=False),
    sa.Column('current_pillar_weights', sa.JSON(), nullable=False),
    sa.Column('current_kpi_weights', sa.JSON(), nullable=False),
    sa.Column('proposed_pillar_weights', sa.JSON(), nullable=False),
    sa.Column('proposed_kpi_weights', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.Column('impact', sa.JSON(), nullable=False),
    sa.Column('proposed_at', sa.DateTime(), nullable=False),
    sa.Column('proposed_by', sa.String(length=120), nullable=True),
    sa.Column('proposed_by_key_id', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('decided_by', sa.String(length=120), nullable=True),
    sa.Column('decided_by_key_id', sa.Integer(), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('applied_config_version', sa.String(length=20), nullable=True),
    sa.Column('recompute', sa.JSON(), nullable=True),
    sa.Column('superseded_by', sa.Integer(), nullable=True),
    sa.Column('notes', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.customer_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_weight_calibration_customer_state', 'weight_calibrations', ['customer_id', 'state'], unique=False)
    op.create_index(op.f('ix_weight_calibrations_customer_id'), 'weight_calibrations', ['customer_id'], unique=False)
    op.create_index(op.f('ix_weight_calibrations_proposed_at'), 'weight_calibrations', ['proposed_at'], unique=False)
    op.create_index(op.f('ix_weight_calibrations_state'), 'weight_calibrations', ['state'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_weight_calibrations_state'), table_name='weight_calibrations')
    op.drop_index(op.f('ix_weight_calibrations_proposed_at'), table_name='weight_calibrations')
    op.drop_index(op.f('ix_weight_calibrations_customer_id'), table_name='weight_calibrations')
    op.drop_index('idx_weight_calibration_customer_state', table_name='weight_calibrations')
    op.drop_table('weight_calibrations')
    op.drop_column('customer_configs', 'weights_origin')
