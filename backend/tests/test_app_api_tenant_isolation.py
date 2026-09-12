"""
Cross-tenant isolation on the session-cookie surface (/app/api/*), end to end.

The bug this file exists for, reproduced live before the fix: any logged-in
user of any role at any tenant could read and act on ANOTHER tenant's data by
passing that tenant's customer_id — portfolio, account journeys,
interventions, ROI/Power-of-1, calibrations, playbook config, the whole
platform's user list, and a working password-setup token for another tenant's
admin (a complete account takeover). Two causes, both closed here:
`allows_customer` read an unset allowed_customer_ids as "unrestricted" and
nothing ever set the column; `user_scope` additionally short-circuited
role == 'admin' to unrestricted, while create_customer hands 'admin' to every
new tenant's OWN first user.

tests/test_app_api_http.py covers the happy paths and the role gates for ONE
tenant. This file is the adversarial half: two real tenants, a real user of
every role in each, and every /app/api/* route that names or implies a
customer_id, asked for the other tenant's ids.

  * EVERY route × EVERY role: tenant A's user is refused tenant B's ids, and
    the refusal is the tenant check (403 + the scope error), not merely the
    role gate — which is why each route is also asserted with a role that
    passes its gate.
  * The same request against the user's OWN tenant is not refused (a positive
    control: these tests must fail because of scope, not because the route is
    broken for everyone).
  * Foreign object ids with the caller's OWN customer_id (B's account,
    intervention, calibration proposal, user) are refused too — the service
    layer keys on the (customer_id, id) pair.
  * A user with allowed_customer_ids IS NULL — a row that predates revision
    0005 — reaches nothing, including their own tenant. Fail-closed.
  * The route table itself is asserted: every entry in app_api.http.ROUTES is
    either exercised here or listed in NO_TENANT with a reason, so a new route
    cannot quietly ship without a tenant-isolation decision.
"""
import os
import sys
import uuid
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
SERVER_KEY = 'test-server-key-' + uuid.uuid4().hex

ROLES = ('admin', 'cro', 'cfo', 'csm', 'vpcsm')
SCOPE_ERROR = 'not permitted for your account/tenant scope'


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


def _make_tenant(label: str) -> dict:
    """A real tenant through the real onboarding path, one account with a journey, a
    logged-in-able user of every role, and one real row of each kind another tenant
    might try to reach."""
    import mcp_server.common as _common
    from extensions import db
    from models import Account, Intervention, QualitativeSignal, User, WeightCalibration
    from app_api import auth as app_auth
    from journeys.wizard_a import run_wizard_a
    from werkzeug.security import generate_password_hash
    tag = uuid.uuid4().hex[:8]
    with _common.get_flask_app().app_context():
        from mcp_server.cs_pulse_onboarding import create_customer
        res = create_customer(name=f'{label} {tag}', domain=f'{label}-{tag}.test', vertical='saas_premium',
                              admin_email=f'admin_{label}_{tag}@t.test', admin_name=f'{label} Admin',
                              data_origin='synthetic_test')
        cid = res['customer_id']
        app_auth.consume_setup_token(res['admin_setup_token'], f'{label}-admin-password-1')
        acct = Account(customer_id=cid, account_name=f'{label} Account', revenue=250_000, vertical='saas_premium')
        db.session.add(acct); db.session.commit()
        aid = acct.account_id
        run_wizard_a(cid)

        logins = {'admin': (res['admin_email'], f'{label}-admin-password-1')}
        for role in [r for r in ROLES if r != 'admin']:
            email = f'{role}_{label}_{tag}@t.test'
            db.session.add(User(customer_id=cid, user_name=f'{label} {role}', email=email, role=role, active=True,
                                password_hash=generate_password_hash(f'{label}-{role}-password-1'),
                                allowed_customer_ids=[cid]))
            logins[role] = (email, f'{label}-{role}-password-1')
        # pre-0005 rows, one per role: exactly how every user on every box was created —
        # both paths left allowed_customer_ids NULL, whatever the role
        unscoped = {}
        for role in ROLES:
            email = f'unscoped_{role}_{label}_{tag}@t.test'
            db.session.add(User(customer_id=cid, user_name=f'{label} unscoped {role}', email=email, role=role,
                                active=True, password_hash=generate_password_hash(f'{label}-{role}-unscoped-1'),
                                allowed_customer_ids=None))
            unscoped[role] = (email, f'{label}-{role}-unscoped-1')
        db.session.commit()

        iv = Intervention(customer_id=cid, account_id=aid, playbook_id='escalation_exec_response', playbook_version='1.0',
                          action_class='escalate', approval_mode='human', state='proposed', trigger_key=uuid.uuid4().hex * 2,
                          trigger_episode_ids=['sig:0'], trigger_node_ids=[], trigger_roles=['escalation'],
                          expected_outcome_types=['escalation_resolved'], expected_window_days=60)
        cal = WeightCalibration(customer_id=cid, vertical='saas_premium', state='proposed', method_version='1.0',
                                config_snapshot={}, outcome_counts={}, outcome_node_ids=[], current_pillar_weights={},
                                current_kpi_weights={}, proposed_pillar_weights={}, proposed_kpi_weights={},
                                evidence={}, impact={}, proposed_at=datetime.utcnow(), notes=[])
        sig_id = f'sig-{label}-{tag}'
        sig = QualitativeSignal(signal_id=sig_id, customer_id=cid, account_id=aid, signal_date=date.today(),
                                signal_type='email', content='confidential', requires_review=True)
        db.session.add_all([iv, cal, sig]); db.session.commit()
        return {'cid': cid, 'account_id': aid, 'intervention_id': iv.id, 'proposal_id': cal.id, 'signal_id': sig_id,
                'admin_user_id': res['admin_user_id'], 'logins': logins, 'unscoped': unscoped}


