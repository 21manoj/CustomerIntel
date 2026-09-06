"""
The human UI's HTTP surface end to end (Starlette TestClient, real Postgres,
docs/design/ui-rbac.md): login sets a signed httponly cookie, /app/api/me
reads it back, every route is 401 with no session, role gates are 403 (not
just hidden), account/tenant scoping filters what a non-admin sees, and
approve/report/admin actions land through the same functions the Bearer
routes use. Set-password (the only unauthenticated write) only ever
consumes a token an admin already issued — there is no self-service
"email me a reset link" route (see the design doc for why).
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
    os.environ.setdefault('SESSION_SECRET', 'test-secret-' + uuid.uuid4().hex)
    os.environ['SESSION_COOKIE_INSECURE'] = 'true'
    import mcp_server.auth as mauth
    mauth.MCP_SERVER_API_KEY = SERVER_KEY
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


@pytest.fixture(scope='module')
def tenant(client):
    """One tenant, two accounts, an admin (unrestricted) and a csm scoped to account A only."""
    import mcp_server.common as _common
    from extensions import db
    from models import Account, User
    from app_api import auth as app_auth
    from journeys.wizard_a import run_wizard_a
    tag = uuid.uuid4().hex[:8]
    with _common.get_flask_app().app_context():
        from mcp_server.cs_pulse_onboarding import create_customer
        res = create_customer(name=f'UI {tag}', domain=f'ui-{tag}.test', vertical='saas_premium',
                              admin_email=f'admin_{tag}@t.test', admin_name='Admin', data_origin='synthetic_test')
        cid = res['customer_id']
        app_auth.consume_setup_token(res['admin_setup_token'], 'admin-password-1')
        a = Account(customer_id=cid, account_name='Account A', revenue=100_000, vertical='saas_premium')
        b = Account(customer_id=cid, account_name='Account B', revenue=200_000, vertical='saas_premium')
        db.session.add_all([a, b]); db.session.commit()
        run_wizard_a(cid)          # real journeys (empty evidence is fine — every new account starts here)
        from werkzeug.security import generate_password_hash
        csm = User(customer_id=cid, user_name='Scoped CSM', email=f'csm_{tag}@t.test', role='csm', active=True,
                  password_hash=generate_password_hash('csm-password-1'), allowed_customer_ids=[cid], allowed_account_ids=[a.account_id])
        cfo = User(customer_id=cid, user_name='Finance', email=f'cfo_{tag}@t.test', role='cfo', active=True,
                  password_hash=generate_password_hash('cfo-password-1'))
        db.session.add_all([csm, cfo]); db.session.commit()
        out = {'customer_id': cid, 'account_a': a.account_id, 'account_b': b.account_id,
               'admin_email': res['admin_email'], 'csm_email': csm.email, 'cfo_email': cfo.email}
    return out


def _login(client, email, password):
    r = client.post('/app/api/auth/login', json={'email': email, 'password': password})
    return r


def test_login_sets_a_cookie_and_me_reads_it_back(client, tenant):
    r = _login(client, tenant['admin_email'], 'admin-password-1')
    assert r.status_code == 200 and r.json()['user']['role'] == 'admin'
    assert 'ci_session' in r.cookies
    me = client.get('/app/api/me')
    assert me.status_code == 200 and me.json()['email'] == tenant['admin_email']
    assert client.post('/app/api/auth/logout').status_code == 200
    assert client.get('/app/api/me').status_code == 401


def test_login_wrong_password_is_401_and_no_cookie(client, tenant):
    r = _login(client, tenant['admin_email'], 'not-the-password')
    assert r.status_code == 401 and 'ci_session' not in r.cookies


def test_every_protected_route_is_401_with_no_session(client, tenant):
    for method, path, body in [
        ('get', f"/app/api/portfolio?customer_id={tenant['customer_id']}", None),
        ('get', f"/app/api/accounts/{tenant['account_a']}?customer_id={tenant['customer_id']}", None),
        ('get', f"/app/api/interventions?customer_id={tenant['customer_id']}", None),
        ('get', f"/app/api/roi?customer_id={tenant['customer_id']}", None),
        ('get', f"/app/api/users?customer_id={tenant['customer_id']}", None),
    ]:
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 401, (path, r.text)


def test_portfolio_scopes_to_allowed_accounts_for_a_restricted_user(client, tenant):
    _login(client, tenant['csm_email'], 'csm-password-1')
    r = client.get(f"/app/api/portfolio?customer_id={tenant['customer_id']}")
    assert r.status_code == 200
    ids = {row['account_id'] for row in r.json()['accounts']}
    assert ids == {tenant['account_a']}          # account B is invisible to this user
    assert client.get(f"/app/api/accounts/{tenant['account_b']}?customer_id={tenant['customer_id']}").status_code == 403
    assert client.get(f"/app/api/accounts/{tenant['account_a']}?customer_id={tenant['customer_id']}").status_code == 200
    client.post('/app/api/auth/logout')


def test_admin_sees_every_account(client, tenant):
    _login(client, tenant['admin_email'], 'admin-password-1')
    r = client.get(f"/app/api/portfolio?customer_id={tenant['customer_id']}")
    ids = {row['account_id'] for row in r.json()['accounts']}
    assert ids == {tenant['account_a'], tenant['account_b']}
    client.post('/app/api/auth/logout')


def test_role_gate_finance_routes_403_for_csm_ok_for_cfo(client, tenant):
    _login(client, tenant['csm_email'], 'csm-password-1')
    assert client.get(f"/app/api/roi?customer_id={tenant['customer_id']}").status_code == 403
    client.post('/app/api/auth/logout')
    _login(client, tenant['cfo_email'], 'cfo-password-1')
    r = client.get(f"/app/api/roi/priorities?customer_id={tenant['customer_id']}")
    assert r.status_code in (200, 422)          # 422 only if the vertical's economics genuinely can't resolve; never 401/403
    client.post('/app/api/auth/logout')


def test_role_gate_users_and_calibrations_are_admin_only(client, tenant):
    _login(client, tenant['cfo_email'], 'cfo-password-1')
    assert client.get(f"/app/api/users?customer_id={tenant['customer_id']}").status_code == 403
    assert client.get(f"/app/api/calibrations?customer_id={tenant['customer_id']}").status_code == 403
    assert client.post('/app/api/playbooks/config', json={'customer_id': tenant['customer_id']}).status_code == 403
    client.post('/app/api/auth/logout')
    _login(client, tenant['admin_email'], 'admin-password-1')
    assert client.get(f"/app/api/users?customer_id={tenant['customer_id']}").status_code == 200
    client.post('/app/api/auth/logout')


def test_admin_invite_set_password_then_login(client, tenant):
    _login(client, tenant['admin_email'], 'admin-password-1')
    r = client.post('/app/api/users', json={'customer_id': tenant['customer_id'], 'email': f'inv_{uuid.uuid4().hex[:8]}@t.test',
                                            'name': 'Invited', 'role': 'csm', 'allowed_account_ids': [tenant['account_a']]})
    assert r.status_code == 200
    body = r.json()
    assert body['setup_token'] and 'setup_token' not in json.dumps(body['user'])
    client.post('/app/api/auth/logout')
    sp = client.post('/app/api/auth/set-password', json={'token': body['setup_token'], 'new_password': 'invited-password-1'})
    assert sp.status_code == 200
    lg = _login(client, body['user']['email'], 'invited-password-1')
    assert lg.status_code == 200 and lg.json()['user']['role'] == 'csm'
    # the token is single-use
    sp2 = client.post('/app/api/auth/set-password', json={'token': body['setup_token'], 'new_password': 'another-one'})
    assert sp2.status_code == 400
    client.post('/app/api/auth/logout')


def test_interventions_approve_requires_csm_or_admin_role(client, tenant):
    _login(client, tenant['cfo_email'], 'cfo-password-1')
    r = client.post('/app/api/interventions/999999/approve', json={'customer_id': tenant['customer_id']})
    assert r.status_code == 403
    client.post('/app/api/auth/logout')
    _login(client, tenant['csm_email'], 'csm-password-1')
    r2 = client.post('/app/api/interventions/999999/approve', json={'customer_id': tenant['customer_id']})
    assert r2.status_code == 400 and 'not found' in r2.json()['error']     # role gate passed; the row just doesn't exist
    client.post('/app/api/auth/logout')
