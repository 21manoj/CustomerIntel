"""
The platform console (/app/api/platform/*), end to end against two real tenants.

What must hold (docs/design/platform-console.md):

  * ONLY users.platform_role opens the console. No session -> 401; every tenant
    role, including the tenant's own 'admin' -> 403 on EVERY platform route, and
    the refusal is written to the audit log.
  * A 'viewer' reads everything and changes nothing.
  * An operator sees across tenants — and never sees a secret (key hash,
    password hash, raw key).
  * The console is the only cross-tenant door: an operator with no tenant scope
    is refused on the ordinary tenant routes like anyone else.
  * Suspending a tenant refuses its logins, kills its live sessions and refuses
    its API keys, deletes nothing, and reactivating restores all three.
  * Operators cannot manage each other from the browser; nobody can lock
    themselves out.
  * The role is granted only by scripts/manage_platform_operators.py.
  * Every route in platform_admin.http.ROUTES is exercised here.
"""
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_app_api_tenant_isolation import (ROLES, SERVER_KEY, TEST_DB, _assert_isolated_test_db, _login, _logout,  # noqa: E402
                                           _make_tenant)

OP_PASSWORD = 'operator-password-1'


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


def _ctx():
    import mcp_server.common as _common
    return _common.get_flask_app().app_context()


def _operator(role: str) -> tuple:
    """A console-only person, made the way the box makes one: the CLI, then the setup token."""
    from scripts import manage_platform_operators as cli
    import io
    from contextlib import redirect_stdout
    email = f'{role}_{uuid.uuid4().hex[:8]}@ops.test'
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(['create', '--email', email, '--name', f'Ops {role}', '--role', role]) == 0
    token = [ln for ln in buf.getvalue().splitlines() if 'setup token' in ln][0].rsplit(': ', 1)[1].strip()
    with _ctx():
        from app_api import auth as app_auth
        app_auth.consume_setup_token(token, OP_PASSWORD)
    return (email, OP_PASSWORD)


@pytest.fixture(scope='module')
def world(client):
    a, b = _make_tenant('alpha'), _make_tenant('bravo')
    with _ctx():
        import api_key_service as svc
        for t in (a, b):
            raw, rec = svc.generate_api_key(t['cid'], t['admin_user_id'], 'integration')
            t['key_id'], t['raw_key'] = rec.id, raw
    return {'A': a, 'B': b, 'operator': _operator('operator'), 'viewer': _operator('viewer')}


def _platform_requests(w: dict) -> list:
    """(method, path, body, is_write) — one per route, aimed at tenant B."""
    b = w['B']
    return [
        ('get', '/app/api/platform/overview', None, False),
        ('get', '/app/api/platform/tenants', None, False),
        ('get', f"/app/api/platform/tenants/{b['cid']}", None, False),
        ('get', f"/app/api/platform/audit?customer_id={b['cid']}", None, False),
        ('post', f"/app/api/platform/tenants/{b['cid']}/suspend", {'reason': 'test'}, True),
        ('post', f"/app/api/platform/tenants/{b['cid']}/reactivate", None, True),
        ('post', f"/app/api/platform/keys/{b['key_id']}/revoke", None, True),
        ('post', f"/app/api/platform/users/{b['admin_user_id']}/active", {'active': False}, True),
        ('post', f"/app/api/platform/users/{b['admin_user_id']}/reset-password", None, True),
        ('post', f"/app/api/platform/tenants/{b['cid']}/delete", {'confirm_name': 'whatever'}, True),
        ('get', '/app/api/platform/alerts', None, False),
        ('post', '/app/api/platform/alerts/recipients', {'protocol': 'email', 'endpoint': 'oncall@ops.test'}, True),
        ('post', '/app/api/platform/alerts/recipients/remove', {'id': 'arn:aws:sns:us-east-1:1:t:x'}, True),
    ]


def _call(client, method, path, body):
    return getattr(client, method)(path, json=body) if method == 'post' else client.get(path)


def _tenant_untouched(w):
    with _ctx():
        from extensions import db
        from models import Customer, CustomerApiKey, User
        b = w['B']
        assert db.session.get(Customer, b['cid']).suspended_at is None
        assert db.session.get(CustomerApiKey, b['key_id']).is_active is True
        u = db.session.get(User, b['admin_user_id'])
        assert u.active is True and u.magic_link_token is None


# ── who gets in ──

