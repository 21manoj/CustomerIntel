"""
Pilot blocker 4: a leaked key could not be rotated or revoked (revoke_api_key
existed in api_key_service but nothing exposed it), and a key sent as ?api_key=
was written to uvicorn's access log on every request.
"""
import logging
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app               # noqa: E402
app = get_flask_app()


@pytest.fixture(scope='module')
def tenants():
    from models import Customer
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:6]
        c1 = Customer(customer_name=f'R1 {tag}', email=f'r1_{tag}@t.test', domain=f'r1-{tag}.test')
        c2 = Customer(customer_name=f'R2 {tag}', email=f'r2_{tag}@t.test', domain=f'r2-{tag}.test')
        db.session.add_all([c1, c2]); db.session.commit()
        yield {'c1': c1.customer_id, 'c2': c2.customer_id}
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def http_transport(monkeypatch):
    import mcp_server.auth as auth
    import api_key_service
    monkeypatch.setenv('MCP_TRANSPORT', 'http')
    monkeypatch.setattr(auth, 'MCP_AUTH_REQUIRED', True)
    monkeypatch.setattr(auth, 'MCP_SERVER_API_KEY', 'srv-' + uuid.uuid4().hex)
    api_key_service._failures.clear() if hasattr(api_key_service, '_failures') else None
    yield


def _mint(cid, scopes, name='k'):
    from api_key_service import generate_api_key
    with app.app_context():
        full, rec = generate_api_key(cid, created_by=None, name=name, scopes=scopes)
        return full, rec.id


def _call(key, fn):
    import mcp_server.auth as auth
    tok = auth._current_api_key_var.set(key or '')
    try:
        return fn()
    finally:
        auth._current_api_key_var.reset(tok)


def _server_key():
    import mcp_server.auth as auth
    return auth.MCP_SERVER_API_KEY


# ── revocation ────────────────────────────────────────────────────────────────

def test_revoked_key_is_refused_on_the_next_call(tenants):
    from fastmcp.exceptions import ToolError
    from mcp_server.auth import require_auth_if_key_present
    from mcp_server.cs_pulse_keys import revoke_api_key, list_api_keys
    full, kid = _mint(tenants['c1'], ['read', 'write'])

    def use():
        with app.app_context():
            return require_auth_if_key_present('get_journey', tenants['c1'])
    assert _call(full, use).id == kid

    res = _call(_server_key(), lambda: revoke_api_key(tenants['c1'], kid))
    assert res['status'] == 'revoked'
    with pytest.raises(ToolError, match='Invalid or expired'):
        _call(full, use)
    # idempotent, and visible only when asked for
    assert _call(_server_key(), lambda: revoke_api_key(tenants['c1'], kid))['status'] == 'already_revoked'
    assert kid not in [k['id'] for k in _call(_server_key(), lambda: list_api_keys(tenants['c1']))['keys']]
    listed = _call(_server_key(), lambda: list_api_keys(tenants['c1'], include_revoked=True))['keys']
    assert [k['is_active'] for k in listed if k['id'] == kid] == [False]
    assert all('key_hash' not in k for k in listed)


def test_revoked_key_is_refused_on_the_http_routes(tenants):
    from signal_engine.http import _authorize
    from api_key_service import revoke_api_key
    full, kid = _mint(tenants['c1'], ['read', 'write'])

    def use():
        with app.app_context():
            return _authorize(tenants['c1'], 'read', route='review_queue')
    assert _call(full, use)[0] is True
    with app.app_context():
        assert revoke_api_key(kid, customer_id=tenants['c1']) is True
    ok, err = _call(full, use)
    assert ok is False and 'Invalid or expired' in err['error']


def test_rotate_issues_a_working_key_with_the_same_grants_and_kills_the_old_one(tenants):
    from fastmcp.exceptions import ToolError
    from mcp_server.auth import require_auth_if_key_present
    from mcp_server.cs_pulse_keys import rotate_api_key
    from models import CustomerApiKey
    with app.app_context():
        from api_key_service import generate_api_key
        old_full, rec = generate_api_key(tenants['c1'], created_by=None, name='Pilot app', scopes=['read', 'write'],
                                         allowed_account_ids=[7, 9], partner_tier=None)
        old_id = rec.id

    res = _call(_server_key(), lambda: rotate_api_key(tenants['c1'], old_id))
    new_full = res['api_key']
    assert new_full != old_full and new_full.startswith('csp_write_')
    assert res['revoked_key_id'] == old_id and res['key']['id'] != old_id
    assert res['key']['name'] == 'Pilot app' and res['key']['scopes'] == ['read', 'write']
    assert res['key']['allowed_account_ids'] == [7, 9]

    def use(key):
        with app.app_context():
            return require_auth_if_key_present('upload_csv', tenants['c1'])
    assert _call(new_full, lambda: use(new_full)).id == res['key']['id']
    with pytest.raises(ToolError, match='Invalid or expired'):
        _call(old_full, lambda: use(old_full))
    with app.app_context():
        # the full key is not stored anywhere
        row = db.session.get(CustomerApiKey, res['key']['id'])
        assert new_full not in (row.key_hash, row.key_prefix) and len(row.key_hash) == 64
    # a revoked key cannot be rotated back to life
    with pytest.raises(ToolError, match='already revoked'):
        _call(_server_key(), lambda: rotate_api_key(tenants['c1'], old_id))


