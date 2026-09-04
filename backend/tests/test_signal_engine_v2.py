"""
Signal engine v2 — the evidence pipeline, end to end against Postgres.

Covers the three unblockers and the normalization layer: a signal
submitted through MCP becomes an OBSERVED SIGNAL node with a taxonomy
subtype/role (no more 'qualitative_signal'), dated by the event, with the
person resolved against the roster or flagged; the structured path skips
the LLM; duplicates are caught by content hash; polarity conflicts follow
the role and are flagged; the journey picks the episode up immediately;
fusion no longer writes health columns; the HTTP routes are mounted and
key-authenticated; webhooks verify signatures and the customer toggle.
"""
import json
import os
import sys
import uuid
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop('ANTHROPIC_API_KEY', None)          # stub enrichment: deterministic keyword intents
os.environ['FEATURE_SIGNAL_ENGINE'] = 'true'

from flask import Flask
from extensions import db

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common
_common._flask_app = app

import utils.health_thresholds as ht
from models import Account, HealthScore, ContextNode, QualitativeSignal, JourneyData, FeatureToggle


@pytest.fixture(scope='module')
def tenant():
    _assert_isolated_test_db(TEST_DB)
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'SigV2 {tag}', domain=f'sigv2-{tag}.test', vertical='saas_premium',
                              admin_email=f'sv2_{tag}@t.test', admin_name='S')['customer_id']
        a = Account(customer_id=cid, account_name='Northwind Analytics', revenue=1_800_000, vertical='saas_premium',
                    external_account_id='northwind.com',
                    profile_metadata={'primary_champion_name': 'Elena Rossi', 'primary_champion_title': 'VP Data',
                                      'primary_champion_email': 'elena@northwind.com', 'csm_name': 'Maya Johnson',
                                      'executive_sponsor': 'Tom Becker', 'renewal_date': '2026-08-01'})
        db.session.add(a)
        db.session.flush()
        for i, (m, s) in enumerate([(date(2026, 1, 1), 80), (date(2026, 2, 1), 78), (date(2026, 3, 1), 74)]):
            db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s, kpi_only_score=s,
                                       health_status=ht.classify(s)))
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='STAKEHOLDER', node_subtype='champion',
                                   source='observed', title='Elena Rossi (VP Data)', properties={'name': 'Elena Rossi', 'title': 'VP Data'},
                                   tier=1, occurred_at=datetime(2025, 8, 1)))
        db.session.commit()
        yield cid, a.account_id
        db.session.remove()
        db.drop_all()


