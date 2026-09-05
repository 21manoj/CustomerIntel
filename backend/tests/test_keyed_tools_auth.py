"""
Over HTTP, only the onboarding tools are frictionless. Every other tool
(read surface, review, outcomes, Ask AI, signal engine) requires a key:
the server key, or a customer key scoped to that customer with the scope
the tool needs. Found 2026-09-04 by the governance pass: these tools had
been added to the frictionless set and were reachable anonymously.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from flask import Flask                                   # noqa: E402
from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common                       # noqa: E402
_common._flask_app = app


@pytest.fixture(scope='module')
def keys(monkeypatch_module=None):
    from models import Customer
    from api_key_service import generate_api_key
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:6]
        c1 = Customer(customer_name=f'K1 {tag}', email=f'k1_{tag}@t.test', domain=f'k1-{tag}.test')
        c2 = Customer(customer_name=f'K2 {tag}', email=f'k2_{tag}@t.test', domain=f'k2-{tag}.test')
        db.session.add_all([c1, c2]); db.session.commit()
        read_key, _ = generate_api_key(c1.customer_id, created_by=None, name='r', scopes=['read'])
        write_key, _ = generate_api_key(c1.customer_id, created_by=None, name='w', scopes=['write'])
        yield {'c1': c1.customer_id, 'c2': c2.customer_id, 'read': read_key, 'write': write_key}
        db.session.remove()
        db.drop_all()


def _with(key, fn):
    import mcp_server.auth as auth
    tok = auth._current_api_key_var.set(key or '')
    try:
        with app.app_context():
            return fn()
    finally:
        auth._current_api_key_var.reset(tok)


@pytest.fixture(autouse=True)
def http_transport(monkeypatch):
    import mcp_server.auth as auth
    monkeypatch.setenv('MCP_TRANSPORT', 'http')
    monkeypatch.setattr(auth, 'MCP_AUTH_REQUIRED', True)
    monkeypatch.setattr(auth, 'MCP_SERVER_API_KEY', 'srv-' + uuid.uuid4().hex)
    yield


def test_registry_partition_is_complete():
    from mcp_server.onboarding_tool_registry import ONBOARDING_TOOLS, KEYED_TOOLS, ALL_TOOLS
    assert not (ONBOARDING_TOOLS & KEYED_TOOLS)
    for t in ('get_journey', 'list_journeys', 'get_evidence', 'get_review_queue', 'review_signal', 'log_outcome', 'ask',
              'submit_signal', 'process_signals', 'configure_signal_engine'):
        assert t in KEYED_TOOLS, t
    # every tool the server registers is classified
    import re
    src = (BACKEND / 'mcp_server' / 'cs_pulse_onboarding.py').read_text() + (BACKEND / 'mcp_server' / 'cs_pulse_adapters.py').read_text()
    registered = set(re.findall(r"_require_auth_if_key_present\('([a-z_]+)'", src))
    assert registered <= ALL_TOOLS, registered - ALL_TOOLS


def test_keyed_tool_without_a_key_is_denied_and_audited(keys):
    from fastmcp.exceptions import ToolError
    from mcp_server.auth import require_auth_if_key_present
    from mcp_server import audit
    for tool in ('get_journey', 'review_signal', 'log_outcome', 'ask'):
        with pytest.raises(ToolError, match='requires an API key'):
            _with(None, lambda: require_auth_if_key_present(tool, keys['c1']))
    with app.app_context():
        rows = audit.query(keys['c1'], tool='ask', outcome='denied')
        assert rows and rows[0]['detail'] == 'key required' and rows[0]['key_kind'] == 'none'


def test_onboarding_tool_stays_frictionless(keys):
    from mcp_server.auth import require_auth_if_key_present
    assert _with(None, lambda: require_auth_if_key_present('list_verticals', None)) is None
    assert _with(None, lambda: require_auth_if_key_present('upload_csv', keys['c1'])) is None


def test_server_key_and_scoped_customer_key(keys):
    import mcp_server.auth as auth
    from fastmcp.exceptions import ToolError
    from mcp_server.auth import require_auth_if_key_present
    assert _with(auth.MCP_SERVER_API_KEY, lambda: require_auth_if_key_present('get_journey', keys['c1'])) is None
    rec = _with(keys['read'], lambda: require_auth_if_key_present('get_journey', keys['c1']))
    assert rec is not None and rec.customer_id == keys['c1']
    with pytest.raises(ToolError, match='does not have access'):
        _with(keys['read'], lambda: require_auth_if_key_present('get_journey', keys['c2']))
    with pytest.raises(ToolError, match="write"):
        _with(keys['read'], lambda: require_auth_if_key_present('review_signal', keys['c1']))     # read key, write tool
    rec = _with(keys['write'], lambda: require_auth_if_key_present('log_outcome', keys['c1']))
    assert rec is not None
    with pytest.raises(ToolError, match='Invalid or expired'):
        _with('csp_read_bogus_' + 'x' * 30, lambda: require_auth_if_key_present('get_journey', keys['c1']))
