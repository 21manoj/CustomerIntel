"""
journeys/edge_derivation.py — phase-transition LED_TO edges, tprm_v1 only.

Two layers:
  * a direct unit test of derive_phase_transition_edges() against a
    constructed phases/episodes fixture (the skip branches, direction,
    derivation label, and idempotent re-run)
  * an integration test through journeys.wizard_a.run_wizard_a(), proving
    it fires for a real tprm_v1 journey AND — the important half — that it
    fires for NOTHING else: a same-shaped non-tprm_v1 fixture (saas_premium,
    already exercised the same way by test_signals_only_journey.py) writes
    ZERO ContextEdge rows, proving the `vertical == 'tprm_v1'` gate in
    wizard_a.run_wizard_a() actually holds.
"""
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()

from models import Account, ContextEdge, ContextNode, Customer, JourneyData  # noqa: E402
from journeys.edge_derivation import PHASE_TRANSITION_DERIVATION, derive_phase_transition_edges  # noqa: E402


# ═════════════════════════════════════════════════════════════════════
# Direct unit test of derive_phase_transition_edges()
# ═════════════════════════════════════════════════════════════════════

@pytest.fixture()
def graph():
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        c = Customer(customer_name=f'EdgeDeriv Unit {tag}', email=f'edu_{tag}@t.test', domain=f'edu-{tag}.test')
        db.session.add(c)
        db.session.flush()
        a = Account(customer_id=c.customer_id, account_name='Unit Vendor', revenue=100_000, external_account_id=tag)
        db.session.add(a)
        db.session.flush()

        def node(dt, title):
            n = ContextNode(customer_id=c.customer_id, account_id=a.account_id, node_type='SIGNAL',
                            node_subtype='vendor_system_outage', source='observed', title=title, tier=1,
                            occurred_at=dt, properties={'quote': title}, source_platform='test')
            db.session.add(n)
            db.session.flush()
            return n

        n1 = node(datetime(2026, 1, 5), 'deterioration trigger')
        n2 = node(datetime(2026, 3, 5), 'intervention trigger')
        n3 = node(datetime(2026, 4, 5), 'resolution trigger')
        n_no_evidence = node(datetime(2026, 2, 1), 'orphan episode, no evidence_node_ids')
        db.session.commit()

        yield {
            'customer_id': c.customer_id, 'account_id': a.account_id,
            'n1': n1.node_id, 'n2': n2.node_id, 'n3': n3.node_id, 'n_no_evidence': n_no_evidence.node_id,
        }
        db.session.remove()
        db.drop_all()


def _episode(episode_id, node_ids):
    return {'episode_id': episode_id, 'evidence_node_ids': node_ids, 'role': 'infra_incident', 'date': '2026-01-01T00:00:00'}


def test_creates_led_to_edges_in_chronological_order_with_correct_direction(graph):
    g = graph
    phases = [
        {'name': 'deterioration', 'entered_at': '2026-01-01', 'exited_at': '2026-03-01', 'trigger_episode_id': 'sig:a'},
        {'name': 'intervention', 'entered_at': '2026-03-01', 'exited_at': '2026-04-01', 'trigger_episode_id': 'sig:b'},
        {'name': 'resolution', 'entered_at': '2026-04-01', 'exited_at': None, 'trigger_episode_id': 'sig:c'},
    ]
    episodes = [
        _episode('sig:a', [g['n1']]),
        _episode('sig:b', [g['n2']]),
        _episode('sig:c', [g['n3']]),
    ]
    with app.app_context():
        results = derive_phase_transition_edges(g['customer_id'], g['account_id'], phases, episodes)
        assert [r['status'] for r in results] == ['created', 'created']
        assert results[0]['from_node_id'] == g['n1'] and results[0]['to_node_id'] == g['n2']
        assert results[1]['from_node_id'] == g['n2'] and results[1]['to_node_id'] == g['n3']

        edges = ContextEdge.query.filter_by(customer_id=g['customer_id'], edge_type='LED_TO').order_by(ContextEdge.edge_id).all()
        assert len(edges) == 2
        e1, e2 = edges
        # from = earlier phase's trigger (cause), to = later phase's trigger (effect) —
        # the same from=earlier/to=later convention as journeys/outcomes.py and playbooks/governance.py's LED_TO writers.
        assert (e1.from_node_id, e1.to_node_id) == (g['n1'], g['n2'])
        assert (e2.from_node_id, e2.to_node_id) == (g['n2'], g['n3'])
        for e in (e1, e2):
            assert e.confidence is None
            assert e.source_platform == 'journeys.wizard_a'
            assert e.properties['derivation'] == PHASE_TRANSITION_DERIVATION
            assert e.properties['evidence_tier'] == 'inferred'
        assert e1.properties['label'] == 'deterioration to intervention'
        assert e2.properties['label'] == 'intervention to resolution'


