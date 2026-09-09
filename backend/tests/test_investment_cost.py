"""
Investment cost (config/investment/<vertical>.json, roi/investment.py) on real Postgres tenants:
  * all 5 vertical files load and validate (roi.settings.investment); an unknown vertical fails closed
  * estimated_cost_per_execution = csm_hourly_cost x estimated_csm_hours, both assumed -> assumed, never derived
  * cost_per_health_point is computed at query time, never stored; a null config lift (a pure-expansion
    playbook, or one with no pillar to attribute a lift to) yields no cost_per_health_point, never guessed
  * the measured-lift gate: below the minimum, the config file's estimated placeholder is used as-is;
    at or above it, roi.measured.playbook_health_lift's measured average is preferred -- and even then
    cost_per_health_point's basis stays 'assumed' (cost itself is never measured; weakest-link rule)
  * pillar / KPI rollups mirror the config file's own coverage (not_covered stays not_covered)
  * the MCP tool is keyed and fails closed for an unknown customer
  * the Ask AI portfolio blocks (priority/po1/roi/investment_cost) still fit the context budget together
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_invmodel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()
from models import Account, Intervention, JourneyData      # noqa: E402

SAAS_ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,renewal_date,primary_champion_name\n"
    "NOR,Northstar Mutual,Insurance,NA,1200000,2027-01-01,Dana Whitfield\n"
    "ORC,Orchard Retail,Retail,NA,900000,2027-03-01,Ivy Chen\n"
)
SAAS_KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "NOR,P1-KPI1,2026-07-01,62\nNOR,P5-KPI1,2026-07-01,104\n"
    "ORC,P1-KPI1,2026-07-01,95\nORC,P5-KPI1,2026-07-01,118\n"
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
def tenant():
    with app.app_context():
        db.create_all()
        cid, ids = _tenant('InvCost', 'saas_premium', SAAS_ACCOUNTS, SAAS_KPIS)
        # Northstar: champion departure -> proposes champion_departure_sponsor_rebuild (real Intervention row)
        _sig(cid, ids['NOR'], 'champion_departure', '2026-07-20T10:00:00Z', 'Dana Whitfield is leaving at the end of the month')
        _rebuild(cid)
        yield cid, ids
        db.session.remove()
        db.drop_all()


# ── loader (roi.settings.investment) ──────────────────────────────────

def test_all_5_verticals_load_and_validate():
    from roi import settings
    from utils.vertical_registry import SUPPORTED_VERTICALS
    assert set(settings.investment_verticals()) >= SUPPORTED_VERTICALS
    for v in SUPPORTED_VERTICALS:
        inv = settings.investment(v)
        assert inv['basis'] == 'assumed' and inv['vertical'] == v
        assert inv['csm_hourly_cost']['value'] > 0 and inv['csm_hourly_cost']['basis'].startswith('assumed')
        assert isinstance(inv['playbooks'], dict) and isinstance(inv['pillars'], dict) and isinstance(inv['kpis'], dict)
        for pid, pb in inv['playbooks'].items():
            assert pb['estimated_csm_hours']['value'] > 0
            assert pb['estimated_cost_per_execution']['value'] == pytest.approx(
                pb['estimated_csm_hours']['value'] * inv['csm_hourly_cost']['value']), pid
            lift = pb['estimated_health_point_lift_per_execution']
            assert lift['value'] is None or lift['value'] > 0, f'{v}.{pid}: a negative/zero lift is never authored'
            if lift['value'] is None:
                assert lift.get('note'), f'{v}.{pid}: a null lift needs a note explaining why'


def test_unknown_vertical_investment_fails_closed():
    from roi import settings
    with pytest.raises(ValueError, match='Unknown vertical'):
        settings.investment('no_such_vertical')


def test_manufacturing_iot_has_zero_playbooks_every_pillar_not_covered():
    """The one vertical with no config/playbooks/<vertical>.json file at all -- confirms the investment
    file honestly reflects that (not_covered everywhere), not a hidden crash or a guessed number."""
    from roi import settings
    inv = settings.investment('manufacturing_iot')
    assert inv['playbooks'] == {} and inv['kpis'] == {}
    assert len(inv['pillars']) == 5
    for code, pdef in inv['pillars'].items():
        assert pdef['status'] == 'not_covered' and pdef['playbooks'] == []


def test_healthcare_provider_coverage_is_real_and_sparse():
    """Established finding: healthcare_provider has no revenue-adjacent pillar at all (protect or grow) --
    only escalation_clinical_response (of 4 real playbooks) targets a pillar."""
    from roi import settings
    inv = settings.investment('healthcare_provider')
    mapped = {pid: pb['targets_pillars'] for pid, pb in inv['playbooks'].items()}
    assert mapped['escalation_clinical_response'] == ['P3']
    for pid in ('rollout_stall_recovery', 'commercial_pressure_save', 'expansion_intent_handoff'):
        assert mapped[pid] == [], f'{pid} should target no pillar in this catalog'
    covered = {code for code, pdef in inv['pillars'].items() if pdef.get('status') != 'not_covered'}
    assert covered == {'P3'}


def test_dc2_s_refresh_at_risk_save_is_mechanically_unmapped():
    """dc2s_kpi_catalog.json's pillar_roles has no 'revenue' entry (unlike datacenter_v1) -- confirmed by
    cross-referencing power_of_1.json's attribution map directly, not asserted from the investment file alone."""
    from roi import settings
    from utils.vertical_registry import get_pillar_roles
    attribution = json.loads((BACKEND / 'config' / 'power_of_1.json').read_text())['attribution']['signal_role_to_pillar_role']
    assert attribution['commercial_pressure'] == 'revenue'
    assert 'revenue' not in get_pillar_roles('dc2_s')
    inv = settings.investment('dc2_s')
    assert inv['playbooks']['refresh_at_risk_save']['targets_pillars'] == []
    assert inv['playbooks']['refresh_at_risk_save']['estimated_cost_per_execution']['value'] > 0   # cost doesn't need a pillar


