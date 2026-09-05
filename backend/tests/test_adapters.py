"""
Adapters (docs/design/adapters.md) on a real Postgres tenant:
  * the reference receiver alone, against a fake platform: a signed payload is accepted and answered with
    `started` then `done` (policy auto_done) over HTTP with the platform key; a replay is acknowledged, not
    re-processed (also across a restart, from the JSONL log); missing / bad / stale signatures are 401 and never
    logged as received; the callback retries on a non-2xx; policy manual reports only `started`
  * the receiver against the real governance layer, both on uvicorn threads: approve → delivered → the callback
    moves the row to sent + started, then closed done; a wrong secret → 401 at the receiver and a FAILED delivery
    (http_status 401, two attempts) on the platform
  * Slack: the URL is validated, stored, never returned; a notify-class approval gets delivery.slack as a second
    entry, other classes do not; a failed Slack post does not mark the webhook delivery failed
  * Gainsight Timeline sample end to end: parse report, unknown account and rejected rows reported, journeys
    rebuilt, a second import writes nothing (source_ref), dry_run writes nothing
  * tools and routes are registered and keyed
"""
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
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
from models import Account, Intervention, QualitativeSignal, ToolAuditLog, JourneyData   # noqa: E402

SAMPLE = BACKEND / 'adapters' / 'sources' / 'samples' / 'gainsight_timeline_sample.csv'


# ── a capturing HTTP endpoint (fake platform / fake Slack) ────────────

