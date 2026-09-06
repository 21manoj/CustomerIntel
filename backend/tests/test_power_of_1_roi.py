"""
Power-of-1 / ROI (docs/design/power-of-1-roi.md) on real Postgres tenants, two verticals:
  * every catalog vertical has a validated economics file; an unknown vertical fails closed (tool → ToolError)
  * derived Po1 on the TENANT'S revenue base and the weights actually applied — no global base anywhere
  * assumed economics are labelled per figure with their basis; derived × assumed = assumed
  * a 1 % KPI move goes through the catalog curve at the account's latest measurement (flat at the healthy max)
  * band view, scenarios (break-even lift = share / sensitivity)
  * priorities: protect vs grow lens, factors, open interventions, episode citations; the portfolio row carries the
    same number; below the floor is unlisted
  * measured impact: realized (measured, cited) vs exposure (derived) per playbook and per pillar, roles a vertical
    has no pillar for land in 'unmapped'; the ledger; hindsight no_run; sensitivity gated (and the gate opens)
  * tools keyed, routes keyed
"""
import json
import os
import re
import sys
import uuid
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
from models import Account, CustomerConfig, HealthScore, Intervention, JourneyData   # noqa: E402

SAAS_ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,renewal_date,primary_champion_name\n"
    "NOR,Northstar Mutual,Insurance,NA,1200000,{renewal},Dana Whitfield\n"
    "ORC,Orchard Retail,Retail,NA,900000,2027-03-01,Ivy Chen\n"
    "QUI,Quiet Co,Media,EU,600000,2027-01-01,\n"
)
# saas_premium: P1 adoption (DAU rate, healthy 60-95), P5 revenue (P5-KPI1). Northstar slides into at_risk; Orchard healthy; Quiet healthy.
SAAS_KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "NOR,P1-KPI1,2026-05-01,62\nNOR,P5-KPI1,2026-05-01,104\n"
    "NOR,P1-KPI1,2026-06-01,55\nNOR,P5-KPI1,2026-06-01,101\n"
    "NOR,P1-KPI1,2026-07-01,44\nNOR,P5-KPI1,2026-07-01,97\n"
    "ORC,P1-KPI1,2026-07-01,95\nORC,P5-KPI1,2026-07-01,118\n"
    "QUI,P1-KPI1,2026-07-01,80\nQUI,P5-KPI1,2026-07-01,110\n"
)
DC_ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,renewal_date\n"
    "TIT,Titan Compute,AI/ML,NA,8200000,2026-10-15\n"
    "MER,Meridian AI,AI/ML,NA,4100000,2027-04-01\n"
)
# datacenter_v1: P1-KPI1 realized $/GPU-hr (healthy 2.5-4.0), P6-KPI1 (adoption), P3-KPI1 (reliability)
DC_KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "TIT,P1-KPI1,2026-06-01,2.0\nTIT,P6-KPI1,2026-06-01,40\n"
    "TIT,P1-KPI1,2026-07-01,1.9\nTIT,P6-KPI1,2026-07-01,35\n"
    "MER,P1-KPI1,2026-07-01,4.5\nMER,P6-KPI1,2026-07-01,90\n"
)


def _sig(cid, aid, subtype, when, text):
    from mcp_server.cs_pulse_onboarding import submit_signal
    return submit_signal(cid, aid, text, source_type='crm_activity', signal_type=subtype, occurred_at=when, process_now=False)


def _tenant(name, vertical, accounts_csv, kpi_csv):
    from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
    tag = uuid.uuid4().hex[:8]
    cid = create_customer(name=f'{name} {tag}', domain=f'{name.lower()}-{tag}.test', vertical=vertical,
                          admin_email=f'{name.lower()}_{tag}@t.test', admin_name='P', data_origin='synthetic_test')['customer_id']
    upload_csv(cid, 'account_details.csv', accounts_csv)
    upload_csv(cid, 'kpi_measurements.csv', kpi_csv)
    res = process_data(cid)
    assert res['status'] == 'success', res
    ids = {a.external_account_id: a.account_id for a in Account.query.filter_by(customer_id=cid).all()}
    return cid, ids