def test_key_management_needs_the_server_key_or_admin_scope(tenants):
    """The read+write key a tenant holds (and that may be the leaked one) must not be able
    to list, revoke or re-mint keys. Anonymous callers and other tenants' keys neither."""
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_keys import list_api_keys, revoke_api_key, rotate_api_key
    from mcp_server import audit
    write_full, write_id = _mint(tenants['c1'], ['read', 'write'])
    admin_full, _ = _mint(tenants['c1'], ['admin'])
    other_admin, _ = _mint(tenants['c2'], ['admin'])
    victim_full, victim_id = _mint(tenants['c1'], ['read'])

    calls = (lambda: list_api_keys(tenants['c1']),
             lambda: revoke_api_key(tenants['c1'], victim_id),
             lambda: rotate_api_key(tenants['c1'], victim_id))
    for fn in calls:
        with pytest.raises(ToolError, match='requires an API key'):
            _call(None, fn)
        with pytest.raises(ToolError, match="'admin' scope"):
            _call(write_full, fn)
        with pytest.raises(ToolError, match='does not have access to customer'):
            _call(other_admin, fn)
    with app.app_context():
        from models import CustomerApiKey
        assert db.session.get(CustomerApiKey, victim_id).is_active is True
        denied = audit.query(tenants['c1'], tool='revoke_api_key', outcome='denied')
        assert {'lacks admin scope', 'key required'} <= {r['detail'] for r in denied}

    # the tenant's own admin key may
    assert victim_id in [k['id'] for k in _call(admin_full, lambda: list_api_keys(tenants['c1']))['keys']]
    assert _call(admin_full, lambda: revoke_api_key(tenants['c1'], victim_id))['status'] == 'revoked'
    with app.app_context():
        done = [r for r in audit.query(tenants['c1'], tool='revoke_api_key', outcome='allowed') if r['detail']]
        assert any(f'id={victim_id}' in r['detail'] for r in done)


def test_a_key_id_from_another_tenant_is_not_found(tenants):
    """customer_id is what the caller is authorised for; key_id must belong to it."""
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_keys import revoke_api_key, rotate_api_key
    from models import CustomerApiKey
    c2_full, c2_id = _mint(tenants['c2'], ['read', 'write'])
    c1_admin, _ = _mint(tenants['c1'], ['admin'])
    for fn in (lambda: revoke_api_key(tenants['c1'], c2_id), lambda: rotate_api_key(tenants['c1'], c2_id)):
        with pytest.raises(ToolError, match='not found'):
            _call(c1_admin, fn)
    with pytest.raises(ToolError, match='not found'):
        _call(_server_key(), lambda: revoke_api_key(999999999, c2_id))
    with app.app_context():
        assert db.session.get(CustomerApiKey, c2_id).is_active is True


def test_key_tools_are_registered_keyed_and_admin():
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    from mcp_server.auth import ADMIN_TOOLS
    assert ADMIN_TOOLS == {'list_api_keys', 'revoke_api_key', 'rotate_api_key'}
    assert ADMIN_TOOLS <= KEYED_TOOLS and not (ADMIN_TOOLS & ONBOARDING_TOOLS)
    assert 'import mcp_server.cs_pulse_keys' in (BACKEND / 'server.py').read_text()


def test_operator_cli_lists_rotates_and_revokes(tenants, capsys):
    sys.path.insert(0, str(BACKEND / 'scripts'))
    import manage_api_keys as cli
    from api_key_service import validate_api_key
    full, kid = _mint(tenants['c1'], ['read', 'write'], name='cli-key')
    assert cli.main(['list', '--customer-id', str(tenants['c1'])]) == 0
    out = capsys.readouterr().out
    assert f'id={kid}' in out and full not in out

    assert cli.main(['rotate', '--customer-id', str(tenants['c1']), '--key-id', str(kid)]) == 0
    new_full = [l.strip() for l in capsys.readouterr().out.splitlines() if l.strip().startswith('csp_')][0]
    with app.app_context():
        assert validate_api_key(full) is None
        assert validate_api_key(new_full) is not None
    # wrong tenant: refused, nothing changes
    assert cli.main(['rotate', '--customer-id', str(tenants['c2']), '--key-id', str(kid)]) == 2
    assert cli.main(['revoke', '--customer-id', str(tenants['c1']), '--all-keys']) == 0
    capsys.readouterr()
    with app.app_context():
        from api_key_service import list_api_keys
        assert validate_api_key(new_full) is None and list_api_keys(tenants['c1']) == []
    assert cli.main(['list', '--customer-id', '999999999']) == 2


