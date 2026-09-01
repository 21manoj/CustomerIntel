"""
Tier 1 live-parity checkpoint (2026-09-01).

The old repo (CustomerSuccessAI-DataCenter, being retired) and this build
were each given an identical fixture -- same customer/account/health-score/
context-graph-node data -- and the same Tier 1 functions were run against
each. Results diffed byte-identical: churn_pct_for_health, KPI/pillar
catalog resolution, the I12 invariant, and I3' evidence-clamping all
produce the same output in both codebases despite this build's dead-code
removal and DC2SKPI/dc2s_* rename.

This test pins those known-correct values as assertions so the parity
holds going forward without needing the old repo present to diff against
-- it won't exist forever. If any of these ever change, that's either a
real regression or a deliberate behavior change that needs its own
justification, not a silent drift.
"""
import os
import sys
import uuid
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from extensions import db


def _make_app():
    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test'
    )
    db.init_app(_app)
    return _app


app = _make_app()

from models import Account, ContextNode, Customer, CustomerConfig, HealthScore


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier1_live_parity.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


@pytest.fixture(scope='module')
def fixture():
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    _assert_isolated_test_db(db_uri)
    with app.app_context():
        db.create_all()
        email = f'parity_{uuid.uuid4().hex[:8]}@test.com'
        customer = Customer(customer_name='Parity Test Co', email=email)
        db.session.add(customer)
        db.session.commit()

        config = CustomerConfig(customer_id=customer.customer_id, vertical='datacenter_v1')
        db.session.add(config)

        account = Account(
            customer_id=customer.customer_id,
            account_name='Parity Test Account',
            revenue=2_000_000,
            external_account_id=f'PARITY-{uuid.uuid4().hex[:8]}',
            account_status='active',  # deliberately mismatched vs health=45 (critical) -- I12
        )
        db.session.add(account)
        db.session.commit()

        hs = HealthScore(
            account_id=account.account_id,
            measurement_month=date(2026, 3, 1),
            health_score=45.0,
            health_status='critical',
        )
        db.session.add(hs)

        node_with_evidence = ContextNode(
            customer_id=customer.customer_id, account_id=account.account_id,
            node_type='OUTCOME', node_subtype='revenue_at_risk',
            source='observed', source_platform='csv_import',
            title='Revenue at risk -- parity fixture',
            properties={'evidence': 'Account showing churn signals, ARR at risk.'},
            tier=1, confidence=1.0,
            revenue_impact=-300000, revenue_impact_type='at_risk',
            occurred_at=datetime(2026, 3, 1),
        )
        db.session.add(node_with_evidence)

        node_no_evidence = ContextNode(
            customer_id=customer.customer_id, account_id=account.account_id,
            node_type='OUTCOME', node_subtype='revenue_protected',
            source='observed', source_platform='csv_import',
            title='Revenue protected -- parity fixture',
            properties={},
            tier=1, confidence=1.0,
            revenue_impact=150000, revenue_impact_type='protected',
            occurred_at=datetime(2026, 3, 5),
        )
        db.session.add(node_no_evidence)
        db.session.commit()

        from utils.context_graph_invariants import clamp_unearned_confidence
        for node in (node_with_evidence, node_no_evidence):
            conf, props, tier, clamped = clamp_unearned_confidence(
                node_type='OUTCOME', source_platform='csv_import', source_ref=None,
                confidence=1.0, properties=node.properties, tier=1,
            )
            node.confidence = conf
            node.tier = tier
            node.properties = props
        db.session.commit()

        yield {
            'customer_id': customer.customer_id,
            'account_id': account.account_id,
            'node_with_evidence_id': node_with_evidence.node_id,
            'node_no_evidence_id': node_no_evidence.node_id,
        }

        db.session.remove()
        db.drop_all()


class TestTier1LiveParity:
    def test_churn_pct_for_health_matches_old_repo(self, fixture):
        with app.app_context():
            from utils.context_graph import churn_pct_for_health
            assert churn_pct_for_health(45.0) == 0.40

    def test_datacenter_v1_catalog_matches_old_repo(self, fixture):
        with app.app_context():
            from utils.vertical_registry import get_kpis, get_pillars
            kpis = get_kpis('datacenter_v1')
            pillars = get_pillars('datacenter_v1')
            assert len(kpis) == 38
            assert len(pillars) == 6
            assert sorted(pillars.keys()) == ['P1', 'P2', 'P3', 'P4', 'P5', 'P6']

    def test_i12_invariant_fires_matches_old_repo(self, fixture):
        with app.app_context():
            from utils.context_graph_invariants import run_all_invariants
            violations = run_all_invariants(fixture['customer_id'])
            i12_hits = [v for v in violations if v.invariant_id == 'I12']
            assert len(i12_hits) == 1

    def test_evidence_clamping_matches_old_repo(self, fixture):
        with app.app_context():
            with_evidence = ContextNode.query.get(fixture['node_with_evidence_id'])
            no_evidence = ContextNode.query.get(fixture['node_no_evidence_id'])
            assert float(with_evidence.confidence) == 1.0
            assert with_evidence.tier == 1
            assert float(no_evidence.confidence) == 0.3
            assert no_evidence.tier == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