def _rebuild(cid):
    from signal_engine.pipeline import process_pending
    from journeys.wizard_a import run_wizard_a
    process_pending(customer_id=cid, limit=100, rebuild_journeys=False)
    return run_wizard_a(cid, evaluate_playbooks=True)


@pytest.fixture(scope='module')
def tenants():
    with app.app_context():
        db.create_all()
        # the journey's as_of is the end of the last scored month (2026-07-31); renewal 20 days after it → band 0-30
        scid, sids = _tenant('Po1Saas', 'saas_premium', SAAS_ACCOUNTS.format(renewal='2026-08-20'), SAAS_KPIS)
        # Northstar: champion departure (critical) + budget pressure (high), renewal in 20 days → protect
        _sig(scid, sids['NOR'], 'champion_departure', '2026-07-20T10:00:00Z', 'Dana Whitfield is leaving at the end of the month')
        _sig(scid, sids['NOR'], 'budget_pressure', '2026-07-24T10:00:00Z', 'Procurement wants a 20% reduction at renewal')
        # Orchard: expansion interest → grow (expansion_intent_handoff proposed, automation level 0)
        _sig(scid, sids['ORC'], 'expansion_interest', '2026-07-22T10:00:00Z', 'Ops asked for 40 more seats for the new region')
        _rebuild(scid)
        # close the champion-departure intervention with a revenue outcome (measured $)
        from playbooks.governance import approve, report
        row = Intervention.query.filter_by(customer_id=scid, account_id=sids['NOR'], playbook_id='champion_departure_sponsor_rebuild').first()
        assert row is not None, [(r.account_id, r.playbook_id) for r in Intervention.query.filter_by(customer_id=scid).all()]
        approve(scid, row.id, note='go')
        report(scid, row.id, 'done', note='sponsor rebuilt', outcome_type='renewal_secured', outcome_date='2026-08-05', revenue=1200000)

        dcid, dids = _tenant('Po1Dc', 'datacenter_v1', DC_ACCOUNTS, DC_KPIS)
        # Titan: an incident (infra_incident, high) → protect; Meridian: capacity + expansion → grow
        _sig(dcid, dids['TIT'], 'critical_incident', '2026-07-18T10:00:00Z', 'Two nodes lost power; training job restarted twice')
        _sig(dcid, dids['MER'], 'capacity_warning', '2026-07-15T10:00:00Z', 'They are at 92% of the reserved cluster')
        _sig(dcid, dids['MER'], 'expansion_interest', '2026-07-21T10:00:00Z', 'Asked for pricing on 64 more H100s')
        _rebuild(dcid)
        yield {'saas': (scid, sids), 'dc': (dcid, dids)}
        db.session.remove()
        db.drop_all()


# ── config / fail-closed ──────────────────────────────────────────────

def test_every_catalog_vertical_has_validated_economics():
    from roi import settings
    from utils.vertical_registry import SUPPORTED_VERTICALS
    assert set(settings.economics_verticals()) >= SUPPORTED_VERTICALS
    for v in SUPPORTED_VERTICALS:
        e = settings.economics(v)
        assert e['basis'] == 'assumed' and e['vertical'] == v
        assert e['retention_sensitivity_per_health_point']['basis'].startswith('assumed')
        assert e['revenue_at_risk_share_by_band']['critical'] > e['revenue_at_risk_share_by_band']['at_risk'] > e['revenue_at_risk_share_by_band']['healthy']
    with pytest.raises(ValueError, match='Unknown vertical'):
        settings.economics('no_such_vertical')
    with pytest.raises(KeyError, match='no priority/nope'):
        settings.get('priority', 'nope')


def test_no_global_arr_base_anywhere():
    """The old model's `_arr_base: 10000000` and its SaaS six-metric constants must not reappear."""
    src = ''.join(p.read_text() for p in (BACKEND / 'roi').glob('*.py')) + (BACKEND / 'config' / 'power_of_1.json').read_text()
    src += ''.join(p.read_text() for p in (BACKEND / 'config' / 'economics').glob('*.json'))
    assert '_arr_base' not in src and '10000000' not in src and 'annual_impact_per_pct' not in src
    assert not re.search(r"['\"](dc2_s|saas_premium)['\"]\s*:", ''.join(p.read_text() for p in (BACKEND / 'roi').glob('*.py')))


