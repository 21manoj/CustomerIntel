"""Permanent deletion of a tenant, from the platform console.

This is the one console action that cannot be undone, so it only runs when all
of these hold:

  1. the caller is a platform operator (the route's guard);
  2. the tenant is already suspended — nobody is using it, and suspending is a
     separate, reversible step taken first;
  3. the operator typed the tenant's exact name;
  4. it is not the operator's own tenant, and no platform operator's login
     belongs to it (move or revoke them on the box first);
  5. an archive of every row about to be deleted has been written OFF the box
     (BACKUP_S3_URI — the same place deploy/backup_db.sh copies the nightly
     dumps). No archive, no deletion.

Then every row of the tenant is deleted in one transaction, and the audit log
keeps a record of who deleted what, how many rows, and where the archive is.
The tenant's earlier audit rows are kept too: tool_audit_log has no foreign key
to customers precisely so that history outlives the tenant.

What is deleted is worked out from the database itself (plan()), not from a
hand-kept list, so a table added later is either covered or makes plan() fail
loudly — tests/test_platform_admin.py pins that against the live schema.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime

KEPT = ('tool_audit_log',)              # carries customer_id, has no FK to customers: history survives the tenant
NOT_TENANT_DATA = ('alembic_version', 'worker_heartbeats')


def plan() -> list:
    """[(table, where_sql)] in a safe delete order (children first), ending with customers.
    Raises if any table can't be classified."""
    from extensions import db
    from sqlalchemy import MetaData
    meta = MetaData()
    meta.reflect(db.engine)
    direct = {t.name for t in meta.tables.values() if 'customer_id' in t.c}
    steps = []
    for t in reversed(meta.sorted_tables):
        if t.name in KEPT or t.name in NOT_TENANT_DATA:
            continue
        if t.name in direct:
            steps.append((t.name, 'customer_id = :cid'))
            continue
        parents = [fk for fk in t.foreign_keys if fk.column.table.name in direct]
        if not parents:
            raise RuntimeError(f'table {t.name} is not classified for tenant deletion: it has no customer_id and no '
                               f'foreign key to a table that has one (see platform_admin/deletion.py)')
        fk = sorted(parents, key=lambda f: f.parent.name)[0]
        steps.append((t.name, f'"{fk.parent.name}" IN (SELECT "{fk.column.name}" FROM "{fk.column.table.name}" WHERE customer_id = :cid)'))
    assert steps and steps[-1][0] == 'customers', 'customers must be deleted last'
    return steps


def _store_archive(name: str, body: bytes) -> str:
    """Write the archive off the box; return where it went. Tests replace this."""
    uri = (os.environ.get('BACKUP_S3_URI') or '').strip()
    if not uri.startswith('s3://'):
        raise ValueError('no off-box backup location is configured (BACKUP_S3_URI), so a tenant cannot be deleted from here')
    import boto3
    bucket, _, prefix = uri[5:].partition('/')
    key = f"{prefix.strip('/')}/deleted-tenants/{name}".lstrip('/')
    boto3.client('s3').put_object(Bucket=bucket, Key=key, Body=body, ServerSideEncryption='AES256')
    return f's3://{bucket}/{key}'


def delete_tenant(op, customer_id: int, confirm_name) -> dict:
    from extensions import db
    from models import Customer, User
    from sqlalchemy import text
    from platform_admin.service import _audit

    c = db.session.get(Customer, int(customer_id))
    if not c:
        raise LookupError(f'tenant {customer_id} not found')
    cid, name = c.customer_id, c.customer_name
    if op.customer_id is not None and int(op.customer_id) == cid:
        raise ValueError('you cannot delete the tenant your own login belongs to')
    if not c.suspended_at:
        raise ValueError('suspend the tenant first: only a suspended tenant can be deleted')
    if str(confirm_name or '') != name:
        raise ValueError("the name you typed does not match the tenant's name exactly")
    if User.query.filter(User.customer_id == cid, User.platform_role.isnot(None)).count():
        raise ValueError('a platform operator login belongs to this tenant; revoke or move it on the box first')

    steps = plan()
    conn = db.session.connection()
    archive = {'tenant': {'customer_id': cid, 'name': name}, 'archived_at': datetime.utcnow().isoformat() + 'Z',
               'archived_by_user_id': op.user_id, 'tables': {}}
    for table, where in steps:
        rows = conn.execute(text(f'SELECT * FROM "{table}" WHERE {where}'), {'cid': cid}).mappings().all()
        archive['tables'][table] = [dict(r) for r in rows]
    counts = {t: len(rows) for t, rows in archive['tables'].items() if rows}
    body = gzip.compress(json.dumps(archive, default=str).encode('utf-8'))
    try:
        location = _store_archive(f"tenant-{cid}-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json.gz", body)
    except ValueError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        raise ValueError(f'the archive could not be written off the box, so nothing was deleted ({type(e).__name__}: {str(e)[:160]})')

    try:
        for table, where in steps:
            conn.execute(text(f'DELETE FROM "{table}" WHERE {where}'), {'cid': cid})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    total = sum(counts.values())
    _audit(op, 'tenant_delete', cid, f'deleted tenant "{name[:80]}": {total} rows in {len(counts)} tables; archive {location}')
    return {'deleted': True, 'customer_id': cid, 'name': name, 'rows': counts, 'total_rows': total, 'archive': location}
