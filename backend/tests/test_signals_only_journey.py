"""
Wizard A for a signals-only account (no KPI layer), derived from data — no flags:
  * data_coverage says which layer exists and why (not_yet vs none, installed-base contract)
  * phases come from evidence when nothing is scored
  * the health predicates in the arc rules use their evidence equivalents (evidence_scope = evidence_only)
  * quiet + no negative phase = steady, not 'no health scores'
  * the narrative says the account is read from evidence; the backtest runs on live months
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
from models import Account, JourneyData                   # noqa: E402


def _sig(cid, aid, subtype, when, text, **kw):
    from mcp_server.cs_pulse_onboarding import submit_signal
    return submit_signal(cid, aid, text, source_type='crm_activity', signal_type=subtype, occurred_at=when, **kw)


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'SigOnly {tag}', domain=f'sigonly-{tag}.test', vertical='saas_premium',
                              admin_email=f'so_{tag}@t.test', admin_name='S', data_origin='synthetic_test')['customer_id']
        mk = lambda name, ext, pm: Account(customer_id=cid, account_name=name, revenue=500_000, vertical='saas_premium',
                                           external_account_id=ext, profile_metadata=pm)
        churn = mk('Halcyon Health', 'HAL', {'primary_champion_name': 'Nadia Bell', 'renewal_date': '2026-08-01'})
        grow = mk('Orchard Retail', 'ORC', {'primary_champion_name': 'Ivy Chen', 'renewal_date': '2026-10-01'})
        quiet = mk('Quiet Co', 'QUI', {'renewal_date': '2026-12-01'})
        fresh = mk('Fresh Co', 'FRE', {'renewal_date': '2026-12-01'})
        hw = mk('Ironworks', 'IRO', {'contract_type': 'hardware', 'refresh_date': '2027-03-01'})
        db.session.add_all([churn, grow, quiet, fresh, hw]); db.session.commit()
        ids = {a.external_account_id: a.account_id for a in (churn, grow, quiet, fresh, hw)}
    # a churn story: negative → intervention → recovery, over four months
    _sig(cid, ids['HAL'], 'champion_departure', '2026-01-20T10:00:00Z', 'Nadia Bell left for a regional health system')
    _sig(cid, ids['HAL'], 'engagement_gap', '2026-02-10T10:00:00Z', 'Two QBR invitations declined by the interim owner')
    _sig(cid, ids['HAL'], 'csm_intervention', '2026-03-05T10:00:00Z', 'Exec sponsor rebuild kicked off; new owner named')
    _sig(cid, ids['HAL'], 'kpi_stabilized', '2026-04-08T10:00:00Z', 'Weekly usage steady three weeks running')
    # an expansion story: positive only
    _sig(cid, ids['ORC'], 'expansion_discussion', '2026-02-03T10:00:00Z', 'Asked what 150 more seats would cost')
    _sig(cid, ids['ORC'], 'champion_advocacy', '2026-02-25T10:00:00Z', 'Ivy presented us at their ops all-hands')
    _sig(cid, ids['ORC'], 'expansion_signal', '2026-03-18T10:00:00Z', 'Procurement requested a co-term quote')
    # a quiet account with routine only, over three months
    _sig(cid, ids['QUI'], 'routine_review', '2026-01-15T10:00:00Z', 'Monthly check-in, nothing to report')
    _sig(cid, ids['QUI'], 'routine_review', '2026-03-15T10:00:00Z', 'Monthly check-in, nothing to report either')
    # a fresh account: one signal, a few days old
    _sig(cid, ids['FRE'], 'routine_review', datetime.utcnow().strftime('%Y-%m-%dT10:00:00Z'), 'Kickoff call held')
    # installed base: one signal
    _sig(cid, ids['IRO'], 'routine_review', '2026-03-01T10:00:00Z', 'Maintenance visit completed')
    yield cid, ids
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _journey(cid, aid):
    with app.app_context():
        return JourneyData.query.filter_by(customer_id=cid, account_id=aid).one().journey_json


def test_coverage_is_derived_and_says_why(tenant):
    cid, ids = tenant
    hal = _journey(cid, ids['HAL'])['data_coverage']
    assert hal['kpi_layer'] == 'none' and hal['months_scored'] == 0 and hal['evidence_count'] == 4 and hal['contract_shape'] == 'subscription'
    assert 'no KPI in' in hal['basis'] and hal['thresholds']['no_kpi_layer_after_days'] == 45
    fre = _journey(cid, ids['FRE'])['data_coverage']
    assert fre['kpi_layer'] == 'not_yet' and 'no KPI yet' in fre['basis']
    iro = _journey(cid, ids['IRO'])['data_coverage']
    assert iro['kpi_layer'] == 'none' and iro['contract_shape'] == 'installed_base' and 'installed-base' in iro['basis']


def test_phases_come_from_evidence_when_nothing_is_scored(tenant):
    cid, ids = tenant
    j = _journey(cid, ids['HAL'])
    assert j['phases_basis'] == 'evidence' and j['summary']['months_scored'] == 0
    names = [p['name'] for p in j['phases']]
    assert names == ['deterioration', 'intervention', 'resolution'], names
    assert all(p['health_start'] is None and p['basis'] == 'evidence' for p in j['phases'])
    assert j['phases'][0]['trigger_episode_id'] and j['phases'][0]['negative_signals'] == 2
    assert j['current_phase'] == 'resolution'


def test_arcs_fire_on_evidence_equivalents(tenant):
    cid, ids = tenant
    hal = _journey(cid, ids['HAL'])['arc']
    assert hal['arc_type'] == 'exec_sponsor_change' and hal['evidence_scope'] == 'evidence_only'
    orc = _journey(cid, ids['ORC'])['arc']
    assert orc['arc_type'] == 'expansion_champion' and orc['evidence_scope'] == 'evidence_only', orc
    assert orc['confidence_semantics'] == 'rule_match_constant'


def test_quiet_evidence_only_account_is_steady_not_no_health(tenant):
    cid, ids = tenant
    j = _journey(cid, ids['QUI'])
    assert j['state'] == 'steady' and 'no KPI layer' in j['arc']['reason'] and j['arc']['evidence_scope'] == 'evidence_only'


def test_narrative_and_portfolio_say_evidence_only(tenant):
    cid, ids = tenant
    from mcp_server.cs_pulse_onboarding import get_journey, list_journeys
    j = get_journey(cid, ids['HAL'])
    text = ' '.join(s['text'] for ch in j['narrative']['chapters'] for s in ch['sentences'])
    assert 'no KPI layer' in text and 'read from evidence' in text
    assert not any(ch['phase'] == 'live' for ch in j['narrative']['chapters'])       # evidence phases are the chapters
    assert [ch['phase'] for ch in j['narrative']['chapters']] == ['deterioration', 'intervention', 'resolution']
    row = next(r for r in list_journeys(cid)['journeys'] if r['account_id'] == ids['HAL'])
    assert row['data_coverage']['kpi_layer'] == 'none' and row['phases_basis'] == 'evidence' and row['arc_type'] == 'exec_sponsor_change'


def test_backtest_runs_on_live_months(tenant):
    cid, ids = tenant
    from evals.lead_time_backtest import run_backtest
    with app.app_context():
        rep = run_backtest(cid, min_events=1)
    assert rep['journeys'] == 5 and rep['evidence_label'] != 'measured'      # ran on live months, no trailing layer, no crash
    assert isinstance(rep['results'], (dict, list))