def test_unknown_vertical_fails_closed_through_the_tool(tenants):
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_roi import get_power_of_1, get_roi, get_investment_priorities
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:6]
        cid = create_customer(name=f'Nope {tag}', domain=f'nope-{tag}.test', vertical='saas_premium', admin_email=f'n_{tag}@t.test',
                              admin_name='N', data_origin='synthetic_test')['customer_id']
        cc = CustomerConfig.query.filter_by(customer_id=cid).first()
        cc.vertical = 'no_such_vertical'; db.session.commit()
        for tool in (get_power_of_1, get_roi, get_investment_priorities):
            with pytest.raises(ToolError, match='no_such_vertical'):
                tool(cid)
        cc.vertical = None; db.session.commit()
        with pytest.raises(ToolError, match='No fallback'):
            get_power_of_1(cid)


# ── Power-of-1 ────────────────────────────────────────────────────────

def test_po1_is_derived_on_the_tenants_own_base_and_applied_weights(tenants):
    cid, ids = tenants['saas']
    with app.app_context():
        from roi.power_of_1 import power_of_1
        from roi import settings
        out = power_of_1(cid)
        assert out['status'] == 'ok' and out['vertical'] == 'saas_premium' and out['synthetic'] is True
        sens = settings.economics('saas_premium')['retention_sensitivity_per_health_point']['value']
        total = sum(float(a.revenue) for a in Account.query.filter_by(customer_id=cid).all())
        assert out['portfolio']['revenue_base'] == {'value': total, 'basis': 'derived', 'basis_chain': ['derived: Σ Account.revenue (get_account_arr)']}
        assert out['portfolio']['revenue_per_health_point']['value'] == pytest.approx(total * sens)
        nor = next(a for a in out['accounts'] if a['account_id'] == ids['NOR'])
        hs = HealthScore.query.filter_by(account_id=ids['NOR']).order_by(HealthScore.measurement_month.desc()).first()
        assert nor['weight_source'] == hs.weight_source and hs.weight_source in ('customer_config', 'catalog', 'lifecycle')
        assert nor['revenue_per_health_point']['value'] == pytest.approx(1_200_000 * sens)
        # the weights actually applied on the row, normalised, drive the per-pillar figure; they sum to one health point
        wsum = sum(float(w) for w in hs.pillar_weights.values())
        assert sum(p['health_points_per_pillar_point'] for p in nor['pillars']) == pytest.approx(1.0, abs=1e-3)
        for p in nor['pillars']:
            w = float(hs.pillar_weights[p['pillar']]) / wsum
            assert p['revenue_per_pillar_point']['value'] == pytest.approx(1_200_000 * sens * w, rel=1e-3)
            assert p['revenue_per_pillar_point']['basis'] == 'assumed'
            chain = p['revenue_per_pillar_point']['basis_chain']
            assert chain[0] == 'derived: Account.revenue' and any(c.startswith('assumed: subscription') for c in chain)
            assert p['current_score'] == hs.contributing_pillars[p['pillar']]
        assert {p['pillar'] for p in nor['pillars']} == set(hs.pillar_weights) == {'P1', 'P5'}


def test_po1_kpi_one_pct_goes_through_the_catalog_curve(tenants):
    cid, ids = tenants['dc']
    with app.app_context():
        from roi.power_of_1 import power_of_1
        from utils.generic_scorer import score_kpi
        from utils.vertical_registry import get_kpis
        out = power_of_1(cid)
        assert out['vertical'] == 'datacenter_v1' and out['economics']['file'] == 'datacenter_v1.json'
        assert out['economics']['retention_sensitivity_per_health_point']['value'] == 0.005
        names = {p['pillar']: p['name'] for p in out['portfolio']['pillars']}
        assert names['P1'] == 'Revenue & Unit Economics' and names['P6'] == 'Provisioning Velocity'
        tit = next(a for a in out['accounts'] if a['account_id'] == ids['TIT'])
        k = next(k for k in tit['kpis'] if k['kpi'] == 'P1-KPI1')
        mv = k['one_pct_value_move']
        kdef = get_kpis('datacenter_v1')['P1-KPI1']
        assert mv['value_now'] == 1.9 and mv['direction'] == 'up'
        assert mv['score_delta'] == pytest.approx(score_kpi(1.9 * 1.01, kdef) - score_kpi(1.9, kdef), abs=1e-3) and mv['score_delta'] > 0
        assert mv['revenue_delta']['value'] == pytest.approx(mv['score_delta'] * k['health_points_per_kpi_point'] * 8_200_000 * 0.005, rel=1e-3)
        assert mv['revenue_delta']['basis'] == 'assumed'
        # Meridian is above the healthy max on P1-KPI1 (4.5 > 4.0): flat on the curve, and says so
        mer = next(a for a in out['accounts'] if a['account_id'] == ids['MER'])
        flat = next(k for k in mer['kpis'] if k['kpi'] == 'P1-KPI1')['one_pct_value_move']
        assert flat['score_delta'] == 0 and flat['revenue_delta']['value'] == 0 and 'flat' in flat['revenue_delta']['note']
        agg = next(k for k in out['portfolio']['kpis'] if k['kpi'] == 'P1-KPI1')
        assert agg['measured_accounts'] == 2 and agg['one_pct_value_move_revenue']['value'] == pytest.approx(mv['revenue_delta']['value'])


