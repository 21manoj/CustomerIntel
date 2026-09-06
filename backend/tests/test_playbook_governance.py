"""
Playbook governance layer (docs/design/playbook-governance-layer.md), end to end on a real Postgres tenant:
  * definitions validate against the taxonomy; bad ones are refused at load
  * evaluate proposes from the journey's latest leading month, cites episode ids, is idempotent, suppresses
  * dry_run writes nothing; the hook after a journey rebuild proposes by itself
  * approve sends one signed payload (verified by a local receiver), writes the INTERVENTION node + LED_TO edges,
    the journey reads it as an 'intervention' episode with a counterfactual hook and a cited narrative sentence
  * a failed delivery is retried once and stays visible; no endpoint = not_configured, still visible
  * report: started, done with an outcome (log_outcome lane, linked, in-window + expected flags), cancelled = decline
  * automation_level 1 auto-approves notify playbooks by policy; kill switch stops everything
  * every transition is a tool_audit_log row; list gives stuck rows and the per-playbook numbers (realized vs exposure)
  * HTTP routes are pinned and keyed; delete_customer removes the rows
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta
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
from models import Account, ContextNode, ContextEdge, Intervention, JourneyData, ToolAuditLog   # noqa: E402


# ── a receiving endpoint (the "workflow engine") ──────────────────────

class _Receiver:
    def __init__(self):
        self.requests = []
        self.status = 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n).decode('utf-8')
                outer.requests.append({'path': self.path, 'headers': {k: v for k, v in self.headers.items()}, 'body': body})
                self.send_response(outer.status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *a):  # quiet
                pass

        self.server = HTTPServer(('127.0.0.1', 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/hook'

    def stop(self):
        self.server.shutdown()


@pytest.fixture(scope='module')
def receiver():
    r = _Receiver()
    yield r
    r.stop()


def _sig(cid, aid, subtype, when, text):
    from mcp_server.cs_pulse_onboarding import submit_signal
    return submit_signal(cid, aid, text, source_type='crm_activity', signal_type=subtype, occurred_at=when, process_now=False)


def _drain_and_build(cid, evaluate=False):
    from signal_engine.pipeline import process_pending
    from journeys.wizard_a import run_wizard_a
    process_pending(customer_id=cid, limit=100, rebuild_journeys=False)
    return run_wizard_a(cid, evaluate_playbooks=evaluate)


@pytest.fixture(scope='module')
def tenant():
    os.environ['PLAYBOOK_WEBHOOK_ALLOW_HTTP'] = 'true'        # the local receiver is plain http
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Playbooks {tag}', domain=f'pb-{tag}.test', vertical='saas_premium',
                              admin_email=f'pb_{tag}@t.test', admin_name='P', data_origin='synthetic_test')['customer_id']
        mk = lambda name, ext, pm, rev: Account(customer_id=cid, account_name=name, revenue=rev, vertical='saas_premium',
                                                external_account_id=ext, profile_metadata=pm)
        north = mk('Northstar Mutual', 'NOR', {'primary_champion_name': 'Dana Whitfield', 'renewal_date': '2026-10-15'}, 420_000)
        orchard = mk('Orchard Retail', 'ORC', {'renewal_date': '2027-03-01'}, 180_000)
        quiet = mk('Quiet Co', 'QUI', {'renewal_date': '2027-01-01'}, 90_000)
        db.session.add_all([north, orchard, quiet]); db.session.commit()
        ids = {'NOR': north.account_id, 'ORC': orchard.account_id, 'QUI': quiet.account_id}
        # Northstar: champion departure (critical), budget pressure (high) + seat under-use (medium), renewal in ~7 weeks
        _sig(cid, ids['NOR'], 'champion_departure', '2026-08-04T10:00:00Z', 'Dana Whitfield is leaving at the end of the month')
        _sig(cid, ids['NOR'], 'budget_pressure', '2026-08-12T10:00:00Z', 'Procurement wants a 20% reduction at renewal')
        _sig(cid, ids['NOR'], 'seat_underutilization', '2026-08-20T10:00:00Z', '140 of 300 seats have not logged in this quarter')
        # Orchard: expansion interest only (notify / auto)
        _sig(cid, ids['ORC'], 'expansion_interest', '2026-08-18T10:00:00Z', 'Ops team asked for 40 more seats for the new region')
        _drain_and_build(cid, evaluate=False)
        yield cid, ids
        db.session.remove()
        db.drop_all()
    os.environ.pop('PLAYBOOK_WEBHOOK_ALLOW_HTTP', None)


def _rows(cid, **kw):
    return Intervention.query.filter_by(customer_id=cid, **kw).order_by(Intervention.id).all()


def _audit(cid, transition):
    return ToolAuditLog.query.filter_by(customer_id=cid, tool=f'intervention.{transition}').order_by(ToolAuditLog.id).all()


# ── definitions ───────────────────────────────────────────────────────

def test_every_vertical_config_validates_against_its_taxonomy():
    from playbooks.definitions import validate_all, load_vertical, governance
    verticals = validate_all()
    assert {'saas_premium', 'dc2_s', 'datacenter_v1', 'healthcare_provider'} <= set(verticals)
    for v in verticals:
        d = load_vertical(v)
        assert d['playbooks'] and d['version'] == '1.0'
        for p in d['playbooks']:
            assert p['approval'] == 'human' or p['action_class'] in governance()['auto_approval_allowed_for']
    assert load_vertical('no_such_vertical')['playbooks'] == [] and 'no playbook definitions' in load_vertical('no_such_vertical')['note']


def test_bad_definitions_are_refused_at_load():
    from playbooks.definitions import _validate_playbook, PlaybookConfigError, governance
    from utils.taxonomy_loader import get_taxonomy
    tax, gov = get_taxonomy('saas_premium'), governance()
    good = {'id': 'x', 'trigger': {'roles': ['escalation'], 'urgency_floor': 'high'}, 'action_class': 'notify', 'approval': 'auto',
            'expected_outcome': {'types': ['escalation_resolved'], 'window_days': 30}}
    assert _validate_playbook(good, 'saas_premium', tax, gov, set())['trigger']['roles_match'] == 'any'
    for patch, msg in [
        ({'trigger': {'roles': ['not_a_role']}}, 'not in the saas_premium taxonomy'),
        ({'approval': 'auto', 'action_class': 'escalate'}, 'approval=auto is allowed only'),
        ({'expected_outcome': {'types': ['made_up'], 'window_days': 30}}, 'not in the saas_premium revenue buckets'),
        ({'trigger': {'roles': ['escalation'], 'urgency_floor': 'urgent'}}, 'urgency_floor'),
        ({'action_class': 'email'}, 'action_class'),
        ({'trigger': {'roles': ['escalation'], 'roles_match': 'some'}}, 'roles_match'),
    ]:
        with pytest.raises(PlaybookConfigError, match=msg):
            _validate_playbook({**good, **patch}, 'saas_premium', tax, gov, set())
    with pytest.raises(PlaybookConfigError, match='duplicate'):
        seen = set()
        _validate_playbook(good, 'saas_premium', tax, gov, seen)
        _validate_playbook(good, 'saas_premium', tax, gov, seen)


# ── evaluate ──────────────────────────────────────────────────────────

def test_dry_run_proposes_with_citations_and_writes_nothing(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.governance import evaluate
        out = evaluate(cid, dry_run=True)
        assert out['status'] == 'evaluated' and out['accounts_evaluated'] == 3
        by = {(p['account_id'], p['playbook_id']): p for p in out['proposed']}
        champ = by[(ids['NOR'], 'champion_departure_sponsor_rebuild')]
        assert champ['urgency'] == 'critical' and champ['approval'] == 'human' and champ['action_class'] == 'escalate'
        assert champ['trigger_episode_ids'] and all(e.startswith('sig:') for e in champ['trigger_episode_ids'])
        assert 'Dana Whitfield' in champ['trigger_quote'] and champ['exposure_revenue'] == 420000.0
        seat = by[(ids['NOR'], 'seat_truedown_save')]           # both roles present, floor high (budget_pressure), renewal inside 120 d
        assert set(seat['trigger_roles']) == {'commercial_pressure', 'usage_decline'} and seat['urgency'] == 'high'
        assert (ids['ORC'], 'expansion_intent_handoff') in by and (ids['NOR'], 'expansion_intent_handoff') not in by
        assert not any(p['account_id'] == ids['QUI'] for p in out['proposed'])
        assert 'intervention_id' not in champ and _rows(cid) == []


def test_evaluate_writes_proposals_once_and_audits(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.governance import evaluate
        first = evaluate(cid)
        assert len(first['proposed']) == 3 and first['auto_approved'] == []      # level 0: the notify playbook waits for a person too
        rows = _rows(cid)
        assert {r.state for r in rows} == {'proposed'} and all(r.proposed_by == 'local' for r in rows)
        champ = next(r for r in rows if r.playbook_id == 'champion_departure_sponsor_rebuild')
        assert champ.trigger_node_ids and champ.trigger_key and champ.expected_window_days == 120
        assert champ.evaluated_as_of is not None
        second = evaluate(cid)
        assert second['proposed'] == [] and {s['reason'] for s in second['skipped'] if s.get('intervention_id')} == {'exists'}
        assert len(_rows(cid)) == 3
        a = _audit(cid, 'propose')
        assert len(a) == 3 and a[0].key_kind == 'local' and a[0].surface == 'playbook' and 'cites sig:' in a[0].detail


def test_hook_after_journey_rebuild_proposes_and_level_1_auto_approves_notify(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.definitions import configure_tenant
        from mcp_server.cs_pulse_onboarding import submit_signal
        configure_tenant(cid, automation_level=1)
        a = Account(customer_id=cid, account_name='Fresh Fields', revenue=60_000, vertical='saas_premium', external_account_id='FRE',
                    profile_metadata={'renewal_date': '2027-02-01'})
        db.session.add(a); db.session.commit()
        res = submit_signal(cid, a.account_id, 'They want to roll the product out to two more teams', source_type='meeting',
                            signal_type='new_team_rollout', occurred_at='2026-08-25T09:00:00Z', process_now=True)
        assert res['processed'] and res['journeys_rebuilt'] == 1
        row = _rows(cid, account_id=a.account_id)[0]
        assert row.playbook_id == 'expansion_intent_handoff' and row.state == 'sent'
        assert row.approved_by == 'policy:automation_level_1' and row.proposed_by == 'system:journey_rebuild'
        assert row.delivery['status'] == 'not_configured' and row.node_id
        assert [x.key_kind for x in _audit(cid, 'approve')][-1] == 'system'
        configure_tenant(cid, automation_level=0)


# ── approve + send ────────────────────────────────────────────────────

def test_approve_sends_one_signed_minimal_payload_and_writes_the_node(tenant, receiver):
    cid, ids = tenant
    with app.app_context():
        from playbooks.definitions import configure_tenant, tenant_config
        from playbooks.governance import approve
        from playbooks import webhook
        secret = 's3cr3t-' + uuid.uuid4().hex
        cfg = configure_tenant(cid, webhook_url=receiver.url, webhook_secret=secret)
        assert cfg['webhook_secret_set'] and secret not in json.dumps(tenant_config(cid))
        champ = next(r for r in _rows(cid) if r.playbook_id == 'champion_departure_sponsor_rebuild')
        receiver.requests.clear()
        out = approve(cid, champ.id, note='Sponsor rebuild with the VP Ops — approved')
        assert out['state'] == 'sent' and out['delivery']['status'] == 'delivered' and out['delivery']['attempts'] == 1
        assert out['approved_by'] == 'local' and out['journeys_rebuilt'] == 1
        # one request, signed over '<timestamp>.<body>', minimal content
        assert len(receiver.requests) == 1
        req = receiver.requests[0]
        assert webhook.verify(secret, req['headers']['X-CI-Timestamp'], req['body'], req['headers']['X-CI-Signature'])
        assert not webhook.verify('wrong', req['headers']['X-CI-Timestamp'], req['body'], req['headers']['X-CI-Signature'])
        p = json.loads(req['body'])
        assert p['intervention_id'] == champ.id and p['playbook']['action_class'] == 'escalate'
        assert p['account']['name'] == 'Northstar Mutual' and p['trigger'][0]['episode_id'].startswith('sig:')
        assert p['callback']['route'] == f'/api/interventions/{champ.id}/report' and p['data_origin']['synthetic'] is True
        for forbidden in ('raw_text', 'roster', 'health_score', 'kpi_only', 'content'):
            assert forbidden not in p
        # the node and its edges
        node = db.session.get(ContextNode, out['node_id'])
        assert node.node_type == 'INTERVENTION' and node.source == 'observed' and node.source_platform == 'playbook'
        assert node.source_event_id == f'intervention:{champ.id}' and node.properties['delivery_status'] == 'delivered'
        edges = ContextEdge.query.filter_by(to_node_id=node.node_id, edge_type='LED_TO').all()
        assert {e.from_node_id for e in edges} == set(champ.trigger_node_ids) and all(e.created_by == 'approve_intervention' for e in edges)
        # the journey reads it
        j = JourneyData.query.filter_by(customer_id=cid, account_id=ids['NOR']).first().journey_json
        ep = next(e for e in j['episodes'] if e['kind'] == 'intervention')
        assert ep['episode_id'] == f'int:{node.node_id}' and ep['role'] == 'intervention' and ep['meta']['delivery_status'] == 'delivered'
        assert any(h['episode_id'] == ep['episode_id'] for h in j['counterfactual_hooks'])
        cited = [s for ch in j['narrative']['chapters'] for s in ch['sentences'] if ep['episode_id'] in s['cites']]
        assert cited and 'approved by local' in cited[0]['text']
        from journeys.wizard_a import GENERATOR_VERSION
        assert j['generator_version'] == GENERATOR_VERSION
        assert _audit(cid, 'send')[-1].detail.startswith(f'#{champ.id} champion_departure_sponsor_rebuild → delivered')
        with pytest.raises(ValueError, match='only a proposed one'):
            approve(cid, champ.id)


def test_failed_delivery_is_retried_once_and_stays_visible(tenant, receiver, monkeypatch):
    cid, ids = tenant
    with app.app_context():
        from playbooks.governance import approve
        from playbooks import webhook
        slept = []
        monkeypatch.setattr(webhook, '_sleep', lambda s: slept.append(s))
        receiver.status = 500
        receiver.requests.clear()
        seat = next(r for r in _rows(cid) if r.playbook_id == 'seat_truedown_save')
        out = approve(cid, seat.id, note='true-down save call')
        receiver.status = 200
        assert out['state'] == 'sent' and out['delivery']['status'] == 'failed' and out['delivery']['attempts'] == 2
        assert out['delivery']['http_status'] == 500 and 'HTTP 500' in out['delivery']['error'] and out['delivery_problem']
        assert len(receiver.requests) == 2 and slept == [2.0]
        node = db.session.get(ContextNode, out['node_id'])
        assert node.properties['delivery_status'] == 'failed'
        j = JourneyData.query.filter_by(customer_id=cid, account_id=ids['NOR']).first().journey_json
        ep = next(e for e in j['episodes'] if e['episode_id'] == f'int:{node.node_id}')
        cited = [s for ch in j['narrative']['chapters'] for s in ch['sentences'] if ep['episode_id'] in s['cites']]
        assert cited and 'delivery to the workflow failed' in cited[0]['text']


# ── report ────────────────────────────────────────────────────────────

def test_report_started_then_done_with_an_outcome(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.governance import report, list_interventions
        champ = next(r for r in _rows(cid) if r.playbook_id == 'champion_departure_sponsor_rebuild')
        r1 = report(cid, champ.id, 'started', note='n8n: exec outreach sequence started')
        assert r1['state'] == 'sent' and r1['started_at'] and not r1['stuck']
        r2 = report(cid, champ.id, 'done', note='New sponsor (COO) signed the renewal', outcome_type='renewal_secured',
                    outcome_date=datetime.utcnow().date().isoformat(), revenue=420000)      # dated after the send: inside the window
        assert r2['state'] == 'closed' and r2['closed_state'] == 'done' and r2['closed_by'] == 'local'
        o = r2['outcome']
        assert o['status'] == 'logged' and o['bucket'] == 'protected' and o['revenue'] == 420000.0
        assert o['in_window'] is True and o['expected'] is True
        onode = db.session.get(ContextNode, o['node_id'])
        assert onode.node_type == 'OUTCOME' and onode.source_platform == 'playbook' and onode.properties['decided_by'] == f'intervention:{champ.id}'
        assert ContextEdge.query.filter_by(from_node_id=champ.node_id, to_node_id=onode.node_id, edge_type='LED_TO').count() == 1
        assert ContextEdge.query.filter_by(to_node_id=onode.node_id, edge_type='LED_TO').count() == 1 + len(champ.trigger_node_ids)
        assert db.session.get(ContextNode, champ.node_id).properties['closed_state'] == 'done'
        j = JourneyData.query.filter_by(customer_id=cid, account_id=ids['NOR']).first().journey_json
        assert any(e['kind'] == 'outcome' and e['evidence_node_ids'] == [onode.node_id] for e in j['episodes'])
        hook = next(h for h in j['counterfactual_hooks'] if h['episode_id'] == f'int:{champ.node_id}')
        assert any(x['episode_id'] == f'out:{onode.node_id}' for x in hook['outcomes_after'])
        with pytest.raises(ValueError, match='already closed'):
            report(cid, champ.id, 'failed')
        # the per-playbook numbers: realized and exposure are two numbers
        s = next(x for x in list_interventions(cid)['by_playbook'] if x['playbook_id'] == 'champion_departure_sponsor_rebuild')
        assert s['closed_done'] == 1 and s['outcomes_in_window'] == 1 and s['outcomes_expected'] == 1
        assert s['realized_revenue'] == 420000.0 and s['exposure_revenue'] == 420000.0 and 'never summed' in s['note']
        assert [x.tool for x in ToolAuditLog.query.filter_by(customer_id=cid).filter(ToolAuditLog.tool.like('intervention.%')).order_by(ToolAuditLog.id).all()][-2:] == ['intervention.started', 'intervention.done']


def test_cancel_declines_a_proposal_and_invalid_transitions_are_refused(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.governance import report, evaluate
        orc = next(r for r in _rows(cid) if r.account_id == ids['ORC'])
        assert orc.state == 'proposed'
        with pytest.raises(ValueError, match='only a sent one can be reported done'):
            report(cid, orc.id, 'done')
        with pytest.raises(ValueError, match='only a sent one can start'):
            report(cid, orc.id, 'started')
        with pytest.raises(ValueError, match='state must be one of'):
            report(cid, orc.id, 'finished')
        out = report(cid, orc.id, 'cancelled', note='sales already engaged')
        assert out['state'] == 'closed' and out['closed_state'] == 'cancelled' and out['node_id'] is None and out['sent_at'] is None
        assert _audit(cid, 'declined')[-1].detail.startswith(f'#{orc.id} expansion_intent_handoff cancelled')
        with pytest.raises(ValueError, match='not found for customer'):
            report(cid + 100000, orc.id, 'cancelled')
        # closed just now: the same (account, playbook) is suppressed inside the window even on new evidence
        _sig(cid, ids['ORC'], 'module_upsell_interest', '2026-08-29T10:00:00Z', 'Asked about the analytics module pricing')
        _drain_and_build(cid, evaluate=False)
        ev = evaluate(cid, ids['ORC'])
        assert ev['proposed'] == [] and {s['reason'] for s in ev['skipped'] if s['playbook_id'] == 'expansion_intent_handoff'} == {'suppressed_recent_close'}


# ── governance ────────────────────────────────────────────────────────

def test_stuck_and_delivery_problems_are_flagged(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.governance import list_interventions
        from playbooks.definitions import governance
        seat = next(r for r in _rows(cid) if r.playbook_id == 'seat_truedown_save')
        seat.sent_at = datetime.utcnow() - timedelta(days=governance()['stuck_after_days'] + 1)
        db.session.commit()
        out = list_interventions(cid, state='sent')
        v = next(x for x in out['interventions'] if x['intervention_id'] == seat.id)
        assert v['stuck'] and v['stuck_days'] >= governance()['stuck_after_days'] and v['delivery_problem'] and seat.id in out['stuck']
        assert out['tenant']['webhook_secret_set'] and out['count'] >= 1
        s = next(x for x in out['by_playbook'] if x['playbook_id'] == 'seat_truedown_save')
        assert s['stuck'] == 1 and s['delivery_problems'] == 1


def test_kill_switch_stops_evaluation_and_approval(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.definitions import configure_tenant
        from playbooks.governance import evaluate, approve
        from models import Intervention
        configure_tenant(cid, kill_switch=True)
        assert evaluate(cid)['status'] == 'disabled'
        row = Intervention(customer_id=cid, account_id=ids['QUI'], playbook_id='escalation_exec_response', playbook_version='1.0',
                           action_class='escalate', approval_mode='human', state='proposed', trigger_key='k' * 64, trigger_episode_ids=['sig:0'],
                           trigger_node_ids=[], trigger_roles=['escalation'], expected_outcome_types=['escalation_resolved'], expected_window_days=60)
        db.session.add(row); db.session.commit()
        with pytest.raises(ValueError, match='kill switch'):
            approve(cid, row.id)
        configure_tenant(cid, kill_switch=False)
        assert evaluate(cid, ids['QUI'])['status'] == 'evaluated'
        db.session.delete(row); db.session.commit()


def test_tenant_overlay_validation(tenant):
    cid, ids = tenant
    with app.app_context():
        from playbooks.definitions import configure_tenant, playbooks_for_customer
        with pytest.raises(ValueError, match='unknown playbook ids'):
            configure_tenant(cid, disabled_playbooks=['no_such_playbook'])
        with pytest.raises(ValueError, match='automation_level'):
            configure_tenant(cid, automation_level=7)
        os.environ.pop('PLAYBOOK_WEBHOOK_ALLOW_HTTP', None)
        with pytest.raises(ValueError, match='https'):
            configure_tenant(cid, webhook_url='http://example.com/hook', webhook_secret='x')
        os.environ['PLAYBOOK_WEBHOOK_ALLOW_HTTP'] = 'true'
        with pytest.raises(ValueError, match='webhook_secret is required'):
            configure_tenant(cid, webhook_url='https://example.com/hook', webhook_secret='')
        cfg = configure_tenant(cid, disabled_playbooks=['escalation_exec_response'])
        assert cfg['disabled_playbooks'] == ['escalation_exec_response']
        d = playbooks_for_customer(cid)
        assert 'escalation_exec_response' not in {p['id'] for p in d['playbooks']} and d['disabled'] == ['escalation_exec_response']
        configure_tenant(cid, disabled_playbooks=[])


def test_mcp_tools_are_keyed_and_registered():
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    for t in ('evaluate_playbooks', 'approve_intervention', 'report_intervention', 'list_interventions', 'configure_playbooks', 'get_playbooks'):
        assert t in KEYED_TOOLS
    assert {'evaluate_playbooks', 'approve_intervention', 'report_intervention', 'configure_playbooks'} <= WRITE_TOOLS
    assert 'list_interventions' not in WRITE_TOOLS and 'get_playbooks' not in WRITE_TOOLS
    import mcp_server.cs_pulse_onboarding as m
    for t in ('evaluate_playbooks', 'approve_intervention', 'report_intervention', 'list_interventions', 'configure_playbooks', 'get_playbooks'):
        assert hasattr(m, t)


def test_mcp_tool_round_trip(tenant):
    cid, ids = tenant
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import get_playbooks, list_interventions, evaluate_playbooks
        d = get_playbooks(cid)
        assert d['vertical'] == 'saas_premium' and {p['id'] for p in d['playbooks']} >= {'seat_truedown_save', 'champion_departure_sponsor_rebuild'}
        assert '"webhook_secret"' not in json.dumps(d) and 's3cr3t-' not in json.dumps(d)
        out = evaluate_playbooks(cid, dry_run=True)
        assert out['dry_run'] is True
        li = list_interventions(cid)
        assert li['count'] >= 3 and set(li['interventions'][0]) >= {'trigger', 'delivery', 'stuck', 'notes'}


def test_delete_customer_removes_interventions(tenant):
    cid, ids = tenant
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import delete_customer
        from models import Customer
        c = db.session.get(Customer, cid)
        n = Intervention.query.filter_by(customer_id=cid).count()
        assert n >= 3
        out = delete_customer(cid, c.domain, 'test teardown')
        assert out['deleted_rows']['interventions'] == n and Intervention.query.filter_by(customer_id=cid).count() == 0