@pytest.fixture(scope='module')
def tenants(client):
    return {'A': _make_tenant('alpha'), 'B': _make_tenant('bravo')}


def _login(client, creds):
    r = client.post('/app/api/auth/login', json={'email': creds[0], 'password': creds[1]})
    assert r.status_code == 200, r.text
    return r


def _logout(client):
    client.post('/app/api/auth/logout')


def _routes(t: dict) -> list:
    """Every /app/api/* request that names a customer_id, aimed at tenant `t`.

    (label, method, path, body, roles_that_pass_the_role_gate). `roles` is the set whose
    ROLE gate lets them through, so a refusal for one of those roles can only have come
    from the tenant check — the point of the exercise."""
    cid, aid = t['cid'], t['account_id']
    return [
        ('portfolio',             'get',   f'/app/api/portfolio?customer_id={cid}', None, ROLES),
        ('account detail',        'get',   f"/app/api/accounts/{aid}?customer_id={cid}", None, ROLES),
        ('interventions list',    'get',   f'/app/api/interventions?customer_id={cid}', None, ROLES),
        ('interventions list+acct', 'get', f'/app/api/interventions?customer_id={cid}&account_id={aid}', None, ROLES),
        ('interventions evaluate', 'get',  f'/app/api/interventions/evaluate?customer_id={cid}', None, ROLES),
        ('interventions evaluate+acct', 'get', f'/app/api/interventions/evaluate?customer_id={cid}&account_id={aid}', None, ROLES),
        ('intervention approve',  'post',  f"/app/api/interventions/{t['intervention_id']}/approve",
                                           {'customer_id': cid, 'note': 'x'}, ('admin', 'csm')),
        ('intervention report',   'post',  f"/app/api/interventions/{t['intervention_id']}/report",
                                           {'customer_id': cid, 'state': 'done'}, ('admin', 'csm')),
        ('roi measured',          'get',   f'/app/api/roi?customer_id={cid}', None, ('admin', 'cfo', 'cro')),
        ('roi priorities',        'get',   f'/app/api/roi/priorities?customer_id={cid}', None, ('admin', 'cfo', 'cro')),
        ('roi power-of-1',        'get',   f'/app/api/roi/power-of-1?customer_id={cid}', None, ('admin', 'cfo', 'cro')),
        ('forecast',              'get',   f'/app/api/forecast?customer_id={cid}', None, ROLES),
        ('forecast+account',      'get',   f'/app/api/forecast?customer_id={cid}&account_id={aid}', None, ROLES),
        ('ask ai',                'post',  '/app/api/ask', {'customer_id': cid, 'question': 'how are we doing?'}, ROLES),
        ('calibrations get',      'get',   f'/app/api/calibrations?customer_id={cid}', None, ('admin',)),
        ('calibrations propose',  'post',  '/app/api/calibrations/propose', {'customer_id': cid}, ('admin',)),
        ('calibration approve',   'post',  f"/app/api/calibrations/{t['proposal_id']}/approve", {'customer_id': cid}, ('admin',)),
        ('calibration reject',    'post',  f"/app/api/calibrations/{t['proposal_id']}/reject", {'customer_id': cid}, ('admin',)),
        ('review queue',          'get',   f'/app/api/review-queue?customer_id={cid}', None, ('admin', 'csm')),
        ('review post',           'post',  '/app/api/review',
                                           {'customer_id': cid, 'signal_id': t['signal_id'], 'decision': 'accept'}, ('admin', 'csm')),
        ('playbook config get',   'get',   f'/app/api/playbooks/config?customer_id={cid}', None, ('admin',)),
        ('playbook config post',  'post',  '/app/api/playbooks/config', {'customer_id': cid, 'kill_switch': False}, ('admin',)),
        ('users list',            'get',   f'/app/api/users?customer_id={cid}', None, ('admin',)),
        ('users invite',          'post',  '/app/api/users',
                                           {'customer_id': cid, 'email': f'x{uuid.uuid4().hex[:8]}@t.test',
                                            'name': 'X', 'role': 'csm'}, ('admin',)),
        # no customer_id in these two — the tenant is implied by the target user row
        ('users patch',           'patch', f"/app/api/users/{t['admin_user_id']}", {'role': 'csm'}, ('admin',)),
        ('users reset-password',  'post',  f"/app/api/users/{t['admin_user_id']}/reset-password", None, ('admin',)),
    ]


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return getattr(client, method)(path, json=body)


