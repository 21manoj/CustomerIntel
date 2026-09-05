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
            assert n.source_platform == 'crm_activity' and n.source_event_id == 'crm:evt:1' and n.properties['signal_id'] == res['signal_id']   # event id in its own system
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
        assert ev['basis'] == 'llm_extraction' and ev['subtype'] == 'competitor_mention' and ev['role'] == 'commercial_pressure'
        assert ev['person'] == 'Ravi Menon' and ev['person_unresolved'] is True
        with app.app_context():
            sig = QualitativeSignal.query.filter_by(signal_id=res['signal_id']).first()
            assert sig.llm_model_version == 'stub_keyword_v2' and sig.requires_review is True
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


class TestMultiSignalExtraction:
    def test_one_transcript_many_signals_many_nodes(self, tenant, monkeypatch):
        """v2: a communication that carries several signals writes one OBSERVED
        node per signal (each with its own role, urgency, person), all citing
        the same source event; the journey sees all of them."""
        cid, aid = tenant
        from utils.taxonomy_loader import get_taxonomy

        def fake_llm(signal_id, raw_text, account_id, customer_id, vertical, taxonomy=None, roster=None):
            from signal_engine.enrichment import normalize_extraction
            out = normalize_extraction({'signals': [
                {'subtype': 'integration_bug', 'quote': 'the Salesforce sync keeps dropping records', 'sentiment_score': -0.6,
                 'urgency_score': 0.7, 'escalation_probability': 0.4, 'confidence': 0.9,
                 'people': [{'name': 'Marcus Webb', 'title': 'Ops Lead'}]},          # new face, not on the roster
                {'subtype': 'module_upsell_interest', 'quote': 'adding the analytics module for EMEA', 'sentiment_score': 0.5,
                 'urgency_score': 0.3, 'escalation_probability': 0.0, 'confidence': 0.8,
                 'people': [{'name': 'Elena Rossi', 'title': 'VP Infrastructure', 'roster_role': 'champion'}]},
            ], 'requires_review': False, 'is_duplicate': False, 'suggested_action': 'Fix the sync; open the analytics-module conversation'},
                taxonomy or get_taxonomy(vertical))
            out['llm_model_version'] = 'fake-llm'
            return out
        import signal_engine.enrichment as enrichment
        monkeypatch.setattr(enrichment, 'enrich_signal', fake_llm)   # pipeline imports it at call time

        from mcp_server.cs_pulse_onboarding import submit_signal
        res = submit_signal(cid, aid, 'The Salesforce sync keeps dropping records. Separately Elena asked about adding the analytics module for EMEA.',
                            source_type='transcript', occurred_at='2026-03-12T10:00:00Z', consent_verified=True)
        assert res['status'] == 'queued' and res['processed'] is True
        with app.app_context():
            sig = QualitativeSignal.query.filter_by(signal_id=res['signal_id']).first()
            nodes = ContextNode.query.filter_by(source_event_id=sig.signal_id).order_by(ContextNode.node_id).all()
            assert [n.node_subtype for n in nodes] == ['integration_bug', 'module_upsell_interest']   # saas_premium overlay words
            assert [n.properties['role'] for n in nodes] == ['product_friction', 'expansion_intent']
            assert nodes[0].properties['effective_urgency'] == 'high' and nodes[1].properties['effective_urgency'] == 'high'
            assert nodes[0].title == 'the Salesforce sync keeps dropping records'                       # the quote is the evidence
            assert nodes[1].properties['stakeholder_role'] == 'champion' and nodes[1].properties['person_unresolved'] is False
            assert nodes[0].properties['stakeholder_name'] == 'Marcus Webb' and nodes[0].properties['person_unresolved'] is True   # each node: its own people
            assert sig.stakeholder_roles is None                                        # nothing declared by the source
            assert float(nodes[1].properties['sentiment_score']) > 0 and nodes[1].properties['polarity_conflict'] is False
            assert sig.cg_node_id == nodes[0].node_id and len(sig.extractions) == 2 and sig.effective_urgency == 'high'
            j = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json
            march = next(s for s in j['leading_vs_trailing']['series'] if s['month'] == '2026-03-01')
            assert march['roles'].get('product_friction') == 1 and march['roles'].get('expansion_intent') == 1