def test_po1_band_view_and_scenarios(tenants):
    cid, ids = tenants['saas']
    with app.app_context():
        from roi.power_of_1 import power_of_1
        from roi import settings
        import utils.health_thresholds as ht
        econ = settings.economics('saas_premium')
        shares = econ['revenue_at_risk_share_by_band']
        out = power_of_1(cid, ids['NOR'])
        nor = out['accounts'][0]
        bv = nor['band_view']
        assert bv['band'] == ht.classify(bv['health_now'])
        assert bv['revenue_at_risk'] == {'value': pytest.approx(1_200_000 * shares[bv['band']]), 'basis': 'assumed',
                                         'basis_chain': ['derived: Account.revenue', shares['basis']]}     # the file's sentence, labelled once
        assert not any('assumed: assumed' in c for a in out['accounts'] for p in a['pillars'] for c in p['revenue_per_pillar_point']['basis_chain'])
        if bv['band'] != 'healthy':
            assert bv['next_band'] and bv['points_to_next_band'] > 0
            assert bv['revenue_protected_if_next_band']['value'] == pytest.approx(1_200_000 * (shares[bv['band']] - shares[bv['next_band']]))
            for p, pts in bv['pillar_points_to_next_band'].items():
                w = next(x['weight'] for x in nor['pillars'] if x['pillar'] == p)
                assert pts == pytest.approx(bv['points_to_next_band'] / w, rel=1e-2)
        bands = {b['band']: b for b in out['portfolio']['bands']}
        assert bands[bv['band']]['accounts'] == 1 and bands[bv['band']]['revenue_at_risk']['value'] == bv['revenue_at_risk']['value']
        sens = econ['retention_sensitivity_per_health_point']['value']
        for sc in out['portfolio']['scenarios']:
            assert sc['investment']['basis'] == 'assumed' and sc['investment']['value'] == pytest.approx(1_200_000 * sc['cs_investment_share_of_revenue'])
            assert sc['break_even_health_points'] == pytest.approx(sc['cs_investment_share_of_revenue'] / sens, rel=1e-2)
        assert [s['cs_investment_share_of_revenue'] for s in out['portfolio']['scenarios']] == settings.get('scenarios', 'cs_investment_share_of_revenue')


# ── priorities ────────────────────────────────────────────────────────

