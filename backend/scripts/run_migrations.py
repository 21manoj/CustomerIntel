#!/usr/bin/env python3
"""
One-shot schema migration (backend scaling plan step 3) — runs as its own
compose service (customerintelv1-migrate), gated on Postgres being healthy,
before the app and worker containers start. Both of those now set
MIGRATE_AT_BOOT=false and depend on this service completing successfully
(docker compose `condition: service_completed_successfully`), so no two
containers ever race Alembic DDL against a fresh database — a real risk
once the worker became a separate process from the web app.

build_asgi_app()'s own create_schema default is unchanged: `python
server.py` and `python worker_main.py` run standalone (no compose) still
self-migrate at boot, exactly as before this split.

    DATABASE_URL=… python scripts/run_migrations.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    url = os.environ.get('DATABASE_URL')
    if not url:
        print('DATABASE_URL is required')
        return 2
    from extensions import db
    import models  # noqa: F401 — metadata for create_all's pre-Alembic path
    from utils.schema import migrate
    from mcp_server.common import get_flask_app
    app = get_flask_app()
    with app.app_context():
        result = migrate(db.engine)
    print(f'schema: {result}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