def test_no_session_is_refused_everywhere(client, world):
    _logout(client)
    for method, path, body, _w in _platform_requests(world):
        assert _call(client, method, path, body).status_code == 401, path
    _tenant_untouched(world)


@pytest.mark.parametrize('role', ROLES)
def test_every_tenant_role_is_refused_on_every_platform_route(client, world, role):
    """Including the tenant's own admin, and including a route aimed at their OWN tenant."""
    for tenant in ('A', 'B'):
        _login(client, world[tenant]['logins'][role])
        try:
            for method, path, body, _w in _platform_requests(world):
                r = _call(client, method, path, body)
                assert r.status_code == 403, f'{role}@{tenant} {method} {path} -> {r.status_code}'
                assert 'platform operators only' in r.json()['error']
        finally:
            _logout(client)
    _tenant_untouched(world)


def test_a_refused_attempt_is_audited(client, world):
    _login(client, world['A']['logins']['admin'])
    client.get('/app/api/platform/tenants')
    _logout(client)
    with _ctx():
        from models import ToolAuditLog
        row = ToolAuditLog.query.filter_by(tool='platform.tenants', outcome='denied').order_by(ToolAuditLog.id.desc()).first()
        assert row is not None and f"user:{world['A']['admin_user_id']}" in row.detail


def test_viewer_reads_everything_and_changes_nothing(client, world):
    _login(client, world['viewer'])
    try:
        for method, path, body, is_write in _platform_requests(world):
            r = _call(client, method, path, body)
            if is_write:
                assert r.status_code == 403 and 'read-only' in r.json()['error'], path
            else:
                assert r.status_code == 200, f'{path} -> {r.status_code} {r.text[:200]}'
    finally:
        _logout(client)
    _tenant_untouched(world)


def test_me_reports_platform_role_and_tenant_users_have_none(client, world):
    _login(client, world['operator'])
    me = client.get('/app/api/me').json()
    assert me['platform_role'] == 'operator' and me['customer_id'] is None
    _logout(client)
    _login(client, world['A']['logins']['admin'])
    assert client.get('/app/api/me').json()['platform_role'] is None
    _logout(client)


# ── what an operator sees ──

def test_operator_sees_across_tenants(client, world):
    _login(client, world['operator'])
    try:
        ov = client.get('/app/api/platform/overview').json()
        assert ov['totals']['tenants'] >= 2 and ov['totals']['accounts'] >= 2
        assert set(ov['system']) >= {'worker', 'interventions', 'journey_generator_version', 'stale_journeys'}
        assert isinstance(ov['attention'], list)
        rows = {t['customer_id']: t for t in client.get('/app/api/platform/tenants').json()['tenants']}
        for t in ('A', 'B'):
            row = rows[world[t]['cid']]
            assert row['status'] == 'active' and row['accounts'] == 1 and row['active_keys'] >= 1
            assert row['active_users'] == len(ROLES) * 2
        d = client.get(f"/app/api/platform/tenants/{world['B']['cid']}").json()
        assert d['tenant']['customer_id'] == world['B']['cid']
        assert [a['account_id'] for a in d['accounts']] == [world['B']['account_id']]
        assert d['accounts'][0]['journey'] == 'built'
        assert any(k['id'] == world['B']['key_id'] for k in d['keys'])
        assert all(u['email'].endswith('@t.test') for u in d['users'])
        assert client.get('/app/api/platform/tenants/99999999').status_code == 404
    finally:
        _logout(client)


def test_no_secret_ever_leaves_the_console(client, world):
    with _ctx():
        from models import CustomerApiKey, User
        from extensions import db
        key_hash = db.session.get(CustomerApiKey, world['B']['key_id']).key_hash
        pw_hash = db.session.get(User, world['B']['admin_user_id']).password_hash
    _login(client, world['operator'])
    try:
        blob = ''
        for method, path, body, is_write in _platform_requests(world):
            if not is_write:
                blob += client.get(path).text
        blob += client.get('/app/api/platform/audit?limit=500').text
    finally:
        _logout(client)
    assert key_hash not in blob and pw_hash not in blob
    assert world['B']['raw_key'] not in blob and world['A']['raw_key'] not in blob
    assert SERVER_KEY not in blob
    for word in ('key_hash', 'password_hash', 'magic_link_token', '"confidential"'):
        assert word not in blob, word