class TestExtractionFailureIsNotEvidence:
    def test_error_leaves_signal_queued_then_retry_succeeds(self, tenant, monkeypatch):
        cid, aid = tenant
        import signal_engine.enrichment as enrichment
        from signal_engine.pipeline import process_pending, ingest
        from utils.taxonomy_loader import get_taxonomy

        def broken(signal_id, raw_text, account_id, customer_id, vertical, taxonomy=None, roster=None):
            out = enrichment.normalize_extraction({'signals': [], 'requires_review': True}, taxonomy or get_taxonomy(vertical))
            out['error'] = "No module named 'anthropic'"
            return out
        monkeypatch.setattr(enrichment, 'enrich_signal', broken)
        with app.app_context():
            res = ingest(cid, aid, 'manual', 'Procurement wants to true-down the seats at renewal.', occurred_at='2026-04-02T09:00:00Z')
            sid = res['signal_id']
            out = process_pending(customer_id=cid)
            assert out['errors'] == 1 and out['processed'] == 0 and out['error_signals'][0]['signal_id'] == sid
            sig = QualitativeSignal.query.filter_by(signal_id=sid).first()
            assert sig.cg_node_id is None and sig.intent_signals is None          # still queued, no node
            assert ContextNode.query.filter_by(source_event_id=sid).count() == 0

        def fixed(signal_id, raw_text, account_id, customer_id, vertical, taxonomy=None, roster=None):
            out = enrichment.normalize_extraction({'signals': [{'subtype': 'seat_reduction_request', 'quote': 'true-down the seats',
                                                                 'sentiment_score': -0.5, 'urgency_score': 0.6, 'escalation_probability': 0.2,
                                                                 'confidence': 0.9}], 'requires_review': False, 'is_duplicate': False,
                                                    'suggested_action': 'x'}, taxonomy or get_taxonomy(vertical))
            out['llm_model_version'] = 'fake-llm'
            return out
        monkeypatch.setattr(enrichment, 'enrich_signal', fixed)
        with app.app_context():
            out = process_pending(customer_id=cid)
            assert out['errors'] == 0 and out['processed'] == 1
            n = ContextNode.query.filter_by(source_event_id=sid).one()
            assert n.node_subtype == 'seat_reduction_request' and n.properties['role'] == 'commercial_pressure'


class TestLiveMonthsOnTheJourney:
    def test_signal_after_last_scored_month_is_visible(self, tenant):
        """Signals run ahead of the monthly KPI feed. Evidence dated after the
        last scored month must appear on the journey now — as live months with
        qual and roles but no trailing — not after the next KPI upload."""
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import submit_signal
        with app.app_context():
            before = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json
            last_scored = before['last_scored_month']
        res = submit_signal(cid, aid, 'CFO office asked for a full vendor review before renewal', source_type='crm_activity',
                            signal_type='budget_review', occurred_at='2026-07-09T10:00:00Z')
        assert res['processed'] is True
        with app.app_context():
            j = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json
            assert j['last_scored_month'] == last_scored and '2026-07-01' in j['live_months']
            jul = next(s for s in j['leading_vs_trailing']['series'] if s['month'] == '2026-07-01')
            assert jul['live'] is True and jul['kpi_only'] is None and jul['divergence'] is None
            assert jul['early_warning'] == 'leading_only' and jul['qual'] is not None
            assert jul['roles'].get('commercial_pressure') == 1
            assert j['as_of'] >= '2026-07-09' and j['last_evidence_at'].startswith('2026-07-09')
            scored = [s for s in j['leading_vs_trailing']['series'] if not s['live']]
            assert scored[-1]['month'] == last_scored                    # scored axis untouched


