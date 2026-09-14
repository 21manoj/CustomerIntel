"""
backend scaling plan step 2: every /api/* and /app/api/* handler ran its DB/
service call inline on the single event loop via a synchronous _with_app(fn)
-- so one slow request (a big process_pending drain, a slow query) blocked
every other request on the process, including /health, the Docker healthcheck.

_with_app is now async and runs fn() in Starlette's threadpool (an anyio
worker thread), off the loop; /health got the same treatment inline. These
tests prove the actual behavioral claim -- concurrent requests no longer
serialize behind a slow one -- not just that the code still imports.
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

SLOW_S = 1.2  # comfortably above scheduling noise, short enough to keep the suite fast


@pytest.mark.anyio
async def test_with_app_runs_concurrently_not_serially():
    """Two _with_app calls, each a synchronous time.sleep(SLOW_S), run in
    parallel threadpool workers. Serial execution would take ~2*SLOW_S;
    concurrent execution takes ~SLOW_S. This is the core claim of step 2,
    isolated from the HTTP/ASGI layer."""
    from signal_engine.http import _with_app

    def _blocking_sleep():
        time.sleep(SLOW_S)
        return 'done'

    t0 = time.monotonic()
    results = await asyncio.gather(_with_app(_blocking_sleep), _with_app(_blocking_sleep))
    elapsed = time.monotonic() - t0

    assert results == ['done', 'done']
    assert elapsed < SLOW_S * 1.6, (
        f"two _with_app calls took {elapsed:.2f}s -- expected ~{SLOW_S:.2f}s if they "
        f"ran concurrently; this looks serial (~{2 * SLOW_S:.2f}s), meaning _with_app "
        f"is blocking the event loop again"
    )


@pytest.fixture(scope='module')
def anyio_backend():
    return 'asyncio'


@pytest.fixture(scope='module')
def asgi_app():
    os.environ['MCP_SERVER_API_KEY'] = 'test-server-key-' + uuid.uuid4().hex
    os.environ['MCP_AUTH_REQUIRED'] = 'true'
    import mcp_server.auth as auth
    auth.MCP_SERVER_API_KEY = os.environ['MCP_SERVER_API_KEY']
    from server import build_asgi_app
    app = build_asgi_app(TEST_DB)
    yield app
    import mcp_server.common as _common
    from extensions import db
    with _common.get_flask_app().app_context():
        db.session.remove()
    os.environ['MCP_TRANSPORT'] = 'stdio'


@pytest.mark.anyio
async def test_health_is_not_blocked_by_a_concurrent_slow_route(asgi_app, monkeypatch):
    """End-to-end proof through the real ASGI app: a route whose service call
    is patched to block synchronously for SLOW_S must not delay a concurrent
    /health request -- /health is the Docker healthcheck, and this is exactly
    the failure mode the scaling plan named ("a long process_data would stall
    every other request, including the healthcheck").

    Uses GET /api/audit: its handler does `from mcp_server import audit` and
    calls `audit.query(...)` via dotted attribute access INSIDE the request
    (journeys/http.py), so patching mcp_server.audit.query takes effect even
    though the ASGI app (and its route closures) were already built -- unlike
    a `from x import y` name captured once at route-registration time."""
    import mcp_server.audit as audit_mod

    real_query = audit_mod.query

    def _slow_query(*args, **kwargs):
        time.sleep(SLOW_S)
        return real_query(*args, **kwargs)

    monkeypatch.setattr(audit_mod, 'query', _slow_query)

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        headers = {'Authorization': f"Bearer {os.environ['MCP_SERVER_API_KEY']}"}

        async def _slow_call():
            return await client.get('/api/audit', headers=headers)

        async def _health_call():
            await asyncio.sleep(SLOW_S * 0.3)  # let the slow call start first
            t_before = time.monotonic()
            r = await client.get('/health')
            return r, time.monotonic() - t_before

        slow_resp, (health_resp, health_elapsed) = await asyncio.gather(_slow_call(), _health_call())

        assert slow_resp.status_code == 200, slow_resp.text
        assert health_resp.status_code == 200, health_resp.text
        assert health_elapsed < SLOW_S * 0.7, (
            f"/health took {health_elapsed:.2f}s while a concurrent route was blocked for "
            f"{SLOW_S:.2f}s -- expected /health to return almost immediately; it looks "
            f"stuck behind the slow request again"
        )