def test_viewing_a_tenant_is_itself_audited(client, world):
    _login(client, world['operator'])
    client.get(f"/app/api/platform/tenants/{world['A']['cid']}")
    _logout(client)
    with _ctx():
        from models import ToolAuditLog
        row = ToolAuditLog.query.filter_by(tool='platform.tenant_view', customer_id=world['A']['cid'], outcome='allowed') \
            .order_by(ToolAuditLog.id.desc()).first()
        assert row is not None and row.detail.startswith('operator:')


def test_audit_log_filters_and_pages(client, world):
    _login(client, world['operator'])
    try:
        r = client.get(f"/app/api/platform/audit?customer_id={world['A']['cid']}&limit=2").json()
        assert len(r['entries']) <= 2 and all(e['customer_id'] == world['A']['cid'] for e in r['entries'])
        denied = client.get('/app/api/platform/audit?outcome=denied&tool=platform.').json()['entries']
        assert denied and all(e['outcome'] == 'denied' and e['tool'].startswith('platform.') for e in denied)
        first = client.get('/app/api/platform/audit?limit=1').json()
        assert first['next_before_id'] == first['entries'][0]['id']
        second = client.get(f"/app/api/platform/audit?limit=1&before_id={first['next_before_id']}").json()
        assert second['entries'][0]['id'] < first['entries'][0]['id']
        assert client.get('/app/api/platform/audit?customer_id=abc').status_code == 400
    finally:
        _logout(client)


def test_attention_names_real_problems(client, world):
    """Unscoped users exist in both tenants (the fixture makes them); a failed journey build is stamped here."""
    with _ctx():
        from extensions import db
        from models import JourneyData
        from sqlalchemy.orm.attributes import flag_modified
        j = JourneyData.query.filter_by(customer_id=world['A']['cid']).first()
        j.journey_json = {**j.journey_json, 'build_error': {'error': 'boom', 'at': 'now'}}
        flag_modified(j, 'journey_json')
        db.session.commit()
    _login(client, world['operator'])
    try:
        items = client.get('/app/api/platform/overview').json()['attention']
        kinds = {(i['kind'], i['customer_id']) for i in items}
        assert ('journey_build_failed', world['A']['cid']) in kinds
        assert ('journey_build_failed', world['B']['cid']) not in kinds
        assert ('unscoped_users', world['A']['cid']) in kinds and ('unscoped_users', world['B']['cid']) in kinds
        assert [i['severity'] for i in items] == sorted((i['severity'] for i in items),
                                                        key=lambda s: {'critical': 0, 'warning': 1, 'info': 2}[s])
        d = client.get(f"/app/api/platform/tenants/{world['A']['cid']}").json()
        assert d['accounts'][0]['journey'] == 'failed'
        assert all(i['customer_id'] == world['A']['cid'] for i in d['attention'])
    finally:
        _logout(client)
        with _ctx():
            from extensions import db
            from models import JourneyData
            from sqlalchemy.orm.attributes import flag_modified
            j = JourneyData.query.filter_by(customer_id=world['A']['cid']).first()
            j.journey_json = {k: v for k, v in j.journey_json.items() if k != 'build_error'}
            flag_modified(j, 'journey_json')
            db.session.commit()


# ── the console is the only cross-tenant door ──

def test_an_interrupted_run_is_closed_and_reported_as_interrupted(client, world):
    """A run whose process was stopped part-way stays 'running' for ever unless something closes it.
    The worker's closer marks it failed with the reason; a fresh run is left alone; the console
    reports it as interrupted (a warning), not as a rejected run (critical)."""
    from datetime import datetime, timedelta
    w = world
    with _ctx():
        from extensions import db
        from models import ProcessRun
        from utils.process_runs import close_abandoned_runs, INTERRUPTED_PREFIX
        old = ProcessRun(run_id=f'pd_{uuid.uuid4().hex[:16]}', customer_id=w['A']['cid'], mode='auto', status='running',
                         started_at=datetime.utcnow() - timedelta(days=2))
        fresh = ProcessRun(run_id=f'pd_{uuid.uuid4().hex[:16]}', customer_id=w['B']['cid'], mode='auto', status='running',
                           started_at=datetime.utcnow() - timedelta(minutes=1))
        db.session.add_all([old, fresh]); db.session.commit()
        old_id, fresh_id = old.id, fresh.id
        assert close_abandoned_runs() >= 1
        old, fresh = db.session.get(ProcessRun, old_id), db.session.get(ProcessRun, fresh_id)
        assert old.status == 'failed' and old.finished_at and old.errors[-1].startswith(INTERRUPTED_PREFIX)
        assert fresh.status == 'running' and fresh.finished_at is None
        assert close_abandoned_runs() == 0                      # idempotent
        from platform_admin import service
        kinds = {(i['kind'], i['customer_id'], i['severity']) for i in service.attention()}
        assert ('process_run_interrupted', w['A']['cid'], 'warning') in kinds
        assert not any(k == 'process_run_failed' and c == w['A']['cid'] for k, c, _ in kinds)
        db.session.delete(old); db.session.delete(fresh); db.session.commit()


