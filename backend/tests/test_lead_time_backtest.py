"""
Lead-time backtest harness (docs/design/wizard-a-assessment.md §7.5 step 1)
on a two-account tenant built by hand so every number is checkable:

  Churner: health 80,78,70,60,55,45 (Jan..Jun 2026); one negative signal on
           Feb 10 → leading early_warning from Feb; churn_lost outcome on
           Jun 20. Leading warned at Feb 28 (23:59, month end — the
           conservative availability date) → 111 days before the event;
           trailing first crossed at-risk in June, month end after the
           event → no lead credited, and not a false alarm either (same
           month as the event).
  Quiet:   healthy and flat; one negative signal Mar 15 with no event →
           leading warnings in Mar and Apr are false alarms (2 of 12
           account-months).
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
        'DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
    db.init_app(_app)
    return _app


app = _make_app()
import mcp_server.common as _common
_common._flask_app = app

import utils.health_thresholds as ht
from models import Account, HealthScore, ContextNode, Customer

MONTHS = [date(2026, m, 1) for m in range(1, 7)]


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


def _account(cid, name, series, signals, outcome=None):
    a = Account(customer_id=cid, account_name=name, revenue=1_000_000, vertical='saas_premium')
    db.session.add(a)
    db.session.flush()
    for m, s in zip(MONTHS, series):
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s,
                                   kpi_only_score=s, health_status=ht.classify(s)))
    for i, (dt, sub, sent) in enumerate(signals):
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='SIGNAL', node_subtype=sub,
                                   source='observed', title=f'{sub} {name}', tier=2, occurred_at=dt,
                                   properties={'sentiment_score': str(sent)}, source_platform='csv_import',
                                   source_event_id=f'{name}_{i}'))
    if outcome:
        dt, sub, rev = outcome
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='OUTCOME', node_subtype=sub,
                                   source='observed', title=f'{sub} {name}', tier=1, occurred_at=dt,
                                   revenue_impact=rev, revenue_impact_type=sub, properties={},
                                   source_platform='csv_import'))
    return a.account_id


@pytest.fixture(scope='module')
def report():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Backtest {tag}', domain=f'bt-{tag}.test', vertical='saas_premium',
                              admin_email=f'bt_{tag}@t.test', admin_name='B')['customer_id']
        _account(cid, 'Churner', [80, 78, 70, 60, 55, 45],
                 [(datetime(2026, 2, 10), 'usage_decline', -0.8)],
                 outcome=(datetime(2026, 6, 20), 'churn_lost', -500000.0))
        _account(cid, 'Quiet', [85, 85, 86, 85, 86, 85],
                 [(datetime(2026, 3, 15), 'support_escalation', -0.7)])
        db.session.commit()
        from journeys.wizard_a import run_wizard_a
        run_wizard_a(cid)
        from evals.lead_time_backtest import run_backtest, format_report
        rep = run_backtest(cid, horizon_days=180, min_events=10)
        print('\n' + format_report(rep))
        yield cid, rep
        db.session.remove()
        db.drop_all()


def test_h1_numbers(report):
    cid, rep = report
    h1 = rep['results']['H1_retention']
    assert h1['events'] == 1 and h1['account_months'] == 12
    ev = h1['per_event'][0]
    assert ev['account'] == 'Churner' and ev['event'] == 'churn_lost' and ev['event_date'] == '2026-06-20'
    assert ev['leading_warned_at'] == '2026-02-28' and ev['leading_lead_days'] == 111
    assert ev['trailing_warned_at'] is None                    # June's month end is after the event
    L, T = h1['leading'], h1['trailing']
    assert L['n'] == 1 and L['median'] == 111 and L['recall'] == 1.0
    assert T['n'] == 0 and T['recall'] == 0.0
    # Quiet's Mar + Apr warnings had no event — but the data ends Jun 30,
    # less than the 180-day horizon after them: still open, not false.
    assert L['false_alarm_months'] == 0 and L['censored_warning_months'] == 2
    assert T['false_alarm_months'] == 0 and T['censored_warning_months'] == 0   # June's crossing is in the event's month


def test_verdict_and_evidence_label(report):
    cid, rep = report
    h1 = rep['results']['H1_retention']
    assert h1['verdict'] == 'insufficient_data'                # 1 event < 10
    assert rep['evidence_label'] == 'synthetic_or_unverified — not evidence'
    assert rep['thresholds'] == {'median_lead_days_min': 60, 'recall_min': 0.7, 'false_alarms_per_100_max': 5.0}


def test_refutation_check_runs_when_enough_events(report):
    cid, _ = report
    with app.app_context():
        from evals.lead_time_backtest import run_backtest
        rep = run_backtest(cid, horizon_days=180, min_events=1)
    h1 = rep['results']['H1_retention']
    assert h1['verdict'] == 'supported'                        # median 111, recall 1.0, FA 0 (2 open, censored)
    assert all(c['pass'] for c in h1['checks'].values())
    # with a 60-day horizon: Quiet's two warnings are old enough to judge, and Churner's
    # Feb/Mar warnings are now too far ahead of the June event to count → 4 false alarms → refuted
    with app.app_context():
        short = run_backtest(cid, horizon_days=60, min_events=1)['results']['H1_retention']
    assert short['leading']['false_alarm_months'] == 4 and short['verdict'] == 'refuted'
    assert not short['checks']['false_alarms_per_100']['pass']


def test_measured_label_requires_null_origin_and_assertion(report):
    cid, _ = report
    with app.app_context():
        from evals.lead_time_backtest import run_backtest
        assert run_backtest(cid, assert_real=True)['evidence_label'] == 'measured'
        c = db.session.get(Customer, cid)
        c.data_origin = 'synthetic_eval_profile'
        db.session.commit()
        assert run_backtest(cid, assert_real=True)['evidence_label'] != 'measured'
        c.data_origin = None
        db.session.commit()


def test_h2_has_no_events_here(report):
    cid, rep = report
    h2 = rep['results']['H2_growth']
    assert h2['events'] == 0 and h2['verdict'] == 'insufficient_data'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
