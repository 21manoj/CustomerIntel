"""
Regression tests for the wizard / causal-graph audit (November 2026). Each test pins one thing the
audit found the product saying that was not true:

  1  a customer that has churned (or a vendor that was terminated) kept a renewal forecast, a playbook
     proposal and a priority score as if it were still live
  3  a month in the critical band was named the 'intervention' phase although nobody had intervened
  4  the first-warning sentence claimed lead time when the leading layer flagged late or in the same month
  5  neutral notes (sentiment 0) dragged the leading score down and produced an early warning from nothing
  6  a corrected KPI value re-uploaded for the same month was dropped silently
  9  a month with no health score broke the phase transitions
  10 a user restricted to some accounts could read the whole tenant's forecast rows and totals
  +  one account failing to build left the rest unbuilt, and the failure was invisible
"""
import os
import sys
import uuid
from datetime import date, datetime
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
import utils.health_thresholds as ht                      # noqa: E402
from models import Account, HealthScore, ContextNode, JourneyData, AccountForecast, KPIMeasurement   # noqa: E402

MONTHS = [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]


def _account(cid, name, vertical, series, signals=(), outcomes=(), profile=None, revenue=1_000_000):
    a = Account(customer_id=cid, account_name=name, revenue=revenue, vertical=vertical, profile_metadata=profile or None)
    db.session.add(a)
    db.session.flush()
    for m, s in zip(MONTHS, series):
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s, kpi_only_score=s,
                                   health_status=ht.classify(s) if s is not None else None))
    for i, (dt, sub, sent) in enumerate(signals):
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='SIGNAL', node_subtype=sub, source='observed',
                                   title=f'{sub} ({name})', tier=2, occurred_at=dt, properties={'sentiment_score': str(sent)},
                                   source_platform='csv_import', source_event_id=f'{name}_{i}'))
    for i, (dt, sub, rev) in enumerate(outcomes):
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='OUTCOME', node_subtype=sub, source='observed',
                                   title=f'{sub} ({name})', tier=1, occurred_at=dt, revenue_impact=rev, revenue_impact_type=sub,
                                   properties={}, source_platform='csv_import', source_event_id=f'{name}_out_{i}'))
    return a.account_id


def _create(vertical):
    from mcp_server.cs_pulse_onboarding import create_customer
    tag = uuid.uuid4().hex[:8]
    return create_customer(name=f'Sanctity {tag}', domain=f'sn-{tag}.test', vertical=vertical, admin_email=f'sn_{tag}@t.test',
                           admin_name='S', data_origin='synthetic_test')['customer_id']


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        cid = _create('saas_premium')
        ids = {
            # churned in March: every later surface must say closed, not forecast a renewal
            'churned': _account(cid, 'Churned Co', 'saas_premium', [70, 66, 60, 55, 48, 45],
                                [(datetime(2026, 1, 20), 'usage_decline', -0.6)],
                                [(datetime(2026, 3, 5), 'churn_lost', -800_000.0)], profile={'renewal_date': '2026-06-01'}, revenue=800_000),
            # churned, then won back: live again
            'won_back': _account(cid, 'Won Back Co', 'saas_premium', [70, 60, 50, 55, 70, 80],
                                 outcomes=[(datetime(2026, 1, 5), 'churn_lost', -500_000.0), (datetime(2026, 3, 20), 'renewal_secured', 500_000.0)],
                                 profile={'renewal_date': '2026-12-01'}),
            # a partial loss does not end the account
            'contracted': _account(cid, 'Contracted Co', 'saas_premium', [80, 78, 76, 75, 74, 73],
                                   outcomes=[(datetime(2026, 2, 5), 'contraction', -100_000.0)], profile={'renewal_date': '2026-09-01'}),
            # fell into the critical band, nobody did anything
            'unattended': _account(cid, 'Unattended Co', 'saas_premium', [80, 78, 45, 40, 42, 41], profile={'renewal_date': '2026-10-01'}),
            # KPIs fell in January, the first negative signal arrived in March: the leading layer did not lead
            'late_signal': _account(cid, 'Late Signal Co', 'saas_premium', [80, 60, 45, 42, 40, 40],
                                    [(datetime(2026, 3, 10), 'support_escalation', -0.8), (datetime(2026, 3, 12), 'usage_decline', -0.7)],
                                    profile={'renewal_date': '2026-11-01'}),
            # healthy numbers and nothing but neutral notes
            'neutral': _account(cid, 'Neutral Notes Co', 'saas_premium', [85, 85, 86, 85, 86, 85],
                                [(datetime(2026, 2, 3), 'routine_review', 0), (datetime(2026, 2, 10), 'routine_review', 0),
                                 (datetime(2026, 2, 17), 'routine_review', 0)], profile={'renewal_date': '2027-01-01'}),
            # the renewal went against us (a contraction at the decision point) but the account is still live
            'lost_renewal': _account(cid, 'Lost Renewal Co', 'saas_premium', [70, 66, 60, 55, 52, 51],
                                     outcomes=[(datetime(2026, 3, 5), 'contraction', -200_000.0)], profile={'renewal_date': '2026-03-01'}),
            # one month was never scored
            'gap': _account(cid, 'Gap Co', 'saas_premium', [82, 80, None, 45, 44, 60], profile={'renewal_date': '2026-12-01'}),
        }
        db.session.commit()
        from journeys.wizard_a import run_wizard_a
        res = run_wizard_a(cid, evaluate_playbooks=False)
        yield cid, ids, res
        db.session.remove()
        db.drop_all()