def test_priorities_rank_cite_and_carry_lenses(tenants):
    cid, ids = tenants['saas']
    with app.app_context():
        from roi.priorities import investment_priorities
        from roi import settings
        out = investment_priorities(cid)
        assert out['status'] == 'ok' and out['vertical'] == 'saas_premium'
        rows = {r['account_id']: r for r in out['rows']}
        nor, orc, qui = rows[ids['NOR']], rows[ids['ORC']], rows[ids['QUI']]
        # Northstar: protect — critical urgency on cited evidence, renewal inside 30 days, at-risk/declining health
        assert nor['lens'] == 'protect' and nor['factors']['urgency']['level'] == 'critical' and nor['factors']['renewal']['band'] == '0-30'
        w = settings.get('priority', 'weights')
        f = nor['factors']
        assert nor['risk_factor'] == pytest.approx(w['phase'] * f['phase']['factor'] + w['leading'] * f['leading']['factor']
                                                   + w['urgency'] * f['urgency']['factor'] + w['renewal'] * f['renewal']['factor'], abs=1e-3)
        assert nor['revenue_weighted'] == {'value': pytest.approx(1_200_000 * nor['priority_factor'], abs=1), 'basis': 'derived',
                                           'basis_chain': nor['revenue_weighted']['basis_chain']}
        assert nor['revenue_weighted']['basis_chain'][0] == 'derived: Account.revenue'
        j = JourneyData.query.filter_by(customer_id=cid, account_id=ids['NOR']).one().journey_json
        latest = j['leading_vs_trailing']['series'][-1]
        assert set(latest['contributing_episode_ids']) <= set(nor['cites']['episode_ids']) and nor['cites']['node_ids']
        assert nor['cites']['quote']
        # Orchard: grow — expansion_intent in the latest month and an open expansion-class proposal waiting for approval
        assert orc['lens'] == 'grow' and 'expansion_intent' in orc['opportunity']['roles']
        assert orc['open_interventions'] and orc['open_interventions'][0]['pending_approval'] is True and orc['pending_approvals'] == 1
        assert orc['open_interventions'][0]['intervention_id'] in orc['opportunity']['open_expansion_interventions']
        # Quiet: nothing signalled, healthy, renewal far away → below the floor, unlisted
        assert qui['priority_factor'] < out['list_floor'] and qui['account_id'] not in {r['account_id'] for r in out['listed']}
        assert out['listed'][0]['account_id'] == ids['NOR']
        assert [r['revenue_weighted']['value'] for r in out['rows']] == sorted((r['revenue_weighted']['value'] for r in out['rows']), reverse=True)
        pf = out['portfolio']
        assert pf['accounts'] == 3 and pf['by_lens'] == {'protect': 1, 'grow': 1} and pf['pending_approvals'] >= 1
        assert pf['revenue_total']['value'] == 2_700_000 and pf['revenue_in_protect_lens']['value'] == 1_200_000 and pf['revenue_in_grow_lens']['value'] == 900_000
        assert pf['exposure_weighted']['value'] == pytest.approx(nor['revenue_weighted']['value'])
        one = investment_priorities(cid, ids['NOR'])
        assert one['account_id'] == ids['NOR'] and len(one['rows']) == 1 and one['rows'][0]['risk_factor'] == nor['risk_factor']


def test_priorities_on_datacenter_use_its_own_taxonomy(tenants):
    cid, ids = tenants['dc']
    with app.app_context():
        from roi.priorities import investment_priorities
        rows = {r['account_id']: r for r in investment_priorities(cid)['rows']}
        tit, mer = rows[ids['TIT']], rows[ids['MER']]
        assert tit['lens'] == 'protect' and tit['factors']['urgency']['level'] in ('high', 'critical')
        assert mer['lens'] == 'grow' and set(mer['opportunity']['roles']) >= {'expansion_intent'}


def test_portfolio_row_carries_the_same_priority_number(tenants):
    cid, ids = tenants['saas']
    with app.app_context():
        from journeys.read import list_journeys
        from roi.priorities import investment_priorities, compact
        rows = {r['account_id']: r for r in investment_priorities(cid)['rows']}
        for row in list_journeys(cid):
            assert row['priority'] == compact(rows[row['account_id']])
            assert row['priority']['basis'] == 'derived'


# ── measured impact ───────────────────────────────────────────────────