class _FakeAws:
    """SNS + CloudWatch, as much as platform_admin.alerts uses."""
    def __init__(self, topic):
        self.topic, self.subs, self.n = topic, [], 0
    def list_subscriptions_by_topic(self, TopicArn, **_):
        assert TopicArn == self.topic
        return {'Subscriptions': [dict(s) for s in self.subs]}
    def subscribe(self, TopicArn, Protocol, Endpoint, **_):
        self.subs.append({'SubscriptionArn': 'PendingConfirmation', 'Protocol': Protocol, 'Endpoint': Endpoint})
        return {'SubscriptionArn': 'pending confirmation'}
    def confirm(self, endpoint):
        self.n += 1
        next(s for s in self.subs if s['Endpoint'] == endpoint)['SubscriptionArn'] = f'{self.topic}:{self.n}'
    def unsubscribe(self, SubscriptionArn):
        self.subs = [s for s in self.subs if s['SubscriptionArn'] != SubscriptionArn]
    def describe_alarms(self, AlarmNamePrefix):
        from datetime import datetime
        return {'MetricAlarms': [{'AlarmName': f'{AlarmNamePrefix}-public-health', 'StateValue': 'ALARM', 'StateReason': 'health check failing',
                                  'StateUpdatedTimestamp': datetime(2026, 11, 14, 20, 55), 'AlarmActions': [self.topic]}]}
    def describe_alarm_history(self, AlarmName, **_):
        from datetime import datetime
        return {'AlarmHistoryItems': [{'Timestamp': datetime(2026, 11, 14, 20, 55), 'HistorySummary': 'Alarm updated from OK to ALARM'}]}


@pytest.fixture
def aws(monkeypatch, tmp_path):
    from platform_admin import alerts
    topic = 'arn:aws:sns:us-east-1:000000000000:test-alerts'
    fake = _FakeAws(topic)
    log = tmp_path / 'alert.log'
    log.write_text('2026-11-14T20:55:02Z FAILING:\n- API is not answering on /health\n- root disk is 91% full\n2026-11-14T21:00:01Z recovered\n')
    monkeypatch.setenv('ALERT_SNS_TOPIC_ARN', topic)
    monkeypatch.setenv('ALERT_LOG_PATH', str(log))
    monkeypatch.setattr(alerts, '_clients', lambda: (fake, fake))
    return fake


def test_alerts_without_a_topic_say_so_and_never_call_aws(client, world, monkeypatch):
    from platform_admin import alerts
    monkeypatch.delenv('ALERT_SNS_TOPIC_ARN', raising=False)
    monkeypatch.setattr(alerts, '_clients', lambda: (_ for _ in ()).throw(AssertionError('AWS must not be called')))
    _login(client, world['operator'])
    try:
        r = client.get('/app/api/platform/alerts').json()
        assert r['configured'] is False and 'not sent to anyone' in r['error'] and r['recipients'] == []
        assert client.post('/app/api/platform/alerts/recipients', json={'protocol': 'email', 'endpoint': 'a@b.co'}).status_code == 400
    finally:
        _logout(client)


def test_alerts_show_alarms_history_host_checks_and_survive_an_aws_failure(client, world, aws, monkeypatch):
    _login(client, world['viewer'])
    try:
        r = client.get('/app/api/platform/alerts').json()
        assert r['configured'] and r['error'] is None and r['topic'] == 'test-alerts'
        assert r['alarms'][0]['state'] == 'ALARM' and r['alarms'][0]['notifies'] is True
        assert r['history'][0]['summary'].endswith('OK to ALARM')
        hc = r['host_checks']
        assert hc['available'] and hc['state'] == 'ok' and [e['kind'] for e in hc['events']] == ['recovered', 'failing']
        assert hc['events'][1]['problems'] == ['API is not answering on /health', 'root disk is 91% full']
        from platform_admin import alerts
        monkeypatch.setattr(alerts, '_clients', lambda: (_ for _ in ()).throw(RuntimeError('no credentials')))
        r = client.get('/app/api/platform/alerts')
        assert r.status_code == 200 and 'no credentials' in r.json()['error'] and r.json()['host_checks']['available']
    finally:
        _logout(client)