def _journey(cid, aid):
    return JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json


def _sentences(j, template=None):
    return [s for ch in j['narrative']['chapters'] for s in ch['sentences'] if template is None or s['template'] == template]


# ── 1: a closed account ───────────────────────────────────────────────

def test_taxonomy_names_the_outcomes_that_end_an_account():
    from utils.taxonomy_loader import get_taxonomy
    saas, tprm = get_taxonomy('saas_premium'), get_taxonomy('tprm_v1')
    assert {'churn_lost', 'decommissioned'} <= saas.account_ending_outcome_subtypes
    assert 'contraction' not in saas.account_ending_outcome_subtypes               # a partial loss is not the end
    assert {'vendor_terminated', 'vendor_replaced', 'vendor_contract_not_renewed'} <= tprm.account_ending_outcome_subtypes
    for t in (saas, tprm):
        assert t.account_ending_outcome_subtypes <= t.revenue_buckets['lost'] if hasattr(t, 'revenue_buckets') else True


def test_churned_account_is_closed_on_the_journey_and_in_the_story(tenant):
    cid, ids, res = tenant
    with app.app_context():
        assert res['status'] == 'completed' and not res['failed']
        j = _journey(cid, ids['churned'])
        life = j['lifecycle']
        assert life['status'] == 'closed' and life['subtype'] == 'churn_lost' and life['closed_at'].startswith('2026-03-05')
        closed = _sentences(j, 'account_closed')
        assert len(closed) == 1 and 'is closed; nothing is forecast for it' in closed[0]['text'] and closed[0]['cites'] == [life['by']]
        assert not _sentences(j, 'forecast_statement')
        assert _journey(cid, ids['won_back'])['lifecycle'] == {'status': 'active'}      # the renewal after the churn reopens it
        assert _journey(cid, ids['contracted'])['lifecycle'] == {'status': 'active'}


