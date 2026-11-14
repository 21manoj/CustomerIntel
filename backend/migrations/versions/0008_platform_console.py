"""platform console — operator flag on users, suspension on customers

Revision ID: 0008_platform_console
Revises: 0007_worker_heartbeats
Create Date: 2026-11-14

users.platform_role: NULL for every tenant user; 'operator' or 'viewer' marks a
person who runs the platform (docs/design/platform-console.md). Kept apart from
users.role on purpose — see app_api/auth.py for the leak that came from letting
a tenant role double as a platform one.

customers.suspended_at / suspended_reason: a suspended tenant's keys and
sessions are refused until it is reactivated. Nothing is deleted.

Guarded per column, matching every other migration here.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0008_platform_console'
down_revision = '0007_worker_heartbeats'
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    user_cols = {c['name'] for c in insp.get_columns('users')}
    if 'platform_role' not in user_cols:
        op.add_column('users', sa.Column('platform_role', sa.String(length=20), nullable=True))
    cust_cols = {c['name'] for c in insp.get_columns('customers')}
    if 'suspended_at' not in cust_cols:
        op.add_column('customers', sa.Column('suspended_at', sa.DateTime(), nullable=True))
    if 'suspended_reason' not in cust_cols:
        op.add_column('customers', sa.Column('suspended_reason', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'suspended_reason')
    op.drop_column('customers', 'suspended_at')
    op.drop_column('users', 'platform_role')