def test_roi_realized_vs_exposure_per_playbook_and_pillar(tenants):
    cid, ids = tenants['saas']
    with app.app_context():
        from roi.measured import roi
        out = roi(cid)
        assert out['vertical'] == 'saas_premium' and out['revenue_base']['value'] == 2_700_000 and out['revenue_base']['basis'] == 'derived'
        pb = {p['playbook_id']: p for p in out['by_playbook']}
        cd = pb['champion_departure_sponsor_rebuild']
        row = Intervention.query.filter_by(customer_id=cid, playbook_id='champion_departure_sponsor_rebuild').one()
        assert cd['closed_done'] == 1 and cd['intervention_ids'] == [row.id] and cd['outcome_node_ids'] == [row.outcome_node_id]
        assert cd['realized_revenue'] == {'value': 1_200_000.0, 'basis': 'measured', 'basis_chain': [f'measured: outcome nodes [{row.outcome_node_id}]']}
        assert cd['exposure_revenue'] == {'value': 1_200_000.0, 'basis': 'derived', 'basis_chain': ['derived: account revenue on the intervention rows']}
        assert 'never summed' in cd['note']
        eh = pb['expansion_intent_handoff']
        assert eh['realized_revenue']['value'] is None and eh['realized_revenue']['basis'] == 'measured' and eh['exposure_revenue']['value'] == 900_000.0
        # per pillar: champion_change → engagement → P2 (saas_premium); expansion_intent → expansion → P5
        pp = {p['pillar']: p for p in out['by_pillar']}
        assert pp['P2']['roles'] == ['champion_change'] and pp['P2']['realized_revenue']['value'] == 1_200_000.0 and pp['P2']['name'] == 'Customer Engagement'
        assert pp['P5']['roles'] == ['expansion_intent'] and pp['P5']['realized_revenue']['value'] is None and pp['P5']['exposure_revenue']['value'] == 900_000.0
        assert 'unmapped' not in pp and all('do not sum pillars' in p['note'] for p in out['by_pillar'])
        # ledger: the renewal_secured outcome is 'protected', with revenue, linked to the intervention
        led = {b['bucket']: b for b in out['ledger']['by_bucket']}
        assert led['protected']['outcomes'] == 1 and led['protected']['linked_to_interventions'] == 1
        assert led['protected']['revenue']['value'] == 1_200_000.0 and led['protected']['linked_revenue']['value'] == 1_200_000.0
        assert led['protected']['node_ids'] == [row.outcome_node_id]
        assert led['lost']['outcomes'] == 0 and led['lost']['revenue']['value'] is None
        assert out['hindsight']['status'] == 'no_run'
        s = out['sensitivity']
        assert s['status'] == 'insufficient_data' and s['qualifying_interventions'] <= 1 and s['measured_revenue_per_health_point']['value'] is None
        assert s['assumed_revenue_share_per_health_point']['value'] == 0.004 and s['assumed_revenue_share_per_health_point']['basis'].startswith('assumed')
        assert 'of 5' in s['measured_revenue_per_health_point']['note']


def test_roles_without_a_pillar_in_the_vertical_land_in_unmapped():
    from roi.measured import _pillars_for_roles
    # datacenter_v1 has no 'engagement' pillar; dc2_s has no 'revenue' pillar
    assert _pillars_for_roles('datacenter_v1', ['champion_change', 'infra_incident']) == {'unmapped': ['champion_change'], 'P3': ['infra_incident']}
    assert _pillars_for_roles('dc2_s', ['commercial_pressure', 'capacity_pressure']) == {'unmapped': ['commercial_pressure'], 'P3': ['capacity_pressure']}
    assert _pillars_for_roles('saas_premium', ['routine']) == {'unmapped': ['routine']}


