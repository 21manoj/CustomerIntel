"""
backend scaling plan step 3: the signal-enrichment worker moves into its own
container (worker_main.py), split from the web process. notify_new_signal()
(a threading.Event) only reaches a same-process worker, so once split there
is no other liveness signal -- no message queue or Redis in this stack to
ask instead. models.WorkerHeartbeat is the DB row that fills that gap;
these tests cover (1) the worker actually writes it, on both the success and
error paths, and (2) /health reports it correctly across the states a reader
cares about: never run, healthy, and stale.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                  # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()

from models import Customer, Account, WorkerHeartbeat      # noqa: E402


# ---------------------------------------------------------------------------
# Worker-level: does a pass actually write the heartbeat row?
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def account():
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        c = Customer(customer_name=f'Heartbeat {tag}', email=f'hb_{tag}@t.test', domain=f'hb-{tag}.test', vertical='datacenter_v1')
        db.session.add(c)
        db.session.flush()
        a = Account(customer_id=c.customer_id, account_name='HB Account')
        db.session.add(a)
        db.session.commit()
        cid, aid = c.customer_id, a.account_id
        yield cid, aid
        db.session.remove()
        db.drop_all()


def _clear_heartbeat():
    with app.app_context():
        WorkerHeartbeat.query.delete()
        db.session.commit()


def test_process_once_writes_a_success_heartbeat(account):
    from signal_engine.worker import SignalEnrichmentWorker
    _clear_heartbeat()
    worker = SignalEnrichmentWorker(batch_size=5)

    worker.process_once()  # no pending signals -- still a real pass, still a real heartbeat

    with app.app_context():
        hb = db.session.get(WorkerHeartbeat, SignalEnrichmentWorker.NAME)
        assert hb is not None
        assert hb.last_error is None
        assert hb.consecutive_errors == 0
        assert hb.poll_interval_seconds == worker._interval
        assert (datetime.utcnow() - hb.last_pass_at).total_seconds() < 10


def test_write_heartbeat_records_an_error_pass(account):
    """Exercises the exact call _loop()'s except branch makes: increment
    _consecutive_errors first (as _loop does), then _write_heartbeat(error=...).
    This is the path a split worker process hits if process_pending raises
    mid-poll -- e.g. a DB hiccup -- and it's the only way an operator sees
    that from outside the worker's own container logs."""
    from signal_engine.worker import SignalEnrichmentWorker
    _clear_heartbeat()
    worker = SignalEnrichmentWorker()

    worker._consecutive_errors += 1
    worker._write_heartbeat(processed=0, error='simulated process_pending failure')

    with app.app_context():
        hb = db.session.get(WorkerHeartbeat, SignalEnrichmentWorker.NAME)
        assert hb is not None
        assert hb.last_error == 'simulated process_pending failure'
        assert hb.consecutive_errors == 1


def test_write_heartbeat_resets_consecutive_errors_on_a_later_success(account):
    """A success pass after failures must clear consecutive_errors -- a
    reader trusts this field to mean 'still failing right now', not 'ever
    failed'."""
    from signal_engine.worker import SignalEnrichmentWorker
    _clear_heartbeat()
    worker = SignalEnrichmentWorker()
    worker._consecutive_errors = 3
    worker._write_heartbeat(processed=0, error='still failing')

    worker._consecutive_errors = 0
    worker._write_heartbeat(processed=2, error=None)

    with app.app_context():
        hb = db.session.get(WorkerHeartbeat, SignalEnrichmentWorker.NAME)
        assert hb.last_error is None
        assert hb.consecutive_errors == 0
        assert hb.last_processed_count == 2


def test_write_heartbeat_never_raises_even_if_db_is_unreachable(account, monkeypatch):
    """_write_heartbeat is explicitly non-fatal -- a heartbeat failure must
    never take down actual signal processing."""
    from signal_engine.worker import SignalEnrichmentWorker
    import mcp_server.common as common_mod

    def _boom():
        raise RuntimeError('DB unreachable')

    monkeypatch.setattr(common_mod, 'get_flask_app', _boom)
    worker = SignalEnrichmentWorker()
    worker._write_heartbeat(processed=1, error=None)  # must not raise


# ---------------------------------------------------------------------------
# /health-level: does it report the states a reader cares about?
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def client():
    os.environ['MCP_SERVER_API_KEY'] = 'test-server-key-' + uuid.uuid4().hex
    os.environ['MCP_AUTH_REQUIRED'] = 'true'
    import mcp_server.auth as auth
    auth.MCP_SERVER_API_KEY = os.environ['MCP_SERVER_API_KEY']
    from server import build_asgi_app
    asgi_app = build_asgi_app(TEST_DB)
    from starlette.testclient import TestClient
    with TestClient(asgi_app) as c:
        yield c
    os.environ['MCP_TRANSPORT'] = 'stdio'


def test_health_reports_unknown_when_worker_never_ran(client):
    _clear_heartbeat()
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json()['worker'] == {'known': False}


def test_health_reports_healthy_for_a_fresh_heartbeat(client):
    _clear_heartbeat()
    with app.app_context():
        db.session.add(WorkerHeartbeat(
            worker_name='signal_enrichment', last_pass_at=datetime.utcnow(),
            last_processed_count=3, consecutive_errors=0, poll_interval_seconds=15,
        ))
        db.session.commit()

    w = client.get('/health').json()['worker']
    assert w['known'] is True
    assert w['stale'] is False
    assert w['last_processed_count'] == 3
    assert w['poll_interval_seconds'] == 15
    assert w['age_seconds'] < 10


def test_health_reports_stale_for_an_old_heartbeat(client):
    _clear_heartbeat()
    with app.app_context():
        db.session.add(WorkerHeartbeat(
            worker_name='signal_enrichment',
            last_pass_at=datetime.utcnow() - timedelta(seconds=1000),
            last_processed_count=0, consecutive_errors=2, poll_interval_seconds=15,
        ))
        db.session.commit()

    w = client.get('/health').json()['worker']
    assert w['known'] is True
    assert w['stale'] is True
    assert w['consecutive_errors'] == 2
