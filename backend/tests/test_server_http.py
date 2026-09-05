"""
CustomerIntelV1 HTTP server: /health, and a real MCP streamable-HTTP
handshake through the Bearer middleware (initialize → tools/list →
tools/call), plus the customer-key service and the tenant-isolation
checks that only exist over HTTP.
"""
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
SERVER_KEY = 'test-server-key-' + uuid.uuid4().hex


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


@pytest.fixture(scope='module')
def client():
    _assert_isolated_test_db(TEST_DB)
    os.environ['MCP_SERVER_API_KEY'] = SERVER_KEY
    os.environ['MCP_AUTH_REQUIRED'] = 'true'
    import mcp_server.auth as auth
    auth.MCP_SERVER_API_KEY = SERVER_KEY      # module constant is read at import
    from server import build_asgi_app
    app = build_asgi_app(TEST_DB)
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c
    import mcp_server.common as _common
    from extensions import db
    with _common.get_flask_app().app_context():
        db.session.remove()
        db.drop_all()
    os.environ['MCP_TRANSPORT'] = 'stdio'


def _rpc(client, method, params=None, *, id_=1, session=None, key=SERVER_KEY):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    if session:
        headers['mcp-session-id'] = session
    body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
    if id_ is not None:
        body['id'] = id_
    r = client.post('/mcp', headers=headers, content=json.dumps(body))
    payload = None
    if r.text.strip():
        for line in r.text.splitlines():
            if line.startswith('data:'):
                payload = json.loads(line[5:].strip())
        if payload is None and r.headers.get('content-type', '').startswith('application/json'):
            payload = r.json()
    return r, payload


def _session(client, key=SERVER_KEY):
    r, init = _rpc(client, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                          'clientInfo': {'name': 'test', 'version': '1'}}, key=key)
    assert r.status_code == 200, r.text
    sid = r.headers.get('mcp-session-id')
    assert sid and init['result']['serverInfo']
    _rpc(client, 'notifications/initialized', id_=None, session=sid, key=key)
    return sid


class TestHealth:
    def test_health_and_root(self, client):
        r = client.get('/health')
        assert r.status_code == 200
        body = r.json()
        assert body['server'] == 'CustomerIntelV1' and body['db'] is True and body['status'] == 'ok'
        assert set(body['counts']) == {'customers', 'customers_by_data_origin', 'journeys', 'stale_journeys', 'wizard_runs'}
        assert client.get('/').json()['mcp'] == '/mcp'


class TestMcpOverHttp:
    def test_handshake_and_tool_list(self, client):
        sid = _session(client)
        r, res = _rpc(client, 'tools/list', id_=2, session=sid)
        assert r.status_code == 200, r.text
        names = {t['name'] for t in res['result']['tools']}
        assert {'create_customer', 'upload_csv', 'process_data', 'trigger_wizard'} <= names

    def test_onboarding_tool_is_frictionless_without_a_key(self, client):
        sid = _session(client, key=None)
        r, res = _rpc(client, 'tools/call', {'name': 'create_customer', 'arguments': {
            'name': 'Frictionless Co', 'domain': f'friction-{uuid.uuid4().hex[:6]}.test', 'vertical': 'saas_premium',
            'admin_email': f'f_{uuid.uuid4().hex[:6]}@t.test', 'admin_name': 'F', 'data_origin': 'synthetic_test'}}, id_=3, session=sid, key=None)
        assert r.status_code == 200, r.text
        assert not res['result'].get('isError'), res
        text = res['result']['content'][0]['text']
        assert '"customer_id"' in text and '"api_key"' in text          # a customer-scoped key is issued now

    def test_customer_key_is_tenant_scoped(self, client):
        sid = _session(client, key=None)
        r, res = _rpc(client, 'tools/call', {'name': 'create_customer', 'arguments': {
            'name': 'Scoped Co', 'domain': f'scoped-{uuid.uuid4().hex[:6]}.test', 'vertical': 'saas_premium',
            'admin_email': f's_{uuid.uuid4().hex[:6]}@t.test', 'admin_name': 'S', 'data_origin': 'synthetic_test'}}, id_=4, session=sid, key=None)
        created = json.loads(res['result']['content'][0]['text'])
        key, cid = created['api_key'], created['customer_id']
        assert key.startswith('csp_write_')
        # own tenant: allowed (process_data errors on "no data", not on auth)
        sid2 = _session(client, key=key)
        r, res = _rpc(client, 'tools/call', {'name': 'process_data', 'arguments': {'customer_id': cid}}, id_=5, session=sid2, key=key)
        assert 'No data found' in res['result']['content'][0]['text']
        # another tenant: refused
        r, res = _rpc(client, 'tools/call', {'name': 'process_data', 'arguments': {'customer_id': cid + 1000}}, id_=6, session=sid2, key=key)
        assert res['result'].get('isError') and 'does not have access' in res['result']['content'][0]['text']
        # garbage key: refused
        sid3 = _session(client, key='csp_write_not-a-real-key-at-all-0000000000000000')
        r, res = _rpc(client, 'tools/call', {'name': 'process_data', 'arguments': {'customer_id': cid}}, id_=7, session=sid3,
                      key='csp_write_not-a-real-key-at-all-0000000000000000')
        assert res['result'].get('isError') and 'Invalid or expired' in res['result']['content'][0]['text']