def test_operator_manages_alert_recipients(client, world, aws):
    _login(client, world['operator'])
    try:
        add = lambda p, e: client.post('/app/api/platform/alerts/recipients', json={'protocol': p, 'endpoint': e})
        remove = lambda i: client.post('/app/api/platform/alerts/recipients/remove', json={'id': i})
        for p, e in (('email', 'not-an-address'), ('sms', '415-555-0123'), ('https', 'https://x.test/hook'), ('email', '')):
            assert add(p, e).status_code == 400, (p, e)
        r = add('email', 'OnCall@Ops.test')
        assert r.status_code == 200
        assert r.json()['recipients'] == [{'id': None, 'protocol': 'email', 'endpoint': 'oncall@ops.test', 'status': 'pending'}]
        assert add('email', 'oncall@ops.test').status_code == 400                      # already there
        assert add('sms', '+14155550123').status_code == 200
        aws.confirm('oncall@ops.test')
        first = next(x for x in client.get('/app/api/platform/alerts').json()['recipients'] if x['status'] == 'confirmed')
        assert remove(first['id']).status_code == 400                                  # the only confirmed one stays
        assert remove('arn:aws:sns:us-east-1:000000000000:someone-elses-topic:1').status_code == 404
        assert remove(None).status_code == 404
        aws.confirm('+14155550123')
        r = remove(first['id'])
        assert r.status_code == 200 and [x['endpoint'] for x in r.json()['recipients']] == ['+14155550123']
    finally:
        _logout(client)
    with _ctx():
        from models import ToolAuditLog
        tools = [t.tool for t in ToolAuditLog.query.filter(ToolAuditLog.tool.like('platform.alert_recipient_%')).all()]
        assert 'platform.alert_recipient_add' in tools and 'platform.alert_recipient_remove' in tools


def test_deletion_plan_covers_every_table_in_the_schema(client, world):
    """A table added later must be deletable with its tenant, or plan() must refuse — never silently left behind."""
    with _ctx():
        from extensions import db
        from sqlalchemy import inspect
        from platform_admin import deletion
        planned = [t for t, _ in deletion.plan()]
        assert planned[-1] == 'customers' and len(planned) == len(set(planned))
        assert set(planned) | set(deletion.KEPT) | set(deletion.NOT_TENANT_DATA) == set(inspect(db.engine).get_table_names())