class _Capture:
    """Records every POST; answers with the next status in `statuses` (last one repeats)."""

    def __init__(self, statuses=(200,)):
        self.requests, self.statuses = [], list(statuses)
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n).decode('utf-8')
                outer.requests.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
                status = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}' if status < 400 else b'{"error":"nope"}')

            def log_message(self, *a):
                pass

        self.server = HTTPServer(('127.0.0.1', 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def stop(self):
        self.server.shutdown()


def _serve(asgi_app):
    """Run an ASGI app on a uvicorn thread; returns (base_url, stop)."""
    import socket
    import uvicorn
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(asgi_app, host='127.0.0.1', port=port, log_level='warning'))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, 'uvicorn did not start'

    def stop():
        server.should_exit = True
        t.join(10)
    return f'http://127.0.0.1:{port}', stop


def _signed(secret, payload: dict, ts=None):
    from playbooks.webhook import sign
    from adapters import settings
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    ts = str(int(time.time()) if ts is None else ts)
    c = settings.contract()
    return body, {c['signature_header']: sign(secret, ts, body), c['timestamp_header']: ts, 'Content-Type': 'application/json'}


def _payload(iid=101, cid=7):
    return {'event': 'intervention.approved', 'intervention_id': iid, 'customer_id': cid,
            'playbook': {'id': 'expansion_intent_handoff', 'version': '1.0', 'action_class': 'notify'},
            'account': {'id': 1, 'name': 'Orchard Retail', 'external_id': 'ORC'}, 'urgency': 'high',
            'trigger': [{'episode_id': 'sig:1', 'node_id': 1, 'role': 'expansion_intent', 'quote': 'wants 40 more seats'}],
            'approved_by': 'local', 'callback': {'route': f'/api/interventions/{iid}/report', 'intervention_id': iid}}


@pytest.fixture
def fast_receiver(monkeypatch):
    """No real waits inside the receiver's callback threads."""
    import adapters.receiver.app as rapp
    monkeypatch.setattr(rapp, '_sleep', lambda s: None)


def _receiver(tmp_path, platform_url, secret='rcv-' + uuid.uuid4().hex, **kw):
    from adapters.receiver import ReceiverConfig, create_app
    cfg = ReceiverConfig(secret=secret, platform_url=platform_url, platform_key='csp_test_key', log_path=str(tmp_path / 'events.jsonl'), **kw)
    return create_app(cfg), cfg


def _log(cfg):
    return [json.loads(l) for l in open(cfg.log_path, encoding='utf-8') if l.strip()]


# ── the receiver alone ────────────────────────────────────────────────

def test_receiver_accepts_a_signed_payload_and_calls_back_started_then_done(tmp_path, fast_receiver):
    from starlette.testclient import TestClient
    platform = _Capture()
    try:
        rapp, cfg = _receiver(tmp_path, platform.url)
        client = TestClient(rapp)
        assert client.get('/health').json()['policy'] == 'auto_done'
        body, headers = _signed(cfg.secret, _payload())
        r = client.post('/hook', content=body, headers=headers)
        assert r.status_code == 200 and r.json() == {'status': 'accepted', 'intervention_id': 101, 'policy': 'auto_done'}
        rapp.state.receiver.drain()
        assert [json.loads(x['body'])['state'] for x in platform.requests] == ['started', 'done']
        assert all(x['path'] == '/api/interventions/101/report' and x['headers']['authorization'] == 'Bearer csp_test_key' for x in platform.requests)
        assert json.loads(platform.requests[0]['body'])['customer_id'] == 7
        events = _log(cfg)
        assert [e['event'] for e in events] == ['received', 'callback', 'callback']
        assert events[0]['payload']['intervention_id'] == 101 and events[0]['playbook_id'] == 'expansion_intent_handoff'
        assert [e['status'] for e in events[1:]] == ['ok', 'ok']
        seen = client.get('/received').json()['received']['101']
        assert [c['state'] for c in seen['callbacks']] == ['started', 'done']
    finally:
        platform.stop()


def test_receiver_replay_is_acknowledged_not_reprocessed_also_after_a_restart(tmp_path, fast_receiver):
    from starlette.testclient import TestClient
    from adapters.receiver import ReceiverConfig, create_app
    platform = _Capture()
    try:
        rapp, cfg = _receiver(tmp_path, platform.url)
        client = TestClient(rapp)
        body, headers = _signed(cfg.secret, _payload(iid=202))
        assert client.post('/hook', content=body, headers=headers).json()['status'] == 'accepted'
        rapp.state.receiver.drain()
        n = len(platform.requests)
        r = client.post('/hook', content=body, headers=headers)              # the platform's retry / a queue replay
        assert r.status_code == 200 and r.json()['status'] == 'already_received' and r.json()['intervention_id'] == 202
        body2, headers2 = _signed(cfg.secret, _payload(iid=202))            # fresh timestamp, same intervention
        assert client.post('/hook', content=body2, headers=headers2).json()['status'] == 'already_received'
        rapp.state.receiver.drain()
        assert len(platform.requests) == n
        assert [e['event'] for e in _log(cfg)][-2:] == ['replay', 'replay']
        # restart: the log is the memory
        again = create_app(ReceiverConfig(secret=cfg.secret, platform_url=platform.url, platform_key='k', log_path=cfg.log_path))
        assert again.state.receiver.seen[202]['reloaded'] and [c['state'] for c in again.state.receiver.seen[202]['callbacks']] == ['started', 'done']
        assert TestClient(again).post('/hook', content=body2, headers=headers2).json()['status'] == 'already_received'
        assert len(platform.requests) == n
    finally:
        platform.stop()


def test_receiver_refuses_missing_bad_and_stale_signatures(tmp_path, fast_receiver):
    from starlette.testclient import TestClient
    from adapters import settings
    platform = _Capture()
    try:
        rapp, cfg = _receiver(tmp_path, platform.url, customer_id=7)
        client = TestClient(rapp)
        c = settings.contract()
        body, headers = _signed(cfg.secret, _payload(iid=303))
        cases = {
            'missing_signature': {k: v for k, v in headers.items() if k != c['signature_header']},
            'bad_signature': _signed('wrong-secret', _payload(iid=303))[1],
            'stale_timestamp': _signed(cfg.secret, _payload(iid=303), ts=int(time.time()) - cfg.timestamp_tolerance_seconds - 5)[1],
            'bad_timestamp': {**headers, c['timestamp_header']: 'yesterday'},
        }
        for reason, h in cases.items():
            r = client.post('/hook', content=body, headers=h)
            assert r.status_code == 401 and r.json() == {'error': reason}, reason
        # a signature made with the platform's own sign() over a tampered body is refused too
        assert client.post('/hook', content=body.replace('"intervention_id":303', '"intervention_id":304'), headers=headers).status_code == 401
        # authentic but unusable payload / wrong tenant
        b2, h2 = _signed(cfg.secret, {'hello': 'world'})
        assert client.post('/hook', content=b2, headers=h2).status_code == 400
        b3, h3 = _signed(cfg.secret, _payload(iid=305, cid=8))
        assert client.post('/hook', content=b3, headers=h3).status_code == 403
        # nothing was received, nothing called back
        rapp.state.receiver.drain()
        assert platform.requests == [] and rapp.state.receiver.seen == {}
        assert {e['event'] for e in _log(cfg)} == {'refused'} and {e['reason'] for e in _log(cfg)} >= set(cases) | {'bad_payload', 'wrong_tenant'}
        big = 'x' * (cfg.max_body_bytes + 1)
        assert client.post('/hook', content=big, headers=headers).status_code == 413
    finally:
        platform.stop()


def test_receiver_callback_retries_and_manual_policy(tmp_path, fast_receiver):
    from starlette.testclient import TestClient
    platform = _Capture(statuses=[400, 200])           # the platform is still committing `sent` on the first call
    try:
        rapp, cfg = _receiver(tmp_path, platform.url, policy='manual')
        client = TestClient(rapp)
        body, headers = _signed(cfg.secret, _payload(iid=404))
        assert client.post('/hook', content=body, headers=headers).json()['policy'] == 'manual'
        rapp.state.receiver.drain()
        states = [json.loads(x['body'])['state'] for x in platform.requests]
        assert states == ['started', 'started']                 # retried once, then nothing more: manual never reports done
        cb = rapp.state.receiver.seen[404]['callbacks']
        assert len(cb) == 1 and cb[0]['status'] == 'ok' and cb[0]['attempts'] == 2
    finally:
        platform.stop()


def test_receiver_config_is_validated_and_read_from_env(tmp_path, monkeypatch):
    from adapters.receiver import ReceiverConfig
    from adapters import settings
    env = settings.get('receiver', 'env')
    with pytest.raises(ValueError, match='secret'):
        ReceiverConfig(secret='', platform_url='http://x', platform_key='k')
    with pytest.raises(ValueError, match='platform URL'):
        ReceiverConfig(secret='s', platform_url='not-a-url', platform_key='k')
    with pytest.raises(ValueError, match='policy'):
        ReceiverConfig(secret='s', platform_url='http://x', platform_key='k', policy='whenever')
    for k, v in {'secret': 's3', 'platform_url': 'http://platform.test/', 'platform_key': 'k3', 'customer_id': '10',
                 'policy': 'manual', 'auto_done_after_seconds': '7', 'log_path': str(tmp_path / 'e.jsonl')}.items():
        monkeypatch.setenv(env[k], v)
    cfg = ReceiverConfig.from_env(port=None)
    assert (cfg.secret, cfg.platform_url, cfg.customer_id, cfg.policy, cfg.auto_done_after_seconds) == ('s3', 'http://platform.test', 10, 'manual', 7.0)
    assert ReceiverConfig.from_env(policy='auto_done').policy == 'auto_done'          # explicit wins


# ── a tenant with proposals ───────────────────────────────────────────

def _sig(cid, aid, subtype, when, text):
    from mcp_server.cs_pulse_onboarding import submit_signal
    return submit_signal(cid, aid, text, source_type='crm_activity', signal_type=subtype, occurred_at=when, process_now=False)


def _make_tenant(tag, accounts):
    from mcp_server.cs_pulse_onboarding import create_customer
    cid = create_customer(name=f'Adapters {tag}', domain=f'ad-{tag}.test', vertical='saas_premium',
                          admin_email=f'ad_{tag}@t.test', admin_name='A', data_origin='synthetic_test')['customer_id']
    ids = {}
    for name, ext, pm, rev in accounts:
        a = Account(customer_id=cid, account_name=name, revenue=rev, vertical='saas_premium', external_account_id=ext, profile_metadata=pm)
        db.session.add(a); db.session.commit()
        ids[ext] = a.account_id
    return cid, ids


@pytest.fixture(scope='module')
def tenant():
    os.environ['PLAYBOOK_WEBHOOK_ALLOW_HTTP'] = 'true'
    with app.app_context():
        db.create_all()
        from signal_engine.pipeline import process_pending
        from journeys.wizard_a import run_wizard_a
        from playbooks.governance import evaluate
        cid, ids = _make_tenant(uuid.uuid4().hex[:8], [
            ('Northstar Mutual', 'NOR', {'primary_champion_name': 'Dana Whitfield', 'renewal_date': '2026-10-15'}, 420_000),
            ('Orchard Retail', 'ORC', {'renewal_date': '2027-03-01'}, 180_000),
            ('Harbor Bank', 'HAR', {'renewal_date': '2027-05-01'}, 90_000),
        ])
        _sig(cid, ids['NOR'], 'champion_departure', '2026-08-04T10:00:00Z', 'Dana Whitfield is leaving at the end of the month')
        _sig(cid, ids['NOR'], 'budget_pressure', '2026-08-12T10:00:00Z', 'Procurement wants a 20% reduction at renewal')
        _sig(cid, ids['NOR'], 'seat_underutilization', '2026-08-20T10:00:00Z', '140 of 300 seats have not logged in this quarter')
        _sig(cid, ids['ORC'], 'expansion_interest', '2026-08-18T10:00:00Z', 'Ops team asked for 40 more seats for the new region')
        _sig(cid, ids['HAR'], 'executive_escalation', '2026-08-22T10:00:00Z', 'Their COO escalated the outage to our CEO')
        process_pending(customer_id=cid, limit=100, rebuild_journeys=False)
        run_wizard_a(cid, evaluate_playbooks=False)
        out = evaluate(cid)
        assert len(out['proposed']) >= 4
        yield cid, ids
        db.session.remove()
        db.drop_all()
    os.environ.pop('PLAYBOOK_WEBHOOK_ALLOW_HTTP', None)


def _row(cid, playbook_id):
    return Intervention.query.filter_by(customer_id=cid, playbook_id=playbook_id).order_by(Intervention.id).first()


# ── the receiver against the real governance layer ───────────────────

@pytest.fixture(scope='module')
def platform_http(tenant):
    """The platform ASGI app on a uvicorn thread with a server key (the receiver calls it back over HTTP)."""
    key = 'srv-' + uuid.uuid4().hex
    os.environ['MCP_SERVER_API_KEY'] = key
    os.environ['MCP_AUTH_REQUIRED'] = 'true'
    import mcp_server.auth as auth
    prev_key = auth.MCP_SERVER_API_KEY
    auth.MCP_SERVER_API_KEY = key
    from server import build_asgi_app
    base, stop = _serve(build_asgi_app(TEST_DB, create_schema=False))
    yield base, key
    stop()
    auth.MCP_SERVER_API_KEY = prev_key
    os.environ['MCP_TRANSPORT'] = 'stdio'


def _wait(pred, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            v = pred()
        if v:
            return v
        time.sleep(0.2)
    return None


def test_receiver_closes_the_loop_with_the_real_governance_layer(tenant, platform_http, tmp_path, monkeypatch):
    import httpx
    from adapters.receiver import ReceiverConfig, create_app
    from playbooks import webhook
    cid, ids = tenant
    base, key = platform_http
    secret = 'shared-' + uuid.uuid4().hex
    cfg = ReceiverConfig(secret=secret, platform_url=base, platform_key=key, customer_id=cid, log_path=str(tmp_path / 'live.jsonl'),
                         auto_done_after_seconds=0.5, callback={'timeout_seconds': 10, 'initial_delay_seconds': 0.3, 'retries': 4, 'retry_delay_seconds': 0.3})
    rapp = create_app(cfg)
    rbase, rstop = _serve(rapp)
    try:
        auth = {'Authorization': f'Bearer {key}'}
        r = httpx.post(f'{base}/api/playbooks', json={'customer_id': cid, 'webhook_url': f'{rbase}/hook', 'webhook_secret': secret}, headers=auth)
        assert r.status_code == 200 and r.json()['webhook_secret_set'] and secret not in r.text
        with app.app_context():
            champ = _row(cid, 'champion_departure_sponsor_rebuild')
            assert champ.state == 'proposed'
            iid = champ.id
        r = httpx.post(f'{base}/api/interventions/{iid}/approve', json={'customer_id': cid, 'note': 'sponsor rebuild — go'}, headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out['state'] == 'sent' and out['delivery']['status'] == 'delivered' and out['delivery']['attempts'] == 1
        assert out['approved_by'] == 'server_key' and out['delivery']['url_host'] == '127.0.0.1'
        # the receiver's callback: started, then done by policy
        assert _wait(lambda: db.session.get(Intervention, iid).started_at is not None)
        assert _wait(lambda: db.session.get(Intervention, iid).closed_state == 'done')
        with app.app_context():
            row = db.session.get(Intervention, iid)
            assert row.state == 'closed' and row.closed_by == 'server_key' and row.notes[-1]['note'].startswith('closed by the reference receiver')
            audits = [a.tool for a in ToolAuditLog.query.filter_by(customer_id=cid).filter(ToolAuditLog.tool.like('intervention.%')).order_by(ToolAuditLog.id).all()]
            assert audits[-4:] == ['intervention.approve', 'intervention.send', 'intervention.started', 'intervention.done']
            assert ToolAuditLog.query.filter_by(customer_id=cid, tool='intervention.started').order_by(ToolAuditLog.id.desc()).first().key_kind == 'server'
        rapp.state.receiver.drain()
        events = _log(cfg)
        assert [e['event'] for e in events] == ['received', 'callback', 'callback'] and events[0]['intervention_id'] == iid
        assert all(e['status'] == 'ok' for e in events[1:])
        li = httpx.get(f'{base}/api/interventions', params={'customer_id': cid, 'state': 'closed'}, headers=auth).json()
        assert iid in [v['intervention_id'] for v in li['interventions']] and secret not in json.dumps(li)

        # a wrong secret on the platform side: the receiver answers 401, the platform records a FAILED delivery
        monkeypatch.setattr(webhook, '_sleep', lambda s: None)
        assert httpx.post(f'{base}/api/playbooks', json={'customer_id': cid, 'webhook_secret': 'not-' + secret}, headers=auth).status_code == 200
        with app.app_context():
            seat = _row(cid, 'seat_truedown_save')
            sid = seat.id
        r = httpx.post(f'{base}/api/interventions/{sid}/approve', json={'customer_id': cid}, headers=auth, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()['delivery']
        assert d['status'] == 'failed' and d['http_status'] == 401 and d['attempts'] == 2 and 'bad_signature' in d['error']
        assert r.json()['delivery_problem'] is True
        refused = [e for e in _log(cfg) if e['event'] == 'refused']
        assert len(refused) == 2 and {e['reason'] for e in refused} == {'bad_signature'}
        time.sleep(0.5)
        with app.app_context():
            assert db.session.get(Intervention, sid).started_at is None            # nothing was accepted, nothing called back
        # the adapter routes are on the same server and keyed
        assert httpx.get(f'{base}/api/sources', params={'customer_id': cid}).status_code == 401
        src = httpx.get(f'{base}/api/sources', params={'customer_id': cid}, headers=auth).json()['sources']
        assert src[0]['source'] == 'gainsight_timeline' and 'activity_id' in src[0]['columns']
        r = httpx.post(f'{base}/api/sources/gainsight_timeline/import', json={'customer_id': cid, 'content': SAMPLE.read_text(), 'dry_run': True}, headers=auth)
        assert r.status_code == 200 and r.json()['dry_run'] and r.json()['received'] == 6
        assert httpx.post(f'{base}/api/sources/no_such/import', json={'customer_id': cid, 'content': 'x'}, headers=auth).status_code == 400
    finally:
        rstop()


# ── Slack ─────────────────────────────────────────────────────────────

def test_slack_url_is_validated_stored_and_never_returned(tenant, monkeypatch):
    cid, ids = tenant
    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')          # in-process tool calls (the module's platform server has set http)
    with app.app_context():
        from playbooks.definitions import configure_tenant, tenant_config, tenant_slack_url
        from mcp_server.cs_pulse_onboarding import configure_playbooks, get_playbooks, list_interventions
        os.environ.pop('PLAYBOOK_WEBHOOK_ALLOW_HTTP', None)
        with pytest.raises(ValueError, match='hooks.slack.com'):
            configure_tenant(cid, slack_webhook_url='https://example.com/services/x')
        with pytest.raises(ValueError, match='hooks.slack.com'):
            configure_tenant(cid, slack_webhook_url='http://hooks.slack.com/services/x')
        real = 'https://hooks.slack.com/services/T000/B000/' + uuid.uuid4().hex
        out = configure_playbooks(cid, slack_webhook_url=real)
        assert out['tenant']['slack_webhook_url_set'] is True and real not in json.dumps(out)
        for surface in (get_playbooks(cid), list_interventions(cid), tenant_config(cid)):
            assert real not in json.dumps(surface) and 'slack_webhook_url' not in json.dumps(surface).replace('slack_webhook_url_set', '')
        assert tenant_slack_url(cid) == real
        a = ToolAuditLog.query.filter_by(customer_id=cid, tool='intervention.configure').order_by(ToolAuditLog.id.desc()).first()
        assert 'slack_webhook_url' in a.detail and 'slack=True' in a.detail and 'hooks.slack.com' not in a.detail
        assert configure_playbooks(cid, slack_webhook_url='')['tenant']['slack_webhook_url_set'] is False
        os.environ['PLAYBOOK_WEBHOOK_ALLOW_HTTP'] = 'true'


def test_slack_is_a_second_delivery_entry_for_notify_class_only(tenant, monkeypatch):
    cid, ids = tenant
    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    from playbooks import webhook
    monkeypatch.setattr(webhook, '_sleep', lambda s: None)
    slack, engine = _Capture(), _Capture()
    try:
        with app.app_context():
            from playbooks.definitions import configure_tenant
            from playbooks.governance import approve, row_view
            from adapters import settings
            configure_tenant(cid, webhook_url=f'{engine.url}/hook', webhook_secret='s-' + uuid.uuid4().hex, slack_webhook_url=f'{slack.url}/services/x')
            orc = _row(cid, 'expansion_intent_handoff')
            assert orc.action_class == 'notify' and orc.state == 'proposed'
            out = approve(cid, orc.id, note='hand to sales')
            d = out['delivery']
            assert d['status'] == 'delivered' and d['slack']['status'] == 'delivered' and d['slack']['http_status'] == 200
            assert out['delivery_problem'] is False and len(slack.requests) == 1 and len(engine.requests) == 1
            msg = json.loads(slack.requests[0]['body'])
            assert set(msg) == {'text'}
            text = msg['text']
            assert f'#{orc.id}' in text and 'Orchard Retail' in text and 'expansion_intent_handoff' in text and orc.trigger_quote[:40] in text
            for forbidden in ('score', 'health', 'roster', 'raw_text', '420'):
                assert forbidden not in text.lower()
            assert len([q for q in text.split('"') if q]) >= 1 and len(text) < 600
            # node + audit carry the workflow engine's delivery, and say slack was posted
            from models import ContextNode
            assert db.session.get(ContextNode, out['node_id']).properties['delivery_status'] == 'delivered'
            a = ToolAuditLog.query.filter_by(customer_id=cid, tool='intervention.send').order_by(ToolAuditLog.id.desc()).first()
            assert a.detail.endswith('slack=delivered')
            # an escalate-class approval: no slack entry, no post
            har = _row(cid, 'escalation_exec_response')
            assert har.action_class == 'escalate'
            out2 = approve(cid, har.id)
            assert 'slack' not in out2['delivery'] and len(slack.requests) == 1 and len(engine.requests) == 2
            # a failing Slack endpoint does not fail the webhook delivery
            slack.statuses = [500]
            from models import Intervention as I
            fresh = I(customer_id=cid, account_id=ids['ORC'], playbook_id='expansion_intent_handoff', playbook_version='1.0', action_class='notify',
                      approval_mode='auto', state='proposed', trigger_key='f' * 64, trigger_episode_ids=['sig:x'], trigger_node_ids=[], trigger_roles=['expansion_intent'],
                      trigger_quote='Asked about the analytics module pricing', expected_outcome_types=['expansion_closed'], expected_window_days=90)
            db.session.add(fresh); db.session.commit()
            out3 = approve(cid, fresh.id)
            assert out3['delivery']['status'] == 'delivered' and out3['delivery']['slack']['status'] == 'failed' and 'HTTP 500' in out3['delivery']['slack']['error']
            assert out3['delivery_problem'] is False and row_view(db.session.get(I, fresh.id))['delivery_problem'] is False
            assert settings.get('slack', 'action_classes') == ['notify']
            configure_tenant(cid, slack_webhook_url='', webhook_url='', webhook_secret='')
    finally:
        slack.stop(); engine.stop()


# ── Gainsight Timeline ───────────────────────────────────────────────

def test_gainsight_sample_imports_end_to_end_and_is_idempotent_by_source_ref(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)           # the keyword stub classifies; no model call
    monkeypatch.setenv('MCP_TRANSPORT', 'stdio')
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_adapters import import_from_source
    os.environ['PLAYBOOK_WEBHOOK_ALLOW_HTTP'] = 'true'
    with app.app_context():
        db.create_all()
        cid, ids = _make_tenant(uuid.uuid4().hex[:8], [
            ('Northstar Mutual', 'GS-NOR', {'primary_champion_name': 'Dana Whitfield', 'renewal_date': '2026-10-15'}, 420_000),
            ('Orchard Retail', 'GS-ORC', {'renewal_date': '2027-03-01'}, 180_000),
        ])
        content = SAMPLE.read_text()
        dry = import_from_source(cid, 'gainsight_timeline', content, dry_run=True)
        assert dry['dry_run'] and dry['received'] == 6 and dry['parse']['rows'] == 9 and len(dry['items_preview']) == 5
        assert QualitativeSignal.query.filter_by(customer_id=cid).count() == 0
        out = import_from_source(cid, 'gainsight_timeline', content)
        assert out['source'] == 'gainsight_timeline' and out['received'] == 6 and out['queued'] == 5 and out['already_imported'] == 0 and out['duplicates'] == 0
        assert out['unknown_accounts'] == [{'index': 5, 'ref': 'GS-NOPE', 'row': 6}] and out['rejected'] == []
        assert [r['row'] for r in out['parse']['rejected']] == [7, 8, 9]
        assert 'duplicate Activity ID' in out['parse']['rejected'][2]['reason'] and 'unreadable activity date' in out['parse']['rejected'][1]['reason']
        cols = out['parse']['columns']
        assert cols['mapped']['activity_id'] == 'Activity ID' and cols['mapped']['company_name'] == 'Company Name' and cols['missing'] == []
        assert cols['unmapped'] == ['Milestone Type', 'Scorecard Health']
        assert out['processed']['processed'] == 5 and out['processed']['journeys_rebuilt'] == 2 and out['processed']['nodes_written'] >= 4
        sigs = QualitativeSignal.query.filter_by(customer_id=cid).order_by(QualitativeSignal.occurred_at).all()
        assert len(sigs) == 5 and all(s.source_ref.startswith('gainsight:timeline:') for s in sigs)
        assert {s.source_type for s in sigs} == {'email', 'meeting', 'crm_activity'}
        assert out['by_ref']['gainsight:timeline:1P05XXXXXXA1'] in {s.signal_id for s in sigs}
        budget = next(s for s in sigs if s.source_ref.endswith('A1'))
        assert budget.raw_text.startswith('Renewal budget pressure\n\nProcurement wants a 20% reduction') and '<' not in budget.raw_text
        assert budget.attributes['gainsight'] == {'activity_type': 'Email', 'activity_id': '1P05XXXXXXA1'} and budget.attributes['Scorecard Health'] == 'Red'
        assert [p['name'] for p in budget.stakeholder_roles] == ['Priya Raman', 'Dana Whitfield'] and budget.occurred_at.isoformat() == '2026-08-12T10:15:00'
        milestone = next(s for s in sigs if s.source_ref.endswith('B2'))
        assert milestone.account_id == ids['GS-ORC']                       # resolved by name: the row had no Company ID
        assert milestone.attributes['Milestone Type'] == 'Go Live'
        assert JourneyData.query.filter_by(customer_id=cid).count() == 2
        from models import ContextNode
        assert ContextNode.query.filter_by(customer_id=cid, node_type='SIGNAL').count() >= 4
        assert ToolAuditLog.query.filter_by(customer_id=cid, tool='import_from_source.gainsight_timeline').count() == 1
        # the same file again: nothing written, everything reported
        again = import_from_source(cid, 'gainsight_timeline', content)
        assert again['already_imported'] == 5 and again['queued'] == 0 and again['signal_ids'] == [] and 'processed' not in again
        assert again['unknown_accounts'] == out['unknown_accounts'] and len(again['by_ref']) == 5
        assert QualitativeSignal.query.filter_by(customer_id=cid).count() == 5
        with pytest.raises(ToolError, match='unknown source'):
            import_from_source(cid, 'churnzero', content)
        with pytest.raises(ToolError, match='not found'):
            import_from_source(cid + 100000, 'gainsight_timeline', content)
        headerless = import_from_source(cid, 'gainsight_timeline', 'Foo,Bar\n1,2\n', dry_run=True)
        assert headerless['received'] == 0 and 'missing required columns' in headerless['parse']['rejected'][0]['reason']
        db.session.remove()


def test_gainsight_parser_units():
    from adapters.sources.gainsight_timeline import parse_date, clean_text, parse
    assert parse_date('2026-08-12T10:15:00Z') == '2026-08-12T10:15:00Z'
    assert parse_date('2026-08-12T12:15:00+02:00') == '2026-08-12T10:15:00Z'
    assert parse_date('08/20/2026 02:30 PM') == '2026-08-20T14:30:00Z' and parse_date('2026-08-18') == '2026-08-18T00:00:00Z'
    assert parse_date('next tuesday') is None and parse_date('') is None
    assert clean_text('<p>Hello &amp; <b>bye</b></p><br>x') == 'Hello & bye\n\nx' and clean_text('a<br>b') == 'a\nb'
    r = parse('activity id,activity type,SUBJECT,Notes,Activity Date,company name\nA1,Email,Hi,Note,2026-01-02,Acme\n')
    assert r['rows'] == 1 and r['items'][0]['source_ref'] == 'gainsight:timeline:A1' and r['columns']['mapped']['subject'] == 'SUBJECT'


# ── registration ──────────────────────────────────────────────────────

def test_tools_and_routes_are_registered_and_keyed():
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    import mcp_server.cs_pulse_adapters as m
    from adapters.http import ROUTES
    assert 'import_from_source' in KEYED_TOOLS and 'import_from_source' in WRITE_TOOLS and 'import_from_source' not in ONBOARDING_TOOLS
    assert hasattr(m, 'import_from_source')
    assert ROUTES == ('/api/sources', '/api/sources/{source}/import')
    src = (BACKEND / 'server.py').read_text()
    assert 'mcp_server.cs_pulse_adapters' in src and 'register_adapter_routes(mcp)' in src


def test_no_bare_numbers_outside_the_config():
    """Every tunable the adapters use is in config/adapters.json; the contract (headers, scheme) comes from the governance config."""
    from adapters import settings
    for path in (('receiver', 'timestamp_tolerance_seconds'), ('receiver', 'callback', 'retries'), ('receiver', 'policy', 'auto_done_after_seconds'),
                 ('slack', 'quote_chars'), ('sources', 'max_rows'), ('sources', 'gainsight_timeline', 'text_chars')):
        assert settings.get(*path) is not None
    with pytest.raises(KeyError, match='has no receiver/nope'):
        settings.get('receiver', 'nope')
    c = settings.contract()
    assert c['signature_header'] == 'X-CI-Signature' and c['timestamp_header'] == 'X-CI-Timestamp' and 'started' in c['report_states']