# ── the main matrix: every route × every role, aimed at the other tenant ──

@pytest.mark.parametrize('role', ROLES)
def test_a_user_of_tenant_a_is_refused_every_route_of_tenant_b(client, tenants, role):
    """Every route, every role. A refusal here can be 403 (role gate or tenant scope) or
    404, never a 200 carrying tenant B's data."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['logins'][role])
    try:
        for label, method, path, body, _roles in _routes(B):
            r = _call(client, method, path, body)
            assert r.status_code in (401, 403, 404), f'{role} reached B via {label}: {r.status_code} {r.text[:200]}'
            assert 'bravo' not in r.text.lower(), f'{role} saw tenant B content via {label}: {r.text[:200]}'
    finally:
        _logout(client)


@pytest.mark.parametrize('role', ROLES)
def test_the_refusal_is_the_tenant_check_not_the_role_gate(client, tenants, role):
    """The sharper assertion: for the routes this role's gate DOES let through, the refusal
    must be the scope check — 403 with the tenant-scope error. Without this, a role gate
    could mask a missing tenant check (exactly how the calibrations, playbooks/config and
    users routes leaked: they had a role gate and no tenant check at all)."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['logins'][role])
    try:
        checked = 0
        for label, method, path, body, roles in _routes(B):
            if role not in roles:
                continue
            r = _call(client, method, path, body)
            assert r.status_code == 403, f'{role} {label}: expected 403, got {r.status_code} {r.text[:200]}'
            assert SCOPE_ERROR in r.text, f'{role} {label}: refused by the role gate, not the tenant check — {r.text[:200]}'
            checked += 1
        assert checked >= (24 if role == 'admin' else 6)
    finally:
        _logout(client)