def test_delete_tenant_is_guarded_archived_complete_and_audited(client, world, monkeypatch):
    from platform_admin import deletion
    doomed, bystander = _make_tenant('doomed'), world['A']
    with _ctx():
        from extensions import db
        from models import Account, Customer
        import api_key_service as svc
        raw, _rec = svc.generate_api_key(doomed['cid'], doomed['admin_user_id'], 'integration')
        name = db.session.get(Customer, doomed['cid']).customer_name
        acct = Account.query.filter_by(customer_id=doomed['cid']).first()
        assert acct is not None, 'fixture tenant should have an account'
        before_other = {t: db.session.execute(db.text(f'SELECT count(*) FROM "{t}" WHERE {w}'), {'cid': bystander['cid']}).scalar()
                        for t, w in deletion.plan()}
    real_store = deletion._store_archive
    stored = {}
    path = f"/app/api/platform/tenants/{doomed['cid']}/delete"
    _login(client, world['operator'])
    try:
        def fail(*_a):
            raise AssertionError('nothing may be archived before every guard passes')
        monkeypatch.setattr(deletion, '_store_archive', fail)
        r = client.post(path, json={'confirm_name': name})
        assert r.status_code == 400 and 'suspend the tenant first' in r.json()['error']
        assert client.post(f"/app/api/platform/tenants/{doomed['cid']}/suspend", json={'reason': 'offboarding'}).status_code == 200
        for wrong in (None, '', name.lower() + ' ', name[:-1]):
            assert client.post(path, json={'confirm_name': wrong}).status_code == 400, wrong
        assert client.post('/app/api/platform/tenants/99999999/delete', json={'confirm_name': 'x'}).status_code == 404

        # the archive cannot be written -> nothing is deleted
        monkeypatch.setattr(deletion, '_store_archive', lambda n, b: (_ for _ in ()).throw(RuntimeError('bucket unreachable')))
        r = client.post(path, json={'confirm_name': name})
        assert r.status_code == 400 and 'nothing was deleted' in r.json()['error']
        monkeypatch.delenv('BACKUP_S3_URI', raising=False)
        monkeypatch.setattr(deletion, '_store_archive', real_store)       # the real one, with nowhere to write
        r = client.post(path, json={'confirm_name': name})
        assert r.status_code == 400 and 'BACKUP_S3_URI' in r.json()['error']
        with _ctx():
            from extensions import db
            from models import Customer
            assert db.session.get(Customer, doomed['cid']) is not None

        def store(n, body):
            stored[n] = body
            return f's3://test-bucket/deleted-tenants/{n}'
        monkeypatch.setattr(deletion, '_store_archive', store)
        r = client.post(path, json={'confirm_name': name})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out['deleted'] and out['rows']['customers'] == 1 and out['rows']['users'] >= 1 and out['archive'].startswith('s3://test-bucket/')
        assert client.get(f"/app/api/platform/tenants/{doomed['cid']}").status_code == 404
        assert client.post(path, json={'confirm_name': name}).status_code == 404
        for p in ('/app/api/platform/overview', '/app/api/platform/tenants', f"/app/api/platform/audit?customer_id={doomed['cid']}"):
            assert client.get(p).status_code == 200, p                      # the console still loads without the tenant
    finally:
        _logout(client)

    import gzip, json as _json_mod
    (fname, body), = stored.items()
    archive = _json_mod.loads(gzip.decompress(body))
    assert fname.startswith(f"tenant-{doomed['cid']}-") and archive['tenant']['name'] == name
    assert {t: len(v) for t, v in archive['tables'].items() if v} == out['rows']
    with _ctx():
        from extensions import db
        from models import ToolAuditLog
        import api_key_service as svc
        for t, w in deletion.plan():
            assert db.session.execute(db.text(f'SELECT count(*) FROM "{t}" WHERE {w}'), {'cid': doomed['cid']}).scalar() == 0, t
            assert db.session.execute(db.text(f'SELECT count(*) FROM "{t}" WHERE {w}'), {'cid': bystander['cid']}).scalar() == before_other[t], t
        assert svc.validate_api_key(raw) is None
        rows = ToolAuditLog.query.filter_by(customer_id=doomed['cid']).all()
        deleted = [x for x in rows if x.tool == 'platform.tenant_delete']
        assert len(deleted) == 1 and 'archive s3://test-bucket/' in deleted[0].detail and f'operator:' in deleted[0].detail
        assert any(x.tool == 'platform.tenant_suspend' for x in rows)       # earlier history outlives the tenant
    assert client.post('/app/api/auth/login', json={'email': doomed['logins']['admin'][0], 'password': doomed['logins']['admin'][1]}).status_code in (400, 401, 403)


def test_platform_role_opens_nothing_on_tenant_routes(client, world):
    a = world['A']
    _login(client, world['operator'])
    try:
        for path in (f"/app/api/portfolio?customer_id={a['cid']}", f"/app/api/accounts/{a['account_id']}?customer_id={a['cid']}",
                     f"/app/api/interventions?customer_id={a['cid']}", f"/app/api/users?customer_id={a['cid']}",
                     f"/app/api/roi?customer_id={a['cid']}"):
            assert client.get(path).status_code == 403, path
    finally:
        _logout(client)


def test_granting_the_console_to_a_tenant_user_leaves_their_tenant_scope_alone(client, world):
    from scripts import manage_platform_operators as cli
    email = world['A']['logins']['csm'][0]
    assert cli.main(['grant', '--email', email, '--role', 'viewer']) == 0
    _login(client, world['A']['logins']['csm'])
    try:
        assert client.get('/app/api/platform/tenants').status_code == 200
        assert client.get(f"/app/api/portfolio?customer_id={world['A']['cid']}").status_code == 200
        assert client.get(f"/app/api/portfolio?customer_id={world['B']['cid']}").status_code == 403      # still their tenant only
        assert client.post(f"/app/api/platform/tenants/{world['B']['cid']}/suspend", json={'reason': 'x'}).status_code == 403
    finally:
        _logout(client)
    assert cli.main(['revoke', '--email', email]) == 0
    _login(client, world['A']['logins']['csm'])                      # a tenant user keeps their login when the console goes
    assert client.get('/app/api/platform/tenants').status_code == 403
    _logout(client)
    assert cli.main(['grant', '--email', email, '--role', 'owner']) == 2          # not a role
    assert cli.main(['grant', '--email', 'nobody@nowhere.test']) == 2


