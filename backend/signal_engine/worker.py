"""
Background worker — polls for un-materialized signals and runs
signal_engine.pipeline.process_pending. Optional: the MCP tool
`process_signals` and the HTTP ingest path do the same work on demand;
the worker exists so webhook-delivered signals become evidence without
anyone calling anything.

Start from server.py when SIGNAL_WORKER=true:

    from signal_engine.worker import SignalEnrichmentWorker
    SignalEnrichmentWorker().start()

v2 (2026-09-03): the old worker enriched, wrote `qualitative_signal`
nodes only for intents without a graph equivalent, and fused qualitative
components into pillar/composite health. All of that moved to
pipeline.py (roles, provenance, people) or was retired (fusion —
absolute-separation rule).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
BATCH_SIZE = 20
MAX_CONSECUTIVE_ERRORS = 5
_wake_event = threading.Event()


def notify_new_signal() -> None:
    """Wake the worker immediately (called after an ingest)."""
    _wake_event.set()


class SignalEnrichmentWorker:
    def __init__(self, poll_interval: int = POLL_INTERVAL_SECONDS, batch_size: int = BATCH_SIZE, startup_delay: int = 30):
        self._interval = poll_interval
        self._batch_size = batch_size
        self._startup_delay = startup_delay
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._consecutive_errors = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name='signal-enrichment-worker', daemon=True)
        self._thread.start()
        logger.info('Signal worker started (poll=%ds, batch=%d)', self._interval, self._batch_size)

    def stop(self) -> None:
        self._running = False
        _wake_event.set()

    def _loop(self) -> None:
        time.sleep(self._startup_delay)
        while self._running:
            try:
                count = self.process_once()
                self._consecutive_errors = 0
                if count > 0:
                    time.sleep(2)
                    continue
            except Exception as e:
                self._consecutive_errors += 1
                logger.error('Signal worker error (%d/%d): %s', self._consecutive_errors, MAX_CONSECUTIVE_ERRORS, e, exc_info=True)
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error('Signal worker: too many consecutive errors — backing off 5 minutes')
                    _wake_event.wait(timeout=300)
                    _wake_event.clear()
                    self._consecutive_errors = 0
                    continue
            _wake_event.wait(timeout=self._interval)
            _wake_event.clear()

    def process_once(self) -> int:
        """One batch inside an app context. Returns signals processed."""
        from mcp_server.common import get_flask_app
        from signal_engine.pipeline import process_pending
        app = get_flask_app()
        with app.app_context():
            res = process_pending(limit=self._batch_size)
            if res['processed']:
                logger.info('Signal worker: processed %d (structured %d, enriched %d, unclassified %d) — journeys rebuilt %d',
                            res['processed'], res['structured'], res['enriched'], res['unclassified'], res['journeys_rebuilt'])
            return res['processed']