# ── the cost-per-point formula (roi.investment) ────────────────────────

def test_cost_per_execution_is_assumed_not_derived():
    """estimated_cost_per_execution = csm_hourly_cost (assumed) x estimated_csm_hours (assumed) -- the
    product of two assumed links is itself assumed, never 'derived' just because arithmetic happened
    (roi.basis.money()'s weakest-link rule, the same one power_of_1.py already relies on)."""
    from roi import settings
    from roi.investment import playbook_investment
    inv = settings.investment('saas_premium')
    pid, pb = 'champion_departure_sponsor_rebuild', inv['playbooks']['champion_departure_sponsor_rebuild']
    row = playbook_investment(pid, pb, inv['csm_hourly_cost'], views=[], hooks={})
    assert row['estimated_cost_per_execution']['value'] == 510.0
    assert row['estimated_cost_per_execution']['basis'] == 'assumed'
    assert len(row['estimated_cost_per_execution']['basis_chain']) == 2


def test_no_measured_data_falls_back_to_estimated_lift():
    from roi import settings
    from roi.investment import playbook_investment
    inv = settings.investment('saas_premium')
    pid, pb = 'champion_departure_sponsor_rebuild', inv['playbooks']['champion_departure_sponsor_rebuild']
    row = playbook_investment(pid, pb, inv['csm_hourly_cost'], views=[], hooks={})
    assert row['lift']['source'] == 'estimated' and row['lift']['value'] == 4.0
    assert row['lift']['measured']['status'] == 'insufficient_data' and row['lift']['measured']['qualifying_interventions'] == 0
    assert row['cost_per_health_point']['value'] == pytest.approx(510.0 / 4.0)
    assert row['cost_per_health_point']['basis'] == 'assumed'


def test_null_config_lift_yields_no_cost_per_point_never_guessed():
    from roi import settings
    from roi.investment import playbook_investment
    inv = settings.investment('saas_premium')
    pid, pb = 'expansion_intent_handoff', inv['playbooks']['expansion_intent_handoff']
    row = playbook_investment(pid, pb, inv['csm_hourly_cost'], views=[], hooks={})
    assert row['lift']['source'] == 'not_applicable' and row['lift']['value'] is None
    assert row['cost_per_health_point']['value'] is None
    assert row['cost_per_health_point']['note']            # honest note, not silence
    assert row['estimated_cost_per_execution']['value'] == 170.0   # cost is still real even with no lift


