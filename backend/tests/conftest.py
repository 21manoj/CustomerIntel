"""
Session defaults for the suite.

MCP_TRANSPORT: in-process tool calls in fixtures/tests are the local,
trusted path (stdio semantics). The deploy container exports
MCP_TRANSPORT=http for the server; that must not leak into tests that call
tools directly — since keyed tools over HTTP are denied without a key.
Tests that exercise the HTTP surface set 'http' themselves
(server.build_asgi_app does it) and reset it on teardown.

DATABASE_URL: every test module resolves its own Flask app through
mcp_server.common.get_flask_app() (the same accessor cs_pulse_onboarding.py
and friends use), which is a single process-wide singleton keyed off this
env var. Defaulted here, once, before any test module is collected, so
every module — whichever the test runner happens to import first — builds
that ONE app from the SAME URI instead of racing to set it themselves.

MCP_AUTH_REQUIRED: same race, different trigger. mcp_server.auth.MCP_AUTH_REQUIRED
is a module-level constant computed once, at mcp_server.auth's own first
import, from os.environ.get('MCP_AUTH_REQUIRED', 'true'). get_flask_app()
calls load_dotenv() on its own first call, which -- if MCP_AUTH_REQUIRED
isn't already set -- loads it from backend/.env (a local dev convenience
file with MCP_AUTH_REQUIRED=false, so a developer doesn't need a key for
manual testing). Whichever happens first across the whole suite --
mcp_server.auth's own import, or some earlier test's get_flask_app() call
loading that 'false' from the local .env -- decides the value for every
test in the process; setting os.environ['MCP_AUTH_REQUIRED']='true'
inside an individual test's fixture (several already do) cannot fix this,
since the module constant was already computed by the time that fixture
runs. Found 2026-09-14 chasing a test that only failed as part of the full
suite, never alone. Defaulted here for the same reason as DATABASE_URL:
before any test module -- and so before mcp_server.auth or get_flask_app()
-- is ever imported, so the .env file's local-dev override never wins the
race against pytest's own default of "auth is required in tests."
"""
import os

import pytest

os.environ.setdefault('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
os.environ.setdefault('MCP_AUTH_REQUIRED', 'true')


@pytest.fixture(scope='session', autouse=True)
def _local_transport_for_in_process_tools():
    prev = os.environ.get('MCP_TRANSPORT')
    os.environ['MCP_TRANSPORT'] = 'stdio'
    yield
    if prev is None:
        os.environ.pop('MCP_TRANSPORT', None)
    else:
        os.environ['MCP_TRANSPORT'] = prev
