#!/usr/bin/env python3
"""
Standalone entrypoint for the signal-enrichment worker (backend scaling plan
step 3) — its own container, split from the web process. `uvicorn asgi:app
--workers N` (the planned step 5) never calls server.main(), which is what
used to start the worker thread, so under any multi-worker web deployment it
would otherwise silently vanish and webhook-delivered signals would stop
becoming evidence. This process exists precisely so that can't happen.

Env:
  DATABASE_URL                required, same as the web process
  MIGRATE_AT_BOOT             true|false (default true) — run Alembic here at
                               startup. In the compose topology this is set to
                               false on both the app and worker services; a
                               dedicated one-shot migrate service
                               (scripts/run_migrations.py) owns it instead, so
                               two containers never race DDL against a fresh
                               database. Left true by default so `python
                               worker_main.py` run standalone (no compose)
                               still self-migrates, matching server.py's
                               own default for `python server.py`.
  SIGNAL_WORKER               true|false (default true) — if false, this
                               process stays up (so `restart: unless-stopped`
                               doesn't loop it) but never starts the worker
                               thread. Matches the semantics SIGNAL_WORKER had
                               on the web service before this split.
  SIGNAL_WORKER_POLL_INTERVAL override config/signal_engine.json's
                               worker.poll_interval_seconds. Worth lowering
                               once the worker is a separate process:
                               pipeline.ingest()'s notify_new_signal() only
                               wakes a same-process worker, so a split worker
                               relies on this interval alone for latency from
                               ingest to evidence.
"""
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)


def _migrate_at_boot_enabled() -> bool:
    return os.environ.get('MIGRATE_AT_BOOT', 'true').lower() in ('true', '1', 'yes')


def _signal_worker_enabled() -> bool:
    return os.environ.get('SIGNAL_WORKER', 'true').lower() in ('true', '1', 'yes')


def main() -> None:
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get('DATABASE_URL'):
        raise SystemExit('DATABASE_URL is required')

    if _migrate_at_boot_enabled():
        from extensions import db
        from utils.schema import migrate
        from mcp_server.common import get_flask_app
        app = get_flask_app()
        with app.app_context():
            logger.info('schema: %s', migrate(db.engine))

    if not _signal_worker_enabled():
        logger.warning('SIGNAL_WORKER=false — worker process staying up idle (no signal draining will happen)')
        threading.Event().wait()  # block forever; a container that exits under restart:unless-stopped would loop
        return

    from signal_engine.worker import SignalEnrichmentWorker
    poll_interval = os.environ.get('SIGNAL_WORKER_POLL_INTERVAL')
    worker = SignalEnrichmentWorker(poll_interval=int(poll_interval) if poll_interval else None)
    worker.start()
    logger.info('Signal-enrichment worker process up (pid=%d)', os.getpid())

    worker.join()
    # _loop() catches every Exception itself, so reaching here means the
    # thread returned without stop() having been called -- unexpected. Exit
    # non-zero so `restart: unless-stopped` gets a fresh attempt rather than
    # a container that looks up but has silently stopped doing anything.
    raise SystemExit('worker thread exited unexpectedly')


if __name__ == '__main__':
    main()