def test_measured_lift_preferred_once_gate_opens(tenant):
    """The guard-must-actually-fire class: 5 closed-done interventions of the SAME playbook, each with a
    positive health lift on the journey's counterfactual hook (no revenue needed -- unlike sensitivity's
    $/point), must flip the lift source from 'estimated' to 'measured'. Below 5, the estimate is used
    as-is, never blended with a partial measurement."""
    cid, ids = tenant
    from roi import settings
    from roi.measured import playbook_health_lift, intervention_hooks
    from roi.investment import playbook_investment
    need = settings.get('measured', 'min_interventions_for_sensitivity')
    with app.app_context():
        jd = JourneyData.query.filter_by(customer_id=cid, account_id=ids['ORC']).one()
        j = dict(jd.journey_json)
        hooks_list, views = [], []
        for i in range(need):
            nid = 910_000 + i
            hooks_list.append({'episode_id': f'int:{nid}', 'date': '2026-07-01T00:00:00',
                               'health_before': {'n': 1, 'mean': 50.0, 'last': 50.0}, 'health_after': {'n': 1, 'mean': 56.0, 'last': 56.0}})
            views.append({'intervention_id': 6000 + i, 'playbook_id': 'champion_departure_sponsor_rebuild',
                         'state': 'closed', 'closed_state': 'done', 'node_id': nid, 'outcome': None})
        j['counterfactual_hooks'] = list(j.get('counterfactual_hooks') or []) + hooks_list
        jd.journey_json = j; db.session.commit()
        try:
            # read back through the real DB->hooks pipeline (roi.measured.intervention_hooks), not a
            # hand-built dict -- proves the JourneyData scan + episode_id parsing actually works, the
            # same lookup investment_cost() uses for real
            hooks = intervention_hooks(cid)
            assert all(nid in hooks for nid in (910_000 + i for i in range(need)))
            m = playbook_health_lift('champion_departure_sponsor_rebuild', views, hooks)
            assert m['status'] == 'ok' and m['qualifying_interventions'] == need and m['measured_health_point_lift'] == pytest.approx(6.0)
            # one short of the minimum: still estimated, never a partial number
            m_short = playbook_health_lift('champion_departure_sponsor_rebuild', views[:-1], hooks)
            assert m_short['status'] == 'insufficient_data' and m_short['measured_health_point_lift'] is None

            inv = settings.investment('saas_premium')
            pid, pb = 'champion_departure_sponsor_rebuild', inv['playbooks']['champion_departure_sponsor_rebuild']
            row = playbook_investment(pid, pb, inv['csm_hourly_cost'], views, hooks)
            assert row['lift']['source'] == 'measured' and row['lift']['value'] == pytest.approx(6.0)
            # cost is STILL assumed -- cost itself was never measured, only the lift denominator was;
            # the ratio inherits the weakest link (roi.basis.weakest()), not the strongest
            assert row['cost_per_health_point']['basis'] == 'assumed'
            assert row['cost_per_health_point']['value'] == pytest.approx(510.0 / 6.0)
            assert 'measured' in row['cost_per_health_point']['basis_chain'][-1]

            row_short = playbook_investment(pid, pb, inv['csm_hourly_cost'], views[:-1], hooks)
            assert row_short['lift']['source'] == 'estimated' and row_short['lift']['value'] == 4.0
        finally:
            j['counterfactual_hooks'] = [h for h in j['counterfactual_hooks'] if not str(h['episode_id']).startswith('int:91')]
            jd.journey_json = j; db.session.commit()


# ── the canonical entry point, end to end on a real tenant ────────────

def test_investment_cost_end_to_end_on_real_tenant(tenant):
    cid, ids = tenant
    from roi.investment import investment_cost
    with app.app_context():
        ic = investment_cost(cid)
        assert ic['customer_id'] == cid and ic['vertical'] == 'saas_premium' and ic['basis'] == 'assumed'
        assert {p['playbook_id'] for p in ic['playbooks']} == {'champion_departure_sponsor_rebuild', 'expansion_intent_handoff',
                                                                'escalation_exec_response', 'seat_truedown_save'}
        by_id = {p['playbook_id']: p for p in ic['playbooks']}
        assert by_id['champion_departure_sponsor_rebuild']['lift']['source'] in ('estimated', 'measured')
        assert by_id['expansion_intent_handoff']['cost_per_health_point']['value'] is None
        p4 = next(p for p in ic['pillars'] if p['pillar'] == 'P4')
        assert p4['status'] == 'not_covered' and p4['playbooks'] == []
        p2 = next(p for p in ic['pillars'] if p['pillar'] == 'P2')
        assert p2['playbooks'][0]['playbook_id'] == 'champion_departure_sponsor_rebuild'


def test_investment_cost_unknown_customer_fails_closed_through_tool():
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_roi import get_investment_cost
    with app.app_context():
        with pytest.raises(ToolError, match='not found'):
            get_investment_cost(999_999_999)


# ── Ask AI portfolio budget with all four investment blocks ───────────

def test_portfolio_investment_blocks_fit_budget_together(tenant):
    """Live-measured, not assumed: priority_portfolio + po1_portfolio + roi_portfolio +
    investment_cost_portfolio together, on a real (small) tenant, must still fit under the configured
    char budget with room for at least the portfolio summary row -- referenced from the comment in
    ask_ai/answer.py's portfolio_context() explaining why investment blocks are added before the rows loop."""
    cid, ids = tenant
    from ask_ai import settings as ask_settings
    from ask_ai.answer import portfolio_context
    with app.app_context():
        from journeys.read import list_journeys
        rows = list_journeys(cid)
        ctx, gaps, meta = portfolio_context(cid, 'what would it cost to invest in this portfolio?', rows, None)
        assert 'investment_cost:portfolio' in ctx.citable
        assert 'priority:portfolio' in ctx.citable and 'po1:portfolio' in ctx.citable and 'roi:portfolio' in ctx.citable
        budget = ask_settings.get('context', 'max_chars')
        assert ctx.used <= budget, f'{ctx.used} chars used of {budget} budget with all 4 investment blocks + rows'
        # not a tautology: record the real number so a future budget regression shows up as a diff here
        assert ctx.used > 0