class TestReadSurfaceAndReview:
    """G1 (evidence needs a surface) + G4 (human verification with a record)."""

    def _flagged_signal(self, cid, aid, monkeypatch, text, subtype, confidence):
        import signal_engine.enrichment as enrichment
        from utils.taxonomy_loader import get_taxonomy

        def fake(signal_id, raw_text, account_id, customer_id, vertical, taxonomy=None, roster=None):
            out = enrichment.normalize_extraction({'signals': [
                {'subtype': subtype, 'quote': raw_text[:40], 'sentiment_score': -0.5, 'urgency_score': 0.5,
                 'escalation_probability': 0.1, 'confidence': confidence, 'people': []}],
                'requires_review': confidence < 0.6, 'is_duplicate': False, 'suggested_action': 'x'}, taxonomy or get_taxonomy(vertical))
            out['llm_model_version'] = 'fake-llm'
            return out
        monkeypatch.setattr(enrichment, 'enrich_signal', fake)
        from mcp_server.cs_pulse_onboarding import submit_signal
        return submit_signal(cid, aid, text, source_type='email', occurred_at='2026-03-18T09:00:00Z')

    def test_journey_read_surface_cites_evidence(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import get_journey, list_journeys, get_evidence
        j = get_journey(cid, aid)
        assert j['account_id'] == aid and j['evidence'] and 'open_review_count' in j
        cited = {nid for e in j['episodes'] for nid in e['evidence_node_ids']}
        assert cited and all(str(n) in j['evidence'] for n in cited)          # every citation resolves
        ev = next(v for v in j['evidence'].values() if v['subtype'] == 'champion_departure')
        assert ev['role'] == 'champion_change' and ev['provenance']['source_platform'] == 'crm_activity'
        assert ev['person']['name'] == 'Elena Rossi' and ev['provenance']['classification_basis'] == 'declared_subtype'
        rows = list_journeys(cid)['journeys']
        me = next(r for r in rows if r['account_id'] == aid)
        assert me['arc_type'] and me['latest']['month'] and me['episodes'] > 0
        byrole = get_evidence(cid, account_id=aid, role='champion_change')['evidence']
        assert byrole and all(r['role'] == 'champion_change' for r in byrole)
        compact = get_journey(cid, aid, compact=True)
        assert 'episodes' not in compact and len(compact['leading_vs_trailing']['series']) <= 3

    def test_unreviewed_low_confidence_counts_less_then_accept_restores(self, tenant, monkeypatch):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import get_review_queue, review_signal, get_journey
        def march_unreviewed():
            with app.app_context():
                j = JourneyData.query.filter_by(account_id=aid).first().journey_json
                return next(s for s in j['leading_vs_trailing']['series'] if s['month'] == '2026-03-01')['unreviewed_count']
        base = march_unreviewed()            # earlier stub-path signals in this tenant are flagged too
        res = self._flagged_signal(cid, aid, monkeypatch, 'Maybe a competitor demo? unclear from the thread', 'competitor_mention', 0.4)
        sid = res['signal_id']
        q = get_review_queue(cid, account_id=aid)
        assert any(r['signal_id'] == sid for r in q['review_queue'])
        assert march_unreviewed() == base + 1
        with app.app_context():
            j = JourneyData.query.filter_by(account_id=aid).first().journey_json
            ep = next(e for e in j['episodes'] if e['evidence_node_ids'] == [res['evidence']['node_id']])
            assert ep['meta']['requires_review'] is True and ep['meta']['review'] is None
        out = review_signal(cid, sid, 'accept', note='confirmed with AE', reviewer='vp-cs@t.test')
        assert out['nodes'][0]['review'] == 'accepted' and out['audit_ids'] and out['requires_review'] is False
        assert not any(r['signal_id'] == sid for r in get_review_queue(cid, account_id=aid)['review_queue'])
        assert march_unreviewed() == base
        j = get_journey(cid, aid)
        assert j['evidence'][str(res['evidence']['node_id'])]['review']['by'] == 'vp-cs@t.test'

    def test_reject_hides_from_journey_but_keeps_node_and_audit(self, tenant, monkeypatch):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import review_signal, get_evidence
        from models import SignalReview
        res = self._flagged_signal(cid, aid, monkeypatch, 'Forwarding the newsletter about pricing changes in the market', 'pricing_concern', 0.5)
        sid, nid = res['signal_id'], res['evidence']['node_id']
        with app.app_context():
            before = next(s for s in JourneyData.query.filter_by(account_id=aid).first().journey_json['leading_vs_trailing']['series']
                          if s['month'] == '2026-03-01')['roles'].get('commercial_pressure', 0)
        out = review_signal(cid, sid, 'reject', note='newsletter, not the customer', reviewer='csm@t.test')
        assert out['nodes'][0]['review'] == 'rejected'
        with app.app_context():
            assert db.session.get(ContextNode, nid) is not None                         # kept for audit
            j = JourneyData.query.filter_by(account_id=aid).first().journey_json
            assert not any(nid in e['evidence_node_ids'] for e in j['episodes'])         # gone from the journey
            after = next(s for s in j['leading_vs_trailing']['series'] if s['month'] == '2026-03-01')['roles'].get('commercial_pressure', 0)
            assert after == before - 1
            a = SignalReview.query.filter_by(signal_id=sid).one()
            assert a.decision == 'reject' and a.was_flagged is True and a.reviewer == 'csm@t.test' and a.created_at
        assert not any(r['node_id'] == nid for r in get_evidence(cid, account_id=aid)['evidence'])
        assert any(r['node_id'] == nid for r in get_evidence(cid, account_id=aid, include_rejected=True)['evidence'])

    def test_blanket_accept_does_not_undo_a_specific_reject(self, tenant, monkeypatch):
        """Live finding 2026-09-04: reject node X, then accept the signal → X came back."""
        cid, aid = tenant
        import signal_engine.enrichment as enrichment
        from utils.taxonomy_loader import get_taxonomy
        from mcp_server.cs_pulse_onboarding import review_signal, submit_signal

        def fake(signal_id, raw_text, account_id, customer_id, vertical, taxonomy=None, roster=None):
            out = enrichment.normalize_extraction({'signals': [
                {'subtype': 'dau_drop', 'quote': 'logins are down a third', 'sentiment_score': -0.5, 'urgency_score': 0.5,
                 'escalation_probability': 0.1, 'confidence': 0.9, 'people': []},
                {'subtype': 'case_study_consent', 'quote': 'happy to do a case study', 'sentiment_score': 0.8, 'urgency_score': 0.1,
                 'escalation_probability': 0.0, 'confidence': 0.9, 'people': []}],
                'requires_review': False, 'is_duplicate': False, 'suggested_action': 'x'}, taxonomy or get_taxonomy(vertical))
            out['llm_model_version'] = 'fake-llm'
            return out
        monkeypatch.setattr(enrichment, 'enrich_signal', fake)
        res = submit_signal(cid, aid, 'Logins are down a third, but they are happy to do a case study', source_type='email',
                            occurred_at='2026-03-22T09:00:00Z')
        sid = res['signal_id']
        n_dau, n_cs = res['evidence']['node_ids']
        review_signal(cid, sid, 'reject', node_id=n_cs, note='sarcasm', reviewer='csm@t.test')
        out = review_signal(cid, sid, 'accept', reviewer='vp@t.test')
        assert [n['node_id'] for n in out['nodes']] == [n_dau]                     # only the undecided node
        with app.app_context():
            assert db.session.get(ContextNode, n_cs).properties['review']['status'] == 'rejected'
        out = review_signal(cid, sid, 'accept')                                      # idempotent: re-confirms n_dau only
        assert [n['node_id'] for n in out['nodes']] == [n_dau]
        out = review_signal(cid, sid, 'accept', node_id=n_cs, reviewer='vp@t.test')   # explicit override is allowed
        assert out['nodes'][0]['review'] == 'accepted'

    def test_narrative_is_in_the_journey_and_every_cite_resolves(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import get_journey
        j = get_journey(cid, aid)
        n = j['narrative']
        ids = {e['episode_id'] for e in j['episodes']}
        assert n['validated'] and n['sentence_count'] > 0
        for ch in n['chapters']:
            for s in ch['sentences']:
                assert s['cites'] and set(s['cites']) <= ids
                for c in s['cites']:                                   # and each cite reaches evidence with provenance
                    ep = next(e for e in j['episodes'] if e['episode_id'] == c)
                    assert all(str(nid) in j['evidence'] for nid in ep['evidence_node_ids'])
        rejected = [o for o in n['omitted'] if o['reason'] == 'rejected_evidence']
        assert rejected and all(c.startswith('sig:') for o in rejected for c in o['cites'])     # the newsletter we rejected earlier

    def test_reclassify_retypes_and_rederives(self, tenant, monkeypatch):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import review_signal
        res = self._flagged_signal(cid, aid, monkeypatch, 'They said the new dashboard is great and want it for two more teams', 'feature_request', 0.5)
        sid, nid = res['signal_id'], res['evidence']['node_id']
        out = review_signal(cid, sid, 'reclassify', subtype='new_team_rollout', reviewer='csm@t.test')
        n = out['nodes'][0]
        assert n['subtype'] == 'new_team_rollout' and n['role'] == 'expansion_intent' and n['effective_urgency'] == 'high'
        with app.app_context():
            node = db.session.get(ContextNode, nid)
            assert node.properties['original_subtype'] == 'feature_request' and node.properties['classification_basis'] == 'human_reclassified'
            assert float(node.properties['sentiment_score']) > 0          # polarity re-derived from the new role
            j = JourneyData.query.filter_by(account_id=aid).first().journey_json
            ep = next(e for e in j['episodes'] if e['evidence_node_ids'] == [nid])
            assert ep['role'] == 'expansion_intent' and ep['meta']['review'] == 'reclassified'
        import pytest
        from fastmcp.exceptions import ToolError
        with pytest.raises(ToolError):
            review_signal(cid, sid, 'reclassify', subtype='not_a_subtype')
        with pytest.raises(ToolError):
            review_signal(cid, sid, 'maybe')


class TestStaleJourneyRebuild:
    def test_old_generator_version_is_rebuilt_and_health_reports_it(self, tenant):
        cid, aid = tenant
        from journeys.wizard_a import GENERATOR_VERSION, stale_journey_query, rebuild_stale_journeys
        with app.app_context():
            jd = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first()
            assert jd.generator_version == GENERATOR_VERSION and jd.journey_json['generator_version'] == GENERATOR_VERSION
            old = dict(jd.journey_json); old.pop('live_months', None); old.pop('generator_version', None)
            jd.journey_json = old; jd.generator_version = '3.0'
            db.session.commit()
            assert stale_journey_query(cid).count() == 1
            out = rebuild_stale_journeys(cid)
            assert out['stale'] == 1 and out['rebuilt'] == 1 and out['customers'] == {cid: 1}
            jd = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first()
            assert jd.generator_version == GENERATOR_VERSION and 'live_months' in jd.journey_json
            assert stale_journey_query(cid).count() == 0
            assert rebuild_stale_journeys(cid)['stale'] == 0          # idempotent


class TestOutcomeLogging:
    def test_log_outcome_links_signals_rebuilds_journey_and_closes_the_renewal_gap(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import log_outcome, get_journey, get_evidence
        from models import ContextEdge
        j = get_journey(cid, aid)                      # (this tenant's as_of is still spring 2026, so its Aug renewal has not 'passed' here)
        champion_node = next(v for v in j['evidence'].values() if v['subtype'] == 'champion_departure')
        res = log_outcome(cid, aid, 'renewal_secured', '2026-08-03', revenue=1_800_000, note='Signed 12-month renewal at flat ARR',
                          linked_signal_ids=[champion_node['provenance']['signal_id']], decided_by='ae@t.test', source_ref='SO-2211')
        assert res['status'] == 'logged' and res['bucket'] == 'protected' and res['evidence_clamped'] is False
        assert res['linked_signal_node_ids'] == [champion_node['node_id']] and res['unresolved_signal_refs'] == []
        with app.app_context():
            n = db.session.get(ContextNode, res['node_id'])
            assert n.node_type == 'OUTCOME' and n.source == 'observed' and n.tier == 1 and float(n.confidence) == 1.0
            assert n.properties['decided_by'] == 'ae@t.test' and n.source_ref == 'SO-2211' and n.source_event_id.startswith('outcome:')
            e = ContextEdge.query.filter_by(to_node_id=n.node_id, edge_type='LED_TO').one()
            assert e.from_node_id == champion_node['node_id'] and e.created_by == 'log_outcome'
        j = get_journey(cid, aid)
        assert not any(o.get('template') == 'renewal_outcome' for o in j['narrative']['omitted'])
        ep = next(e for e in j['episodes'] if e['kind'] == 'outcome' and e['subtype'] == 'renewal_secured')
        assert ep['revenue'] == 1_800_000 and ep['revenue_bucket'] == 'protected'
        assert any('renewal secured' in s['text'].lower() for ch in j['narrative']['chapters'] for s in ch['sentences'])
        assert any(v['subtype'] == 'renewal_secured' for v in get_evidence(cid, account_id=aid)['evidence'])
        # idempotent, loss sign, vocabulary
        assert log_outcome(cid, aid, 'renewal_secured', '2026-08-03', revenue=1_800_000)['status'] == 'exists'
        loss = log_outcome(cid, aid, 'contraction', '2026-08-20', revenue=200_000, decided_by='ae@t.test')
        assert loss['revenue'] == -200_000 and loss['bucket'] == 'lost'
        import pytest
        from fastmcp.exceptions import ToolError
        with pytest.raises(ToolError):
            log_outcome(cid, aid, 'made_up_outcome', '2026-08-03')
        with pytest.raises(ToolError):
            log_outcome(cid, aid, 'churn_lost', '')

    def test_outcome_without_note_or_decider_is_clamped(self, tenant):
        cid, aid = tenant
        from mcp_server.cs_pulse_onboarding import log_outcome
        res = log_outcome(cid, aid, 'expansion_opportunity', '2026-09-01', revenue=50_000)
        assert res['evidence_clamped'] is True and res['confidence'] <= 0.3         # unearned claim, honestly marked


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

    def test_read_surface_and_review_routes(self, client, tenant):
        cid, aid = tenant
        from journeys.http import ROUTES as JR
        from signal_engine.http import ROUTES as SR
        assert '/api/journeys' in JR and '/api/signals/review' in SR and '/api/signals/review/history' in SR
        assert client.get(f'/api/journeys?customer_id={cid}').status_code == 401
        h = {'Authorization': f'Bearer {client.key}'}
        rows = client.get(f'/api/journeys?customer_id={cid}', headers=h).json()['journeys']
        assert any(r['account_id'] == aid for r in rows)
        j = client.get(f'/api/journeys/{aid}?customer_id={cid}&compact=1', headers=h).json()
        assert j['account_id'] == aid and j['evidence']
        ev = client.get(f'/api/evidence?customer_id={cid}&account_id={aid}&role=champion_change', headers=h).json()
        assert ev['count'] >= 1
        r = client.post('/api/signals/review', headers=h, json={'customer_id': cid, 'signal_id': 'nope', 'decision': 'accept'})
        assert r.status_code == 400
        assert '/api/outcomes' in JR
        v = client.get(f'/api/outcomes/vocabulary?customer_id={cid}', headers=h).json()['outcome_types']
        assert 'renewal_secured' in v['protected'] and 'churn_lost' in v['lost']
        r = client.post('/api/outcomes', headers=h, json={'customer_id': cid, 'account_id': aid, 'outcome_type': 'nope', 'occurred_at': '2026-08-01'})
        assert r.status_code == 400 and 'allowed' in r.json()['error']
        assert client.post('/api/outcomes', json={'customer_id': cid, 'account_id': aid, 'outcome_type': 'churn_lost', 'occurred_at': '2026-08-01'}).status_code == 401
        hist = client.get(f'/api/signals/review/history?customer_id={cid}', headers=h).json()['history']
        assert len(hist) >= 3 and {x['decision'] for x in hist} >= {'accept', 'reject', 'reclassify'}

    def test_status_and_auth(self, client, tenant):
        cid, aid = tenant
        h = client.get('/health').json()
        assert h['journey_generator_version'] and 'stale_journeys' in h['counts']
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