def test_closed_account_gets_no_forecast_no_playbook_and_no_priority(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        from wizards.wizard_d_foresight import run_wizard_d, get_forecast
        res = run_wizard_d(cid)
        assert res['status'] == 'completed' and res['closed_accounts'] == 1
        block = AccountForecast.query.filter_by(run_id=res['run_id'], account_id=ids['churned']).first().forecast_json
        assert block['status'] == 'closed' and block['basis'] == 'closed'
        assert block['retention']['p'] == 0 and block['expansion']['p'] == 0 and block['revenue']['expected_arr_end'] == 0
        assert block['revenue']['arr_before_close'] == 800_000
        fc = get_forecast(cid)
        rows = {r['account_id']: r for r in fc['accounts']}
        assert rows[ids['churned']]['status'] == 'closed' and rows[ids['won_back']]['status'] == 'forecast'
        # the portfolio is the live book: the closed account's ARR is reported beside it, not inside it
        assert fc['portfolio']['closed']['accounts'] == 1 and fc['portfolio']['closed']['arr_before_close'] == 800_000
        assert fc['portfolio']['accounts'] == len(ids) - 1
        # a forecast written before the churn must not survive as 'current'
        j = _journey(cid, ids['churned'])
        assert not _sentences(j, 'forecast_statement')

        from playbooks.governance import evaluate
        ev = evaluate(cid, dry_run=True)
        assert {'account_id': ids['churned'], 'playbook_id': None, 'reason': 'account_closed'} in ev['skipped']
        assert not [p for p in ev.get('proposed', []) if p.get('account_id') == ids['churned']]

        from roi.priorities import investment_priorities
        pr = investment_priorities(cid, account_id=ids['churned'])
        row = pr['rows'][0]
        assert row['account_id'] == ids['churned'] and row['lifecycle'] == 'closed'
        assert row['risk_factor'] == 0 and row['opportunity_factor'] == 0


def test_account_that_just_lost_its_renewal_is_not_read_as_safer_than_before(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        from wizards.wizard_d_foresight import run_wizard_d
        from wizards import wizard_d_settings as settings
        res = run_wizard_d(cid)
        block = AccountForecast.query.filter_by(run_id=res['run_id'], account_id=ids['lost_renewal']).first().forecast_json
        assert block['status'] == 'forecast' and block['decision_point']['resolved_negative'] is True
        assert block['retention']['mode'] == 'after_negative_decision'
        assert 'last_decision_negative' in [d['factor'] for d in block['drivers']]
        # it used to be read on the mid-term formula: about 97% retained, the month after it shrank
        assert block['retention']['p'] <= settings.get('prior', 'base_retention_at_decision')


# ── 3: 'intervention' needs an intervention ───────────────────────────

def test_critical_band_without_an_intervention_is_labelled_critical(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        j = _journey(cid, ids['unattended'])
        health_phases = [p for p in j['phases'] if p['name'] == 'intervention']
        assert health_phases and all(p['label'] == 'critical' for p in health_phases)
        assert j['current_phase'] == 'intervention' and j['current_phase_label'] == 'critical'
        assert db.session.get(Account, ids['unattended']).arc_phase == 'critical'
        text = ' '.join(s['text'] for s in _sentences(j))
        assert 'critical band' in text and 'intervention phase' not in text.lower()
        from journeys.read import list_journeys
        listed = {r['account_id']: r for r in list_journeys(cid)}
        assert listed[ids['unattended']]['current_phase_label'] == 'critical'
        assert listed[ids['churned']]['lifecycle'] == 'closed'


# ── 4 and 5: the leading layer ────────────────────────────────────────

def test_first_warning_sentence_admits_when_the_leading_layer_did_not_lead(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        j = _journey(cid, ids['late_signal'])
        lvt = j['leading_vs_trailing']
        assert lvt['lead_days'] is not None and lvt['lead_days'] <= 0
        s = _sentences(j, 'first_warning_gap')
        assert len(s) == 1
        assert ('did not lead' in s[0]['text']) or ('no advance warning' in s[0]['text'])
        assert 'days later' not in s[0]['text']


def test_first_warning_wording_for_each_sign_of_the_gap():
    from journeys.narrative import _t_first_warning
    ep = {'e1': {'episode_id': 'e1', 'title': 'Champion left', 'date': '2026-01-10T00:00:00', 'subtype': 'champion_departure'}}
    base = {'first_leading_warning_at': '2026-01-01', 'first_trailing_warning_at': '2026-03-01',
            'series': [{'month': '2026-01-01', 'contributing_episode_ids': ['e1']}],
            'trailing_warning_threshold': 50, 'trailing_healthy_threshold': 70}
    ahead = _t_first_warning(base | {'lead_days': 59, 'trailing_at_first_leading': 82.0}, ep)
    assert ahead and '59 days later' in ahead[0] and 'already in the at-risk band' not in ahead[0]
    soft = _t_first_warning(base | {'lead_days': 59, 'trailing_at_first_leading': 61.0}, ep)
    assert soft and 'already in the at-risk band' in soft[0]                        # lead time, but the numbers were already soft
    same = _t_first_warning(base | {'lead_days': 0, 'trailing_at_first_leading': 45.0}, ep)
    assert same and 'no advance warning' in same[0]
    late = _t_first_warning(base | {'lead_days': -40, 'trailing_at_first_leading': 40.0}, ep)
    assert late and 'did not lead' in late[0] and '40 days earlier' in late[0]


def test_neutral_notes_are_counted_but_never_raise_an_early_warning(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        j = _journey(cid, ids['neutral'])
        feb = next(m for m in j['leading_vs_trailing']['series'] if m['month'] == '2026-02-01')
        assert feb['signal_count'] == 3 and feb['neutral_count'] == 3 and feb['negative_count'] == 0
        assert feb['qual'] is None                                                  # nothing with a direction was said
        assert all(m['early_warning'] != 'early_warning' for m in j['leading_vs_trailing']['series'])
        assert j['leading_vs_trailing']['first_leading_warning_at'] is None


# ── 6: a corrected KPI value ──────────────────────────────────────────

def test_kpi_correction_replaces_the_stored_value_and_reopens_the_month(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        from utils.csv_ingest import load_kpis
        aid = ids['contracted']
        row = {'kpi_code': 'SANCT-1', 'measured_at': '2026-02-01', 'value': '40', 'target': '90', 'weight': '0.25', 'pillar': 'P1'}
        resolve = lambda r: aid                                                     # noqa: E731
        assert load_kpis([row], resolve) == (1, 0, 0)
        db.session.commit()
        first = KPIMeasurement.query.filter_by(account_id=aid, kpi_code='SANCT-1').one()
        stamped = first.created_at
        assert load_kpis([row], resolve) == (0, 0, 0)                               # the same file again changes nothing
        db.session.commit()
        db.session.refresh(first)
        assert first.created_at == stamped
        assert load_kpis([row | {'value': '88'}], resolve) == (0, 0, 1)             # the corrected number lands
        db.session.commit()
        db.session.refresh(first)
        assert float(first.value) == 88.0 and first.created_at > stamped            # a newer created_at is what reopens the month
        assert KPIMeasurement.query.filter_by(account_id=aid, kpi_code='SANCT-1').count() == 1
        assert load_kpis([row | {'value': ''}], resolve) == (0, 1, 0)               # a blank is missing, not a correction to zero
        db.session.rollback()


# ── 9: a month with no score ──────────────────────────────────────────

def test_month_without_a_health_score_does_not_break_the_journey(tenant):
    cid, ids, res = tenant
    with app.app_context():
        assert not [f for f in res['failed'] if f['account_id'] == ids['gap']]
        j = _journey(cid, ids['gap'])
        assert j['phases'] and j['lifecycle'] == {'status': 'active'}
        moves = [e['subtype'] for e in j['episodes'] if e['kind'] == 'health_transition']
        assert moves and not [m for m in moves if 'None' in m]                      # no transition into or out of 'no status'


# ── 10: a restricted user's forecast ──────────────────────────────────

def test_forecast_is_restricted_to_the_callers_accounts(tenant):
    cid, ids, _ = tenant
    with app.app_context():
        from wizards.wizard_d_foresight import run_wizard_d, get_forecast
        run_wizard_d(cid)
        whole = get_forecast(cid)
        mine = get_forecast(cid, account_ids={ids['contracted']})
        assert [r['account_id'] for r in mine['accounts']] == [ids['contracted']]
        assert mine['portfolio']['scope'] == 'restricted' and mine['portfolio']['accounts'] == 1
        assert mine['portfolio']['arr'] < whole['portfolio']['arr']
        assert get_forecast(cid, account_ids=set())['accounts'] == []


# ── a failed build is visible and does not take the others down ───────

def test_one_failing_account_does_not_stop_the_rest_and_is_reported(tenant, monkeypatch):
    cid, ids, _ = tenant
    with app.app_context():
        import journeys.wizard_a as wa
        import journeys.journey_builder as jb
        real = jb.build_journey
        bad = ids['unattended']

        def flaky(account, vertical):
            if account.account_id == bad:
                raise RuntimeError('boom')
            return real(account, vertical)
        monkeypatch.setattr(jb, 'build_journey', flaky)
        res = wa.run_wizard_a(cid, evaluate_playbooks=False)
        assert res['status'] == 'partial'
        assert [f['account_id'] for f in res['failed']] == [bad] and 'boom' in res['failed'][0]['error']
        assert res['journeys_written'] == len(ids) - 1
        stamped = _journey(cid, bad)
        assert stamped['build_error']['error'] and stamped['phases']                # the previous build is kept, and flagged
        from journeys.read import list_journeys
        assert {r['account_id']: r for r in list_journeys(cid)}[bad]['build_error']
        monkeypatch.setattr(jb, 'build_journey', real)
        again = wa.run_wizard_a(cid, evaluate_playbooks=False)
        assert again['status'] == 'completed' and 'build_error' not in _journey(cid, bad)
