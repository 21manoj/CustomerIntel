"""customers.expires_at — time-bounded tenants (clone_customer's ttl_minutes)

Revision ID: 0006_customer_expires_at
Revises: 0005_user_tenant_scope_backfill
Create Date: 2026-09-09

Same precedent as ContextNode/ContextEdge.expires_at (idx_ctx_node_tier_expires,
models.py ~line 362/442): NULL = never expires (every existing tenant, and every
normal create_customer call), non-NULL = a deadline. Written by clone_customer
when called with ttl_minutes, for the future "temporary demo clone" flow. This
migration only adds the column and its index — nothing in this codebase reads
expires_at to actually sweep/delete a tenant yet (no scheduler/cron/Celery
exists here at all); that enforcement is separate, later work.

Guarded on has_column, matching 0002/0003's has_table guard: the pre-Alembic
path (utils/schema.migrate stamps a create_all()-built database at 0001, then
upgrades) already holds this column on any database created after this
revision landed in models.py.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0006_customer_expires_at'
down_revision = '0005_user_tenant_scope_backfill'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('customers')}
    if 'expires_at' not in cols:
        op.add_column('customers', sa.Column('expires_at', sa.DateTime(), nullable=True))
        op.create_index(op.f('ix_customers_expires_at'), 'customers', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_customers_expires_at'), table_name='customers')
    op.drop_column('customers', 'expires_at')
