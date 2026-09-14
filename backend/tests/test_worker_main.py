"""worker_main.py's env-var toggles, and SignalEnrichmentWorker's join()/
is_alive() added for it (backend scaling plan step 3)."""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import worker_main  # noqa: E402


def test_migrate_at_boot_defaults_true(monkeypatch):
    monkeypatch.delenv('MIGRATE_AT_BOOT', raising=False)
    assert worker_main._migrate_at_boot_enabled() is True


def test_migrate_at_boot_honors_false(monkeypatch):
    monkeypatch.setenv('MIGRATE_AT_BOOT', 'false')
    assert worker_main._migrate_at_boot_enabled() is False


def test_signal_worker_defaults_true(monkeypatch):
    monkeypatch.delenv('SIGNAL_WORKER', raising=False)
    assert worker_main._signal_worker_enabled() is True


def test_signal_worker_honors_false(monkeypatch):
    monkeypatch.setenv('SIGNAL_WORKER', 'false')
    assert worker_main._signal_worker_enabled() is False


def test_worker_join_and_is_alive_before_start():
    from signal_engine.worker import SignalEnrichmentWorker
    w = SignalEnrichmentWorker()
    assert w.is_alive() is False
    w.join(timeout=0.1)  # must not raise when never started


def test_worker_join_and_is_alive_after_start_stop():
    from signal_engine.worker import SignalEnrichmentWorker
    w = SignalEnrichmentWorker(poll_interval=0, startup_delay=0)
    w.start()
    assert w.is_alive() is True
    w.stop()
    w.join(timeout=5)
    assert w.is_alive() is False