# ── keys stay out of the logs ─────────────────────────────────────────────────

SECRET = 'csp_write_Zx9Qe7hT2mVbN4kLpR8sW1yA6cD3fG5jH0uI'


def test_redact_covers_query_params_customer_keys_bearer_and_server_key(monkeypatch):
    from utils.log_redaction import redact
    monkeypatch.setenv('MCP_SERVER_API_KEY', 'server-key-0123456789abcdef')
    cases = {
        f'/mcp?api_key={SECRET}': '/mcp?api_key=REDACTED',
        f'/mcp?x=1&token={SECRET}&customer_id=14': '/mcp?x=1&token=REDACTED&customer_id=14',
        '/mcp?API_KEY=server-key-0123456789abcdef': '/mcp?API_KEY=REDACTED',
        f'validation failed for {SECRET} from 1.2.3.4': 'validation failed for csp_write_REDACTED from 1.2.3.4',
        'Authorization: Bearer server-key-0123456789abcdef': 'Authorization: Bearer REDACTED',
        'got server-key-0123456789abcdef in body': 'got REDACTED in body',
        # not credentials: untouched
        '/api/signals/review-queue?customer_id=14&page=2': '/api/signals/review-queue?customer_id=14&page=2',
        'kpi_key=nps health=72': 'kpi_key=nps health=72',
    }
    for raw, want in cases.items():
        assert redact(raw) == want, raw


def test_uvicorn_access_line_never_carries_the_key():
    """The exact record uvicorn emits per request, through uvicorn's own formatter."""
    import io
    import logging.config
    from utils.log_redaction import uvicorn_log_config
    cfg = uvicorn_log_config()
    assert all('redact_secrets' in h['filters'] for h in cfg['handlers'].values())
    buf = io.StringIO()
    cfg['handlers']['access']['stream'] = buf
    cfg['handlers']['default']['stream'] = buf
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate, logging.getLogger(n).level)
             for n in ('uvicorn', 'uvicorn.access', 'uvicorn.error')}
    try:
        logging.config.dictConfig(cfg)
        logging.getLogger('uvicorn.access').info(
            '%s - "%s %s HTTP/%s" %d', '10.0.0.1:5000', 'POST', f'/mcp?api_key={SECRET}', '1.1', 200)
        logging.getLogger('uvicorn.error').info('Exception in ASGI application for /mcp?token=%s', SECRET)
    finally:
        for n, (handlers, propagate, level) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:] = handlers; lg.propagate = propagate; lg.setLevel(level)
    out = buf.getvalue()
    assert SECRET not in out and 'Zx9Qe7' not in out
    assert '"POST /mcp?api_key=REDACTED HTTP/1.1" 200' in out
    assert 'token=REDACTED' in out


def test_root_handlers_redact_and_install_is_idempotent():
    import io
    from utils.log_redaction import install_on_root, RedactSecretsFilter
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(h)
    try:
        install_on_root(); install_on_root()
        assert sum(isinstance(f, RedactSecretsFilter) for f in h.filters) == 1
        logging.getLogger('customerintel.anything').warning('caller sent %s', SECRET)
        logging.getLogger('customerintel.anything').warning('dict args %(k)s', {'k': f'api_key={SECRET}'})
        logging.getLogger('customerintel.anything').warning('webhook url had token=%s', 'opaque-token-12345')
    finally:
        root.removeHandler(h)
    out = buf.getvalue()
    assert SECRET not in out and 'opaque-token-12345' not in out and out.count('REDACTED') == 3


def test_query_string_key_still_authenticates(tenants, monkeypatch):
    """The claude.ai connector needs ?api_key=; redaction must not change that."""
    import asyncio
    import server
    import mcp_server.auth as auth
    monkeypatch.setattr(server, 'ALLOW_QUERY_KEY', True)
    seen = {}

    async def inner(scope, receive, send):
        seen['key'] = auth._current_api_key_var.get('')

    scope = {'type': 'http', 'path': '/mcp', 'headers': [], 'query_string': f'api_key={SECRET}'.encode(), 'client': ('1.1.1.1', 1)}
    asyncio.run(server.BearerAuthMiddleware(inner)(scope, None, None))
    assert seen['key'] == SECRET