def test_no_http_route_can_set_platform_role(client, world):
    """A tenant admin editing a user (the only user-writing route) cannot smuggle the flag in."""
    a = world['A']
    _login(client, a['logins']['admin'])
    try:
        users = client.get(f"/app/api/users?customer_id={a['cid']}").json()['users']
        target = [u for u in users if u['role'] == 'cro'][0]
        client.patch(f"/app/api/users/{target['user_id']}", json={'customer_id': a['cid'], 'platform_role': 'operator', 'role': 'cro'})
        client.post('/app/api/users', json={'customer_id': a['cid'], 'email': f'x{uuid.uuid4().hex[:6]}@t.test', 'name': 'X',
                                            'role': 'csm', 'platform_role': 'operator'})
    finally:
        _logout(client)
    with _ctx():
        from models import User
        assert User.query.filter(User.customer_id == a['cid'], User.platform_role.isnot(None)).count() == 0


# ── suspension ──

def test_suspend_refuses_logins_sessions_and_keys_and_reactivate_restores_them(client, world):
    from starlette.testclient import TestClient
    b = world['B']
    with _ctx():
        import api_key_service as svc
        assert svc.validate_api_key(b['raw_key']) is not None
    tenant_client = TestClient(client.app)                              # a second browser, already logged in
    _login(tenant_client, b['logins']['csm'])
    assert tenant_client.get(f"/app/api/portfolio?customer_id={b['cid']}").status_code == 200

    _login(client, world['operator'])
    try:
        assert client.post(f"/app/api/platform/tenants/{b['cid']}/suspend", json={}).status_code == 400      # a reason is required
        r = client.post(f"/app/api/platform/tenants/{b['cid']}/suspend", json={'reason': 'contract ended'})
        assert r.status_code == 200 and r.json()['status'] == 'suspended'

        assert tenant_client.get(f"/app/api/portfolio?customer_id={b['cid']}").status_code == 401            # live session dies
        assert tenant_client.post('/app/api/auth/login', json={'email': b['logins']['admin'][0],
                                                               'password': b['logins']['admin'][1]}).status_code != 200
        with _ctx():
            import api_key_service as svc
            assert svc.validate_api_key(b['raw_key']) is None                                                # keys refused
            assert svc.validate_api_key(world['A']['raw_key']) is not None                                   # the other tenant is untouched
            from models import Account, Customer
            from extensions import db
            assert Account.query.filter_by(customer_id=b['cid']).count() == 1                                # nothing deleted
            assert db.session.get(Customer, b['cid']).suspended_reason == 'contract ended'

        rows = {t['customer_id']: t for t in client.get('/app/api/platform/tenants').json()['tenants']}
        assert rows[b['cid']]['status'] == 'suspended' and rows[world['A']['cid']]['status'] == 'active'
        assert client.get(f"/app/api/platform/tenants/{b['cid']}").status_code == 200                        # the operator still sees it

        r = client.post(f"/app/api/platform/tenants/{b['cid']}/reactivate")
        assert r.status_code == 200 and r.json()['status'] == 'active'
        assert client.post('/app/api/platform/tenants/99999999/suspend', json={'reason': 'x'}).status_code == 404
    finally:
        _logout(client)
    with _ctx():
        import api_key_service as svc
        assert svc.validate_api_key(b['raw_key']) is not None
        from models import ToolAuditLog
        tools = [r.tool for r in ToolAuditLog.query.filter_by(customer_id=b['cid'], outcome='allowed')
                 .filter(ToolAuditLog.tool.in_(['platform.tenant_suspend', 'platform.tenant_reactivate'])).order_by(ToolAuditLog.id).all()]
        assert tools[-2:] == ['platform.tenant_suspend', 'platform.tenant_reactivate']
    _login(tenant_client, b['logins']['csm'])
    assert tenant_client.get(f"/app/api/portfolio?customer_id={b['cid']}").status_code == 200
    tenant_client.close()


def test_a_tenant_user_with_the_console_cannot_suspend_their_own_tenant(client, world):
    from scripts import manage_platform_operators as cli
    email = world['A']['logins']['cfo'][0]
    assert cli.main(['grant', '--email', email, '--role', 'operator']) == 0
    _login(client, world['A']['logins']['cfo'])
    try:
        r = client.post(f"/app/api/platform/tenants/{world['A']['cid']}/suspend", json={'reason': 'oops'})
        assert r.status_code == 400 and 'your own login' in r.json()['error']
    finally:
        _logout(client)
        assert cli.main(['revoke', '--email', email]) == 0