class TestSubmitSignalStructuredPath:
    def test_declared_subtype_writes_role_node_without_llm(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import submit_signal
        res = submit_signal(cid, aid, 'Champion (VP Data) left the company — CRM contact updated',
                            source_type='crm_activity', signal_type='champion_departure', occurred_at='2026-02-10T09:30:00Z',
                            participants=[{'name': 'Elena Rossi', 'role': 'VP Data'}], source_ref='crm:evt:1')
        assert res['status'] == 'queued' and res['processed'] is True and res['structured'] is True
        ev = res['evidence']
        assert ev['subtype'] == 'champion_departure' and ev['role'] == 'champion_change' and ev['basis'] == 'declared_subtype'
        assert ev['person'] == 'Elena Rossi' and ev['person_unresolved'] is False
        with app.app_context():
            n = db.session.get(ContextNode, ev['node_id'])
            assert n.source == 'observed' and n.node_type == 'SIGNAL' and n.node_subtype == 'champion_departure'
            assert n.occurred_at.isoformat().startswith('2026-02-10T09:30')     # event time, not call time
            assert n.source_platform == 'crm_activity' and n.source_event_id == res['signal_id']
            assert n.properties['stakeholder_role'] == 'champion' and n.properties['evidence_tier'] == 'observed'
            assert n.properties['llm_model_version'] == 'structured_rule_map'
            assert float(n.properties['sentiment_score']) < 0                 # role default: negative
            assert n.properties['structural_urgency'] == 'critical' == n.properties['effective_urgency']   # champion_change floor, structured path too
            sig = QualitativeSignal.query.filter_by(signal_id=res['signal_id']).first()
            assert sig.cg_node_id == n.node_id and sig.content_hash and sig.occurred_at
            assert sig.effective_urgency == 'critical' and sig.source_ref == 'crm:evt:1'   # own column now, not keywords

    def test_journey_picked_it_up(self, tenant):
        cid, aid = tenant
        with app.app_context():
            j = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json
            eps = [e for e in j['episodes'] if e['kind'] == 'signal']
            assert any(e['role'] == 'champion_change' and e['meta']['stakeholder'] == 'Elena Rossi' for e in eps)
            feb = next(s for s in j['leading_vs_trailing']['series'] if s['month'] == '2026-02-01')
            assert feb['qual'] is not None and feb['roles'].get('champion_change') == 1
            assert j['arc']['arc_type'] == 'exec_sponsor_change'


class TestFreeTextPath:
    def test_llm_intent_maps_to_role_and_unresolved_person_is_flagged(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import submit_signal
        res = submit_signal(cid, aid, 'Ravi mentioned they are evaluating alternatives to switch platforms next quarter',
                            source_type='email', occurred_at='2026-03-05T15:00:00Z',
                            participants=[{'name': 'Ravi Menon', 'role': 'Interim Data Lead'}])
        ev = res['evidence']
        assert ev['basis'] == 'llm_intent' and ev['subtype'] == 'competitor_mention' and ev['role'] == 'commercial_pressure'
        assert ev['person'] == 'Ravi Menon' and ev['person_unresolved'] is True
        with app.app_context():
            sig = QualitativeSignal.query.filter_by(signal_id=res['signal_id']).first()
            assert sig.llm_model_version == 'stub_keyword_v1' and sig.requires_review is True
            assert 'competitor_mention' in sig.intent_signals

    def test_no_intent_is_unclassified_not_dropped(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import submit_signal
        res = submit_signal(cid, aid, 'Sent over the meeting notes as promised.', source_type='manual',
                            occurred_at='2026-03-06T10:00:00Z')
        assert res['evidence']['subtype'] == 'unclassified_signal' and res['evidence']['role'] is None

    def test_polarity_conflict_follows_the_role(self, tenant):
        cid, aid = tenant
        with app.app_context():
            from signal_engine.pipeline import ingest, process_pending
            from utils.taxonomy_loader import get_taxonomy
            r = ingest(cid, aid, 'ticket', 'Usage quietly trending down with no explicit escalation',
                       occurred_at='2026-03-07T10:00:00Z', signal_type='usage_decline')
            sig = QualitativeSignal.query.filter_by(signal_id=r['signal_id']).first()
            sig.sentiment_score = 0.69          # the source says positive; the role says negative
            db.session.commit()
            out = process_pending(customer_id=cid)
            me = next(x for x in out['signals'] if x['signal_id'] == r['signal_id'])
            assert me['polarity_conflict'] is True
            n = db.session.get(ContextNode, me['node_id'])
            assert float(n.properties['sentiment_score']) < 0 and n.properties['raw_sentiment_score'] == 0.69


class TestDedupAndSeparation:
    def test_exact_duplicate_within_window_is_reported_not_stored(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import submit_signal
        a = submit_signal(cid, aid, 'Weekly sync skipped twice; no response on the capacity thread', source_type='slack',
                          occurred_at='2026-03-10T10:00:00Z', signal_type='engagement_gap')
        b = submit_signal(cid, aid, '  Weekly sync skipped twice;  no response on the capacity thread ', source_type='slack',
                          occurred_at='2026-03-12T10:00:00Z', signal_type='engagement_gap')
        assert a['status'] == 'queued' and b['status'] == 'duplicate' and b['duplicate_of'] == a['signal_id']
        with app.app_context():
            assert QualitativeSignal.query.filter_by(content_hash=a['content_hash']).count() == 1

    def test_fusion_is_gone_and_journey_owns_the_leading_columns(self, tenant):
        cid, aid = tenant
        import importlib
        assert importlib.util.find_spec('signal_engine.fusion') is None
        with app.app_context():
            rows = HealthScore.query.filter_by(account_id=aid).order_by(HealthScore.measurement_month).all()
            assert all(r.kpi_only_score == r.health_score for r in rows)          # trailing untouched
            assert all(r.composite_score is None for r in rows)                   # nothing blended
            assert any(r.qual_score is not None for r in rows)                    # leading written by the journey


class TestWorker:
    def test_process_once_runs_inside_app_context(self, tenant):
        cid, aid = tenant
        with app.app_context():
            from signal_engine.pipeline import ingest
            ingest(cid, aid, 'ticket', 'Storage utilization dropped from 78% to 55%. DR tests skipped.',
                   occurred_at='2026-03-15T10:00:00Z', signal_type='usage_decline')
        from signal_engine.worker import SignalEnrichmentWorker
        assert SignalEnrichmentWorker(startup_delay=0).process_once() == 1
        assert SignalEnrichmentWorker(startup_delay=0).process_once() == 0


class TestHttpSurface:
    @pytest.fixture(scope='class')
    def client(self, tenant):
        key = 'sigv2-server-key-' + uuid.uuid4().hex
        os.environ['MCP_SERVER_API_KEY'] = key
        import mcp_server.auth as auth
        auth.MCP_SERVER_API_KEY = key
        from server import build_asgi_app
        asgi = build_asgi_app(TEST_DB, create_schema=False)
        from starlette.testclient import TestClient
        with TestClient(asgi) as c:
            c.key = key
            yield c
        os.environ['MCP_TRANSPORT'] = 'stdio'

    def test_status_and_auth(self, client, tenant):
        cid, aid = tenant
        s = client.get('/api/signals/status').json()
        assert s['signal_engine_enabled'] and s['capabilities']['composite_fusion'] is False and s['capabilities']['channels']['ticket']
        r = client.post('/api/signals/ingest/manual', json={'customer_id': cid, 'account_id': aid, 'raw_text': 'x' * 20})
        assert r.status_code == 401                                                  # no key
        r = client.post('/api/signals/ingest/ticket', headers={'Authorization': f'Bearer {client.key}'},
                        json={'customer_id': cid, 'account_id': aid, 'raw_text': 'Ticket #4412: validating a second provider for training jobs',
                              'signal_type': 'competitor_mention', 'occurred_at': '2026-03-20T08:00:00Z'})
        assert r.status_code == 202 and r.json()['structured'] is True, r.text
        r = client.post('/api/signals/process', headers={'Authorization': f'Bearer {client.key}'}, json={'customer_id': cid})
        assert r.status_code == 200 and r.json()['processed'] == 1 and r.json()['signals'][0]['role'] == 'commercial_pressure'

    def test_slack_challenge_and_unknown_team(self, client):
        r = client.post('/api/signals/ingest/slack/events', json={'type': 'url_verification', 'challenge': 'abc'})
        assert r.status_code == 200 and r.json() == {'challenge': 'abc'}
        r = client.post('/api/signals/ingest/slack/events', json={'type': 'event_callback', 'team_id': 'TNOPE',
                                                                   'event': {'type': 'message', 'channel': 'C1', 'text': 'this is long enough text'}})
        assert r.status_code == 200 and r.json().get('ignored') == 'unknown team'

    def test_inbound_email_needs_customer_toggle_then_resolves_sender_domain(self, client, tenant):
        cid, aid = tenant
        form = {'from': 'Elena Rossi <elena@northwind.com>', 'to': f'signals-{cid}@ingest.example.com',
                'subject': 'Renewal', 'text': 'We are reviewing the contract and our budget was cut for next year.\n--\nElena'}
        r = client.post('/api/signals/ingest/email/parse', data=form)
        assert r.status_code == 403                                                  # toggle off
        from mcp_server.cs_pulse_onboarding import configure_signal_engine
        configure_signal_engine(cid, enabled=True)
        r = client.post('/api/signals/ingest/email/parse', data=form)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body['account_id'] == aid and body['sender'] == 'elena@northwind.com'
        with app.app_context():
            sig = QualitativeSignal.query.filter_by(signal_id=body['signal_id']).first()
            assert sig.source_type == 'email' and sig.raw_text.startswith('Subject: Renewal') and 'Elena\n' not in sig.raw_text


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