@pytest.mark.parametrize('role', ROLES)
def test_positive_control_the_same_requests_against_your_own_tenant_are_not_scope_refused(client, tenants, role):
    """The matrix above must fail for the right reason. Every route this role may use is
    reachable for the caller's OWN tenant — never a tenant-scope 403."""
    A = tenants['A']
    _login(client, A['logins'][role])
    try:
        for label, method, path, body, roles in _routes(A):
            if role not in roles or label == 'ask ai':      # ask ai would spend a real model call
                continue
            if label in ('users patch', 'users reset-password', 'intervention approve', 'intervention report',
                         'calibration approve', 'calibration reject', 'users invite', 'review post',
                         'playbook config post', 'calibrations propose'):
                continue                                     # writes: covered by test_app_api_http.py, skipped here
            r = _call(client, method, path, body)
            assert r.status_code != 403 or SCOPE_ERROR not in r.text, f'{role} refused its OWN tenant via {label}: {r.text[:200]}'
    finally:
        _logout(client)


# ── a foreign object id with your own customer_id ──

def test_foreign_object_ids_are_refused_even_with_your_own_customer_id(client, tenants):
    """The other half of the pair: pass YOUR customer_id (so allows_customer passes) and
    the other tenant's account/intervention/proposal/signal id. The service layer keys on
    the (customer_id, id) pair, so these must miss — 404, or 400 'not found'."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['logins']['admin'])
    try:
        r = client.get(f"/app/api/accounts/{B['account_id']}?customer_id={A['cid']}")
        assert r.status_code == 404 and 'bravo' not in r.text.lower()
        r = client.get(f"/app/api/forecast?customer_id={A['cid']}&account_id={B['account_id']}")
        assert r.status_code == 404
        r = client.post(f"/app/api/interventions/{B['intervention_id']}/approve", json={'customer_id': A['cid']})
        assert r.status_code == 400 and 'not found' in r.json()['error']
        r = client.post(f"/app/api/interventions/{B['intervention_id']}/report",
                        json={'customer_id': A['cid'], 'state': 'done'})
        assert r.status_code == 400 and 'not found' in r.json()['error']
        r = client.post(f"/app/api/calibrations/{B['proposal_id']}/approve", json={'customer_id': A['cid']})
        assert r.status_code == 400 and 'not found' in r.json()['error']
        r = client.post(f"/app/api/calibrations/{B['proposal_id']}/reject", json={'customer_id': A['cid']})
        assert r.status_code == 400 and 'not found' in r.json()['error']
        r = client.post('/app/api/review', json={'customer_id': A['cid'], 'signal_id': B['signal_id'], 'decision': 'accept'})
        assert r.status_code == 400 and 'not found' in r.json()['error']
        # this one answered with a 500 + traceback until the route grew the same
        # try/except its three sibling calibration routes already had
        r = client.get(f"/app/api/calibrations?customer_id={A['cid']}&proposal_id={B['proposal_id']}")
        assert r.status_code == 400 and 'not found' in r.json()['error']
    finally:
        _logout(client)


# ── the two routes with no customer_id at all ──

def test_users_list_without_a_customer_id_does_not_return_the_platform(client, tenants):
    """GET /app/api/users with no query param used to list every user of every tenant —
    the whole platform's user directory, emails included, to any admin."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['logins']['admin'])
    try:
        r = client.get('/app/api/users')
        assert r.status_code == 200
        assert {u['customer_id'] for u in r.json()['users']} == {A['cid']}
        assert 'bravo' not in r.text.lower()
    finally:
        _logout(client)


