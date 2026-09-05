#!/usr/bin/env python3
"""
Is the database at the Alembic head, and does it match the models? Exit 1 if not.
Runs in deploy_ec2.sh after /health; run it by hand after any hotfix on the box.

    DATABASE_URL=… python scripts/schema_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from sqlalchemy import create_engine
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata
    from extensions import db
    import models  # noqa: F401
    from utils.schema import current_revision, head_revision
    url = os.environ.get('DATABASE_URL')
    if not url:
        print('DATABASE_URL is required'); return 2
    eng = create_engine(url)
    cur, head = current_revision(eng), head_revision()
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), db.metadata)
    print(f'revision: current={cur} head={head}')
    if cur != head:
        print('NOT AT HEAD'); return 1
    if diff:
        print(f'{len(diff)} difference(s) between the database and the models — write a revision:')
        for d in diff:
            print('  ', d if not isinstance(d, list) else d[0])
        return 1
    print('schema matches the models at head')
    return 0


if __name__ == '__main__':
    sys.exit(main())