def test_rerun_is_idempotent_not_duplicated(graph):
    g = graph
    phases = [
        {'name': 'deterioration', 'entered_at': '2026-01-01', 'exited_at': '2026-03-01', 'trigger_episode_id': 'sig:a'},
        {'name': 'intervention', 'entered_at': '2026-03-01', 'exited_at': None, 'trigger_episode_id': 'sig:b'},
    ]
    episodes = [_episode('sig:a', [g['n1']]), _episode('sig:b', [g['n2']])]
    with app.app_context():
        first = derive_phase_transition_edges(g['customer_id'], g['account_id'], phases, episodes)
        assert first[0]['status'] == 'created'
        second = derive_phase_transition_edges(g['customer_id'], g['account_id'], phases, episodes)
        assert second[0]['status'] == 'updated'
        assert second[0]['edge_id'] == first[0]['edge_id']
        assert ContextEdge.query.filter_by(customer_id=g['customer_id'], edge_type='LED_TO').count() == 1


def test_skips_pair_with_no_trigger_episode(graph):
    g = graph
    phases = [
        {'name': 'baseline', 'entered_at': '2026-01-01', 'exited_at': '2026-02-01', 'trigger_episode_id': None},
        {'name': 'deterioration', 'entered_at': '2026-02-01', 'exited_at': None, 'trigger_episode_id': 'sig:a'},
    ]
    episodes = [_episode('sig:a', [g['n1']])]
    with app.app_context():
        results = derive_phase_transition_edges(g['customer_id'], g['account_id'], phases, episodes)
        assert results == [{
            'account_id': g['account_id'], 'phase_a': 'baseline', 'phase_b': 'deterioration',
            'trigger_episode_id_a': None, 'trigger_episode_id_b': 'sig:a', 'status': 'skipped_no_trigger',
        }]
        assert ContextEdge.query.filter_by(customer_id=g['customer_id']).count() == 0


def test_skips_pair_sharing_the_same_trigger_episode(graph):
    g = graph
    phases = [
        {'name': 'deterioration', 'entered_at': '2026-01-01', 'exited_at': '2026-02-01', 'trigger_episode_id': 'sig:a'},
        {'name': 'still_deterioration', 'entered_at': '2026-02-01', 'exited_at': None, 'trigger_episode_id': 'sig:a'},
    ]
    episodes = [_episode('sig:a', [g['n1']])]
    with app.app_context():
        results = derive_phase_transition_edges(g['customer_id'], g['account_id'], phases, episodes)
        assert results[0]['status'] == 'skipped_same_trigger'
        assert ContextEdge.query.filter_by(customer_id=g['customer_id']).count() == 0


def test_skips_pair_when_trigger_episode_has_no_evidence_node_ids(graph):
    g = graph
    phases = [
        {'name': 'deterioration', 'entered_at': '2026-01-01', 'exited_at': '2026-02-01', 'trigger_episode_id': 'sig:a'},
        {'name': 'intervention', 'entered_at': '2026-02-01', 'exited_at': None, 'trigger_episode_id': 'sig:orphan'},
    ]
    episodes = [_episode('sig:a', [g['n1']]), _episode('sig:orphan', [])]
    with app.app_context():
        results = derive_phase_transition_edges(g['customer_id'], g['account_id'], phases, episodes)
        assert results[0]['status'] == 'skipped_no_evidence'
        assert ContextEdge.query.filter_by(customer_id=g['customer_id']).count() == 0


def test_no_phases_or_single_phase_derives_nothing(graph):
    g = graph
    with app.app_context():
        assert derive_phase_transition_edges(g['customer_id'], g['account_id'], [], []) == []
        one = [{'name': 'baseline', 'entered_at': '2026-01-01', 'exited_at': None, 'trigger_episode_id': 'sig:a'}]
        assert derive_phase_transition_edges(g['customer_id'], g['account_id'], one, [_episode('sig:a', [g['n1']])]) == []


# ═════════════════════════════════════════════════════════════════════
# Integration through journeys.wizard_a.run_wizard_a()
# ═════════════════════════════════════════════════════════════════════

