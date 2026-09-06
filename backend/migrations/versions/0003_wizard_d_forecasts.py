"""Wizard D (Foresight): forecast_runs + account_forecasts

Revision ID: 0003_wizard_d_forecasts
Revises: 0002_reconcile_pre_alembic
Create Date: 2026-09-05

Two tables for docs/design/wizard-d-foresight.md §4: one row per run (portfolio
roll-up, label counts, config snapshot) and one row per account per run (the
block Wizard A embeds as journey_json['forecast']). Immutable history — a new
run never rewrites an old row. Written by hand from models.ForecastRun /
models.AccountForecast; tests/test_migrations.py holds them equal.

Guarded on has_table: the pre-Alembic path (utils/schema.migrate stamps a
create_all()-built database at 0001, then upgrades) already holds these
tables when the models that built the database included them — the same
reason 0002 uses IF NOT EXISTS.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0003_wizard_d_forecasts'
down_revision = '0002_reconcile_pre_alembic'
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'forecast_runs' not in existing:
        _create_forecast_runs()
    if 'account_forecasts' not in existing:
        _create_account_forecasts()


def _create_forecast_runs() -> None:
    op.create_table('forecast_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=50), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('vertical', sa.String(length=50), nullable=True),
    sa.Column('generator_version', sa.String(length=20), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('as_of', sa.DateTime(), nullable=False),
    sa.Column('basis_counts', sa.JSON(), nullable=False),
    sa.Column('labels', sa.JSON(), nullable=False),
    sa.Column('portfolio', sa.JSON(), nullable=False),
    sa.Column('config_snapshot', sa.JSON(), nullable=False),
    sa.Column('accounts', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.customer_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_forecast_runs_created_at'), 'forecast_runs', ['created_at'], unique=False)
    op.create_index(op.f('ix_forecast_runs_customer_id'), 'forecast_runs', ['customer_id'], unique=False)
    op.create_index(op.f('ix_forecast_runs_run_id'), 'forecast_runs', ['run_id'], unique=True)


def _create_account_forecasts() -> None:
    op.create_table('account_forecasts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=50), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('as_of', sa.DateTime(), nullable=False),
    sa.Column('basis', sa.String(length=12), nullable=False),
    sa.Column('p_retain', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('p_retain_low', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('p_retain_high', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('p_expand', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('p_expand_low', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('p_expand_high', sa.Numeric(precision=5, scale=4), nullable=False),
    sa.Column('arr', sa.Numeric(precision=15, scale=2), nullable=False),
    sa.Column('expected_arr_end', sa.Numeric(precision=15, scale=2), nullable=False),
    sa.Column('expected_arr_low', sa.Numeric(precision=15, scale=2), nullable=False),
    sa.Column('expected_arr_high', sa.Numeric(precision=15, scale=2), nullable=False),
    sa.Column('decision_point_at', sa.Date(), nullable=True),
    sa.Column('stratum', sa.String(length=40), nullable=True),
    sa.Column('n_labels', sa.Integer(), nullable=False),
    sa.Column('forecast_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.account_id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.customer_id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['forecast_runs.run_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'account_id', name='uq_account_forecast_run')
    )
    op.create_index(op.f('ix_account_forecasts_account_id'), 'account_forecasts', ['account_id'], unique=False)
    op.create_index(op.f('ix_account_forecasts_basis'), 'account_forecasts', ['basis'], unique=False)
    op.create_index(op.f('ix_account_forecasts_customer_id'), 'account_forecasts', ['customer_id'], unique=False)
    op.create_index(op.f('ix_account_forecasts_run_id'), 'account_forecasts', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_account_forecasts_run_id'), table_name='account_forecasts')
    op.drop_index(op.f('ix_account_forecasts_customer_id'), table_name='account_forecasts')
    op.drop_index(op.f('ix_account_forecasts_basis'), table_name='account_forecasts')
    op.drop_index(op.f('ix_account_forecasts_account_id'), table_name='account_forecasts')
    op.drop_table('account_forecasts')
    op.drop_index(op.f('ix_forecast_runs_run_id'), table_name='forecast_runs')
    op.drop_index(op.f('ix_forecast_runs_customer_id'), table_name='forecast_runs')
    op.drop_index(op.f('ix_forecast_runs_created_at'), table_name='forecast_runs')
    op.drop_table('forecast_runs')