class TestApiKeyService:
    def test_generate_validate_scope_revoke(self, client):
        import mcp_server.common as _common
        from api_key_service import generate_api_key, validate_api_key, revoke_api_key, list_api_keys
        from mcp_server.auth import check_scope
        from models import Customer
        from extensions import db
        with _common.get_flask_app().app_context():
            c = Customer(customer_name='Keys', email=f'k_{uuid.uuid4().hex[:6]}@t.test', domain=f'k-{uuid.uuid4().hex[:6]}.test')
            db.session.add(c)
            db.session.commit()
            full, rec = generate_api_key(c.customer_id, created_by=None, name='CI', scopes=['read'])
            assert full.startswith('csp_read_') and rec.key_hash != full
            got = validate_api_key(full)
            assert got and got.id == rec.id and got.last_used_at is not None
            assert check_scope(got, 'read') and not check_scope(got, 'write')
            wrong = full[:-1] + ('0' if full[-1] != '0' else '1')     # guaranteed different (1-in-64 flake when it was always 'x')
            assert validate_api_key(wrong) is None
            assert [k['id'] for k in list_api_keys(c.customer_id)] == [rec.id]
            assert revoke_api_key(rec.id) is True
            assert validate_api_key(full) is None
            with pytest.raises(ValueError):
                generate_api_key(c.customer_id, None, 'bad', scopes=['root'])


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


class TestLLMLedger:
    def test_record_usage_writes_a_row(self, client):
        """Every LLM call site must be proven tracked: the ledger table exists in
        this build (LLMUsageLog → create_all) and record_usage lands a row."""
        import mcp_server.common as _common
        from extensions import db
        from models import Customer, LLMUsageLog
        from utils.llm_budget_controller import record_usage, estimate_cost
        with _common.get_flask_app().app_context():
            c = Customer(customer_name='Ledger', email=f'l_{uuid.uuid4().hex[:6]}@t.test', domain=f'l-{uuid.uuid4().hex[:6]}.test')
            db.session.add(c)
            db.session.commit()
            record_usage(customer_id=c.customer_id, module='signal_engine_enrichment', tokens_in=5424, tokens_out=991,
                         model='claude-sonnet-5', success=True)
            row = LLMUsageLog.query.filter_by(customer_id=c.customer_id).one()
            assert row.module == 'signal_engine_enrichment' and row.tokens_in == 5424 and row.success is True
            assert float(row.cost_estimate_usd) == estimate_cost('claude-sonnet-5', 5424, 991) > 0