# ── keys and users ──

def test_operator_revokes_a_key(client, world):
    with _ctx():
        import api_key_service as svc
        raw, _rec = svc.generate_api_key(world['A']['cid'], world['A']['admin_user_id'], 'to-revoke')
        kid = _rec.id
    _login(client, world['operator'])
    try:
        r = client.post(f'/app/api/platform/keys/{kid}/revoke')
        assert r.status_code == 200 and r.json()['is_active'] is False and 'key_hash' not in r.json()
        assert client.post('/app/api/platform/keys/99999999/revoke').status_code == 404
    finally:
        _logout(client)
    with _ctx():
        import api_key_service as svc
        assert svc.validate_api_key(raw) is None
        assert svc.validate_api_key(world['A']['raw_key']) is not None


def test_operator_deactivates_and_resets_a_tenant_user(client, world):
    a = world['A']
    with _ctx():
        from models import User
        uid = User.query.filter_by(email=a['logins']['vpcsm'][0]).first().user_id
    _login(client, world['operator'])
    try:
        assert client.post(f'/app/api/platform/users/{uid}/active', json={'active': 'no'}).status_code == 400
        r = client.post(f'/app/api/platform/users/{uid}/active', json={'active': False})
        assert r.status_code == 200 and r.json()['active'] is False
        probe = client.__class__(client.app)
        assert probe.post('/app/api/auth/login', json={'email': a['logins']['vpcsm'][0],
                                                       'password': a['logins']['vpcsm'][1]}).status_code != 200
        assert client.post(f'/app/api/platform/users/{uid}/active', json={'active': True}).json()['active'] is True
        token = client.post(f'/app/api/platform/users/{uid}/reset-password').json()['setup_token']
        assert probe.post('/app/api/auth/set-password', json={'token': token, 'new_password': 'a-brand-new-password-1'}).status_code == 200
        assert probe.post('/app/api/auth/login', json={'email': a['logins']['vpcsm'][0],
                                                       'password': 'a-brand-new-password-1'}).status_code == 200
        probe.close()
        assert client.post('/app/api/platform/users/99999999/active', json={'active': False}).status_code == 404
    finally:
        _logout(client)


def test_operators_are_not_managed_from_the_browser(client, world):
    with _ctx():
        from models import User
        viewer_id = User.query.filter_by(email=world['viewer'][0]).first().user_id
        me_id = User.query.filter_by(email=world['operator'][0]).first().user_id
    _login(client, world['operator'])
    try:
        assert client.post(f'/app/api/platform/users/{viewer_id}/active', json={'active': False}).status_code == 403
        assert client.post(f'/app/api/platform/users/{viewer_id}/reset-password').status_code == 403
        r = client.post(f'/app/api/platform/users/{me_id}/active', json={'active': False})
        assert r.status_code == 400 and 'your own login' in r.json()['error']
    finally:
        _logout(client)
    _login(client, world['viewer'])                                 # still works
    _logout(client)


# ── the operator CLI ──

def test_cli_create_revoke_and_list(client, world, capsys):
    from scripts import manage_platform_operators as cli
    email = f'temp_{uuid.uuid4().hex[:8]}@ops.test'
    assert cli.main(['create', '--email', email, '--name', 'Temp']) == 0
    assert cli.main(['create', '--email', email, '--name', 'Temp']) == 2          # already exists
    assert cli.main(['list']) == 0
    assert email in capsys.readouterr().out
    assert cli.main(['reset', '--email', email]) == 0
    assert cli.main(['revoke', '--email', email]) == 0
    with _ctx():
        from models import ToolAuditLog, User
        u = User.query.filter_by(email=email).first()
        assert u.platform_role is None and u.active is False and u.customer_id is None      # console-only login is closed
        assert ToolAuditLog.query.filter_by(tool='platform.operator_create').count() >= 1
    assert cli.main(['reset', '--email', world['A']['logins']['admin'][0]]) == 2    # tenant users are not reset here


# ── the route table ──

def test_every_platform_route_is_exercised_here(world):
    from platform_admin.http import ROUTES
    covered = set()
    for _m, path, _b, _w in _platform_requests(world):
        covered.add('/'.join('{id}' if seg.isdigit() else seg for seg in path.split('?')[0].split('/')))
    assert covered == set(ROUTES)