# Same shape (4 signals, 4 consecutive months: 2 negative -> 1 intervention ->
# 1 positive) reused for both verticals below, so the negative test is a real
# negative: if the tprm_v1 gate in wizard_a were ever broadened or dropped,
# this fixture WOULD produce edges, and the test would catch it.
_FIXTURE_SIGNALS = {
    'tprm_v1': [
        (datetime(2026, 1, 5), 'vendor_system_outage', 'Prod outage at the vendor for three hours'),
        (datetime(2026, 2, 5), 'vendor_issue_escalated', 'Escalated to their VP of Ops'),
        (datetime(2026, 3, 5), 'risk_mitigation_action', 'Corrective action plan issued and accepted'),
        (datetime(2026, 4, 5), 'vendor_remediation_completed', 'Remediation verified complete, SLA restored'),
    ],
    # subtypes proven to yield deterioration -> intervention -> resolution on a
    # signals-only vertical by tests/test_signals_only_journey.py's HAL fixture
    'saas_premium': [
        (datetime(2026, 1, 20), 'champion_departure', 'Champion left for a competitor'),
        (datetime(2026, 2, 10), 'engagement_gap', 'Two QBR invitations declined'),
        (datetime(2026, 3, 5), 'csm_intervention', 'Exec sponsor rebuild kicked off'),
        (datetime(2026, 4, 8), 'kpi_stabilized', 'Weekly usage steady three weeks running'),
    ],
}


def _build_tenant(vertical: str, tag: str):
    from mcp_server.cs_pulse_onboarding import create_customer
    cid = create_customer(data_origin='synthetic_test', name=f'EdgeDeriv {vertical} {tag}', domain=f'ed-{vertical}-{tag}.test',
                          vertical=vertical, admin_email=f'ed_{vertical}_{tag}@t.test', admin_name='E')['customer_id']
    a = Account(customer_id=cid, account_name=f'Vendor {tag}', revenue=500_000, vertical=vertical,
               external_account_id=f'ED-{vertical}-{tag}')
    db.session.add(a)
    db.session.flush()
    aid = a.account_id
    node_ids = []
    for i, (dt, sub, title) in enumerate(_FIXTURE_SIGNALS[vertical]):
        n = ContextNode(customer_id=cid, account_id=aid, node_type='SIGNAL', node_subtype=sub,
                        source='observed', title=title, tier=2, occurred_at=dt,
                        properties={'quote': title}, source_platform='test', source_event_id=f'{tag}_{i}')
        db.session.add(n)
        db.session.flush()
        node_ids.append(n.node_id)
    db.session.commit()
    return cid, aid, node_ids


@pytest.fixture()
def tprm_tenant():
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        cid, aid, node_ids = _build_tenant('tprm_v1', tag)
        yield cid, aid, node_ids
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def saas_tenant():
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        cid, aid, node_ids = _build_tenant('saas_premium', tag)
        yield cid, aid, node_ids
        db.session.remove()
        db.drop_all()


def test_wizard_a_tprm_v1_writes_the_phase_transition_edges(tprm_tenant):
    cid, aid, node_ids = tprm_tenant
    with app.app_context():
        from journeys.wizard_a import run_wizard_a
        res = run_wizard_a(cid, evaluate_playbooks=False)
        assert res['status'] == 'completed' and res['processed'] == 1

        journey = JourneyData.query.filter_by(customer_id=cid, account_id=aid).one().journey_json
        names = [p['name'] for p in journey['phases']]
        assert names == ['deterioration', 'intervention', 'resolution'], names
        assert all(p['trigger_episode_id'] for p in journey['phases'])

        edges = ContextEdge.query.filter_by(customer_id=cid, edge_type='LED_TO').order_by(ContextEdge.edge_id).all()
        assert len(edges) == 2, [e.properties for e in edges]
        e1, e2 = edges
        # deterioration's trigger is Jan's outage (first negative month; Feb's
        # escalation merges into the same phase) -> intervention's trigger (Mar
        # mitigation) -> resolution's trigger (Apr remediation).
        assert (e1.from_node_id, e1.to_node_id) == (node_ids[0], node_ids[2])
        assert (e2.from_node_id, e2.to_node_id) == (node_ids[2], node_ids[3])
        for e in (e1, e2):
            assert e.confidence is None
            assert e.source_platform == 'journeys.wizard_a'
            assert e.properties['derivation'] == PHASE_TRANSITION_DERIVATION
        assert e1.properties['label'] == 'deterioration to intervention'
        assert e2.properties['label'] == 'intervention to resolution'


def test_wizard_a_non_tprm_v1_vertical_writes_zero_edges(saas_tenant):
    """The gate: the exact same phase-worthy shape on saas_premium must write
    NO edges at all — proving `vertical == 'tprm_v1'` in wizard_a.run_wizard_a
    actually restricts this, not just that the derivation function is correct."""
    cid, aid, node_ids = saas_tenant
    with app.app_context():
        from journeys.wizard_a import run_wizard_a
        res = run_wizard_a(cid, evaluate_playbooks=False)
        assert res['status'] == 'completed' and res['processed'] == 1

        journey = JourneyData.query.filter_by(customer_id=cid, account_id=aid).one().journey_json
        names = [p['name'] for p in journey['phases']]
        assert names == ['deterioration', 'intervention', 'resolution'], names  # proves this fixture WOULD have produced edges
        assert all(p['trigger_episode_id'] for p in journey['phases'])

        assert ContextEdge.query.filter_by(customer_id=cid).count() == 0
