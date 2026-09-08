"""back-fill users.allowed_customer_ids — every existing row was unscoped

Revision ID: 0005_user_tenant_scope_backfill
Revises: 0004_wizard_c_calibration
Create Date: 2026-09-08

Data-only, no DDL. `users.allowed_customer_ids` has existed since the baseline
and NOTHING ever wrote it: neither create_customer's admin user nor
app_api/users.invite set it, so every row on every database is NULL. Paired
with app_api/auth.py's old `cids is None` = unrestricted (and its blanket
`role == 'admin'` short-circuit), that made every logged-in user of every
tenant a platform superuser over the session-cookie /app/api/* surface —
reproduced live: another tenant's portfolio, accounts, interventions, ROI,
calibrations, playbook config, its user list, and a working password-setup
token for its admin.

Both creation paths now set the column, and allows_customer() is fail-closed.
Fail-closed is what makes this revision load-bearing rather than cosmetic:
without it every user who predates the fix — every user on the EC2 box —
would be locked out of their OWN tenant, not merely fenced out of others.

Back-fills each user to their own tenant (`users.customer_id`, the FK to
customers), which is exactly the scope they should have had all along. Rows
already carrying a scope are left alone (re-runnable, and it must never
narrow a scope an admin set by hand). Rows with a NULL customer_id are left
NULL: there is no tenant to grant, and fail-closed is the right answer for a
row we cannot place.

Not a script: the rows needing this are on a running box, and a one-off
script is one nobody runs. `alembic upgrade head` already runs at boot
(utils/schema.migrate) and in deploy_ec2.sh.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0005_user_tenant_scope_backfill'
down_revision = '0004_wizard_c_calibration'
branch_labels = None
depends_on = None

# Declared locally rather than imported from models.py: a revision must keep
# describing the schema as it was when it ran, even after the model moves on.
users = sa.table('users',
                 sa.column('user_id', sa.Integer),
                 sa.column('customer_id', sa.Integer),
                 sa.column('allowed_customer_ids', sa.JSON))


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(users.c.user_id, users.c.customer_id).where(
            users.c.allowed_customer_ids.is_(None),
            users.c.customer_id.isnot(None),
        )
    ).fetchall()
    for user_id, customer_id in rows:
        # One statement per row (a users table is small, and the value is
        # per-row) — bound through the JSON column type so the driver does the
        # serialising and the cast, rather than hand-built JSON in a string.
        bind.execute(
            users.update().where(users.c.user_id == user_id).values(allowed_customer_ids=[int(customer_id)])
        )


def downgrade() -> None:
    """Deliberately not reversible. Undoing this would re-open the cross-tenant
    leak on every row, and there is no record of which rows were NULL before —
    the pre-image was 'all of them', which is the bug, not a state to restore."""
    pass
