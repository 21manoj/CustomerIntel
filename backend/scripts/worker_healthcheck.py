#!/usr/bin/env python3
"""
Docker HEALTHCHECK for the standalone signal-enrichment worker container
(backend scaling plan step 3). Reads models.WorkerHeartbeat directly (no
HTTP surface on this container to ask instead) and exits 0 if the worker's
last pass is recent, 1 if it looks stalled or dead.

No row yet (fresh container, first pass hasn't landed) is treated as
healthy — this script's own judgment matches the same generous
interval * 3 grace period /health uses, so `docker ps` and /health never
disagree about what "stale" means.

    DATABASE_URL=… python scripts/worker_healthcheck.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    if not os.environ.get('DATABASE_URL'):
        print('DATABASE_URL is required')
        return 1
    from mcp_server.common import get_flask_app
    from models import WorkerHeartbeat
    from signal_engine.worker import SignalEnrichmentWorker

    app = get_flask_app()
    try:
        with app.app_context():
            hb = WorkerHeartbeat.query.get(SignalEnrichmentWorker.NAME)
    except Exception as e:
        # A missing table here means the worker started before migration
        # completed -- depends_on: condition: service_completed_successfully
        # on customerintelv1-migrate is supposed to prevent exactly that, so
        # treat any query failure (unreachable DB included) as unhealthy
        # rather than guessing.
        print(f'heartbeat query failed: {e}')
        return 1

    if hb is None:
        print('no heartbeat yet — treating as healthy (startup grace)')
        return 0

    interval = hb.poll_interval_seconds or 60
    age_s = (datetime.utcnow() - hb.last_pass_at).total_seconds()
    stale = age_s > max(interval * 3, 180)
    print(f'last pass {age_s:.0f}s ago (poll_interval={interval}s, stale={stale}, '
          f'consecutive_errors={hb.consecutive_errors})')
    return 1 if stale else 0


if __name__ == '__main__':
    sys.exit(main())