def test_password_reset_for_another_tenants_admin_is_refused(client, tenants):
    """The takeover path, called out on its own because it is the worst of them: before the
    fix this returned a valid, single-use password-setup token for another tenant's admin,
    which the caller could then redeem at the unauthenticated /auth/set-password."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['logins']['admin'])
    try:
        r = client.post(f"/app/api/users/{B['admin_user_id']}/reset-password")
        assert r.status_code == 403 and SCOPE_ERROR in r.text
        assert 'setup_token' not in r.text
    finally:
        _logout(client)
    # and B's admin can still log in with the password it already had — nothing was reset
    _login(client, B['logins']['admin'])
    _logout(client)


def test_an_admin_cannot_widen_its_own_or_anyone_elses_tenant_scope(client, tenants):
    """PATCH /app/api/users/{id} writes allowed_customer_ids — the one column the whole fix
    rests on. An admin may only grant tenants it holds itself."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['logins']['admin'])
    try:
        me = client.get('/app/api/me').json()
        r = client.patch(f"/app/api/users/{me['user_id']}", json={'allowed_customer_ids': [A['cid'], B['cid']]})
        assert r.status_code == 403 and SCOPE_ERROR in r.text
        assert client.get('/app/api/me').json()['allowed_customer_ids'] == [A['cid']]
        # still refused B afterwards
        assert client.get(f"/app/api/portfolio?customer_id={B['cid']}").status_code == 403
        # a grant within its own scope is fine
        ok = client.patch(f"/app/api/users/{me['user_id']}", json={'allowed_customer_ids': [A['cid']]})
        assert ok.status_code == 200 and ok.json()['allowed_customer_ids'] == [A['cid']]
    finally:
        _logout(client)


# ── fail-closed: the pre-migration row shape ──

@pytest.mark.parametrize('role', ROLES)
def test_a_user_with_no_scope_column_reaches_nothing_not_everything(client, tenants, role):
    """Every User row created before this fix has allowed_customer_ids IS NULL — neither
    creation path ever wrote it, for any role (revision 0005 back-fills them). Until it
    runs, such a row must reach NOTHING: not another tenant, and not its own either. NULL
    is a provisioning gap, never a grant — the same fail-closed default the rest of this
    codebase uses for missing config (utils.vertical_registry, the taxonomy loader)."""
    A, B = tenants['A'], tenants['B']
    _login(client, A['unscoped'][role])
    try:
        assert client.get('/app/api/me').json()['allowed_customer_ids'] is None
        for label, method, path, body, roles in _routes(A) + _routes(B):
            if role not in roles:
                continue
            r = _call(client, method, path, body)
            assert r.status_code in (401, 403, 404), f'unscoped {role} reached {label}: {r.status_code} {r.text[:200]}'
            assert 'bravo' not in r.text.lower(), f'unscoped {role} saw tenant B via {label}'
        # and specifically refused by the TENANT check, on its own tenant
        r = client.get(f"/app/api/portfolio?customer_id={A['cid']}")
        assert r.status_code == 403 and SCOPE_ERROR in r.text
        if role == 'admin':
            r = client.get('/app/api/users')          # the no-customer_id route: no scope, no listing
            assert r.status_code == 403 and SCOPE_ERROR in r.text
    finally:
        _logout(client)


# ── the route table cannot drift away from this file ──

NO_TENANT = {
    '/app/api/auth/login': 'no session yet; takes credentials, not a customer_id',
    '/app/api/auth/logout': 'clears the cookie; touches no tenant data',
    '/app/api/auth/set-password': 'consumes an admin-issued token; the token identifies the user',
    '/app/api/me': "returns the caller's own row only",
    '/app/api/ask/questions': 'static curated question list per role; no tenant data',
}


def test_every_route_is_either_isolation_tested_or_explicitly_exempt(tenants):
    """The guard against drift: a route added to app_api.http.ROUTES must be either covered
    by _routes() above or listed in NO_TENANT with a reason. Nobody can add a customer_id
    route and skip the tenant-isolation decision without this test failing."""
    from app_api.http import ROUTES
    covered = set()
    for _label, _m, path, _b, _r in _routes(tenants['A']):
        p = path.split('?')[0]
        parts = p.split('/')
        covered.add('/'.join('{id}' if seg.isdigit() else seg for seg in parts))
    missing = []
    for route in ROUTES:
        if route in NO_TENANT:
            continue
        if route not in covered:
            missing.append(route)
    assert not missing, f'routes with no cross-tenant isolation test: {missing}'
    assert set(NO_TENANT) <= set(ROUTES), 'NO_TENANT names a route that no longer exists'