def test_measured_sensitivity_gate_opens_at_the_minimum(tenants):
    """The gate must be shown to open (guard-never-fires class): five closed interventions with a lift and a
    positive revenue outcome on the journeys' hooks → a measured $ per health point, cited."""
    cid, ids = tenants['saas']
    with app.app_context():
        from roi.measured import _sensitivity
        from roi import settings
        econ = settings.economics('saas_premium')
        need = settings.get('measured', 'min_interventions_for_sensitivity')
        jd = JourneyData.query.filter_by(customer_id=cid, account_id=ids['QUI']).one()
        j = dict(jd.journey_json)
        hooks, views = [], []
        for i in range(need):
            nid, onid = 900_000 + i, 950_000 + i
            hooks.append({'episode_id': f'int:{nid}', 'date': '2026-03-01T00:00:00', 'title': f'i{i}',
                          'health_before': {'n': 1, 'mean': 40.0, 'last': 40.0}, 'health_after': {'n': 1, 'mean': 50.0, 'last': 50.0},
                          'outcomes_after': [{'episode_id': f'out:{onid}', 'bucket': 'protected', 'revenue': 20_000.0}]})
            views.append({'intervention_id': 5000 + i, 'state': 'closed', 'closed_state': 'done', 'node_id': nid,
                          'outcome': {'node_id': onid, 'revenue': 20_000.0, 'outcome_type': 'renewal_secured'}})
        j['counterfactual_hooks'] = list(j.get('counterfactual_hooks') or []) + hooks
        jd.journey_json = j; db.session.commit()
        try:
            s = _sensitivity(cid, views, econ)
            assert s['status'] == 'ok' and s['qualifying_interventions'] == need
            assert s['measured_revenue_per_health_point'] == {'value': pytest.approx(20_000.0 * need / (10.0 * need)), 'basis': 'measured',
                                                              'basis_chain': s['measured_revenue_per_health_point']['basis_chain']}
            assert str(950_000) in s['measured_revenue_per_health_point']['basis_chain'][0] and '5000' in s['measured_revenue_per_health_point']['basis_chain'][0]
            assert s['assumed_revenue_share_per_health_point']['value'] == econ['retention_sensitivity_per_health_point']['value']   # still there, separate
            # one short of the minimum: no number
            s2 = _sensitivity(cid, views[:-1], econ)
            assert s2['status'] == 'insufficient_data' and s2['measured_revenue_per_health_point']['value'] is None
        finally:
            j['counterfactual_hooks'] = [h for h in j['counterfactual_hooks'] if not str(h['episode_id']).startswith('int:9')]
            jd.journey_json = j; db.session.commit()


# ── tools + routes ────────────────────────────────────────────────────

def test_tools_are_keyed_reads_and_round_trip(tenants):
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    for t in ('get_investment_priorities', 'get_power_of_1', 'get_roi'):
        assert t in KEYED_TOOLS and t not in ONBOARDING_TOOLS and t not in WRITE_TOOLS
    src = (BACKEND / 'mcp_server' / 'cs_pulse_roi.py').read_text()
    assert set(re.findall(r"_require_auth_if_key_present\('([a-z_0-9]+)'", src)) == {'get_investment_priorities', 'get_power_of_1', 'get_roi'}
    cid, ids = tenants['saas']
    with app.app_context():
        from mcp_server.cs_pulse_roi import get_investment_priorities, get_power_of_1, get_roi
        from fastmcp.exceptions import ToolError
        assert get_investment_priorities(cid)['portfolio']['accounts'] == 3
        assert get_power_of_1(cid, ids['NOR'])['accounts'][0]['account_name'] == 'Northstar Mutual'
        assert get_roi(cid)['interventions']['source'] == 'list_interventions'
        with pytest.raises(ToolError, match='not found'):
            get_roi(cid + 100_000)


def test_http_routes_are_keyed(tenants):
    cid, ids = tenants['saas']
    key = 'srv-' + uuid.uuid4().hex
    os.environ['MCP_SERVER_API_KEY'] = key
    os.environ['MCP_AUTH_REQUIRED'] = 'true'
    import mcp_server.auth as auth
    prev = auth.MCP_SERVER_API_KEY
    auth.MCP_SERVER_API_KEY = key
    try:
        from server import build_asgi_app
        from roi.http import ROUTES
        from starlette.testclient import TestClient
        assert ROUTES == ('/api/roi/priorities', '/api/roi/power-of-1', '/api/roi')
        with TestClient(build_asgi_app(TEST_DB, create_schema=False)) as c:
            for path in ROUTES:
                assert c.get(f'{path}?customer_id={cid}').status_code == 401
            h = {'Authorization': f'Bearer {key}'}
            assert c.get('/api/roi/priorities', headers=h).status_code == 400
            r = c.get(f'/api/roi/priorities?customer_id={cid}&account_id={ids["NOR"]}', headers=h)
            assert r.status_code == 200 and r.json()['rows'][0]['lens'] == 'protect'
            r = c.get(f'/api/roi/power-of-1?customer_id={cid}', headers=h)
            assert r.status_code == 200 and r.json()['portfolio']['revenue_base']['value'] == 2_700_000
            r = c.get(f'/api/roi?customer_id={cid}', headers=h)
            assert r.status_code == 200 and r.json()['by_playbook']
    finally:
        auth.MCP_SERVER_API_KEY = prev
        os.environ['MCP_TRANSPORT'] = 'stdio'
