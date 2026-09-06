"""
Wizard D — Foresight (docs/design/wizard-d-foresight.md), on a real Postgres tenant:
  * prior basis below the label minimum: every block says 'prior' with the counts, a template range, the drivers applied
  * calibrated basis once a tenant has enough labelled decisions (both classes): Beta posterior moves with the stratum's
    own counts, point-in-time strata, decisions grouped within the window, pooled fallback below min_per_stratum
  * dollars are monotone in the probabilities; the portfolio range is propagated (independent narrower than correlated)
  * vertical-agnostic: the two tenants are on different verticals, and the module names none
  * journey embedding: journey_json['forecast'], the cited narrative sentence, the portfolio row, staleness after new evidence
  * tool + route are keyed; trigger_wizard 'd' writes a WizardRun; the process_data step reports counts
  * config: the loader raises on a missing key; no bare number in the module
"""
import os
import re
import sys
import uuid
from datetime import date, datetime
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
import utils.health_thresholds as ht                      # noqa: E402
from models import Account, HealthScore, ContextNode, JourneyData, ForecastRun, AccountForecast, WizardRun   # noqa: E402
from wizards import wizard_d_settings as settings         # noqa: E402

MONTHS = [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]


def _account(cid, name, vertical, series, signals=(), outcomes=(), profile=None, revenue=1_000_000):
    a = Account(customer_id=cid, account_name=name, revenue=revenue, vertical=vertical, profile_metadata=profile or None)
    db.session.add(a)
    db.session.flush()
    for m, s in zip(MONTHS, series):
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s, kpi_only_score=s, health_status=ht.classify(s)))
    for i, (dt, sub, sent) in enumerate(signals):
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='SIGNAL', node_subtype=sub, source='observed',
                                   title=f'{sub} ({name})', tier=2, occurred_at=dt, properties={'sentiment_score': str(sent)},
                                   source_platform='csv_import', source_event_id=f'{name}_{i}'))
    for i, (dt, sub, rev) in enumerate(outcomes):
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='OUTCOME', node_subtype=sub, source='observed',
                                   title=f'{sub} ({name})', tier=1, occurred_at=dt, revenue_impact=rev, revenue_impact_type=sub,
                                   properties={}, source_platform='csv_import', source_event_id=f'{name}_out_{i}'))
    return a.account_id


def _create(vertical, tag):
    from mcp_server.cs_pulse_onboarding import create_customer
    return create_customer(name=f'Foresight {tag}', domain=f'fs-{tag}.test', vertical=vertical, admin_email=f'fs_{tag}@t.test',
                           admin_name='F', data_origin='synthetic_test')['customer_id']


@pytest.fixture(scope='module')
def prior_tenant():
    """saas_premium, six archetypes, two labelled decisions — far below the minimum."""
    with app.app_context():
        db.create_all()
        cid = _create('saas_premium', uuid.uuid4().hex[:8])
        ids = {
            'champion_lost': _account(cid, 'Champion Lost', 'saas_premium', [82, 80, 76, 68, 61, 55],
                                      [(datetime(2026, 1, 10), 'champion_departure', -0.7), (datetime(2026, 2, 5), 'engagement_decline', -0.5)],
                                      profile={'renewal_date': '2026-06-15'}, revenue=1_200_000),
            'steady': _account(cid, 'Steady Eddie', 'saas_premium', [84, 85, 84, 86, 85, 86],
                               [(datetime(2026, 2, 3), 'routine_review', 0.2)], profile={'renewal_date': '2027-08-01'}, revenue=900_000),
            'growth': _account(cid, 'Growth Co', 'saas_premium', [84, 85, 86, 87, 88, 89],
                               [(datetime(2026, 3, 9), 'expansion_interest', 0.8), (datetime(2026, 4, 2), 'advocacy', 0.8)],
                               profile={'renewal_date': '2026-09-01'}, revenue=2_000_000),
            'crisis': _account(cid, 'Infra Crisis', 'saas_premium', [80, 78, 45, 40, 52, 60],
                               [(datetime(2026, 1, 5), 'system_outage', -0.8), (datetime(2026, 1, 12), 'support_escalation', -0.7)],
                               [(datetime(2026, 3, 15), 'churn_averted', 500_000.0)], profile={'renewal_date': '2026-10-01'}),
            'no_renewal': _account(cid, 'No Renewal Co', 'saas_premium', [75, 75, 75, 75, 75, 75], revenue=300_000),
            'lost': _account(cid, 'Lost Co', 'saas_premium', [70, 66, 60, 55, 50, 49],
                             [(datetime(2026, 1, 20), 'usage_decline', -0.6)],
                             [(datetime(2026, 3, 5), 'contraction', -200_000.0)], profile={'renewal_date': '2026-03-01'}, revenue=800_000),
        }
        db.session.commit()
        from journeys.wizard_a import run_wizard_a
        run_wizard_a(cid, evaluate_playbooks=False)
        yield cid, ids
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope='module')
def calibrated_tenant():
    """datacenter_v1, twelve accounts, 36 labelled decisions (24 retained / 12 not) — above the minimum."""
    with app.app_context():
        db.create_all()
        cid = _create('datacenter_v1', uuid.uuid4().hex[:8])
        declining, healthy = [], []
        for i in range(6):
            declining.append(_account(cid, f'Slide {i}', 'datacenter_v1', [82, 76, 68, 60, 55, 52], outcomes=[
                (datetime(2025, 12, 10), 'renewal_secured', 100_000.0),
                (datetime(2026, 2, 10), 'contraction', -150_000.0),
                (datetime(2026, 4, 10), 'contraction', -100_000.0),
            ], profile={'renewal_date': '2026-07-01'}, revenue=2_000_000))
            healthy.append(_account(cid, f'Solid {i}', 'datacenter_v1', [84, 85, 84, 86, 85, 86], outcomes=[
                (datetime(2025, 12, 10), 'renewal_secured', 120_000.0),
                (datetime(2026, 2, 10), 'expansion_closed', 200_000.0),
                (datetime(2026, 4, 10), 'renewal_secured', 120_000.0),
            ], profile={'renewal_date': '2026-08-01'}, revenue=3_000_000))
        db.session.commit()
        from journeys.wizard_a import run_wizard_a
        run_wizard_a(cid, evaluate_playbooks=False)
        yield cid, declining, healthy
        db.session.remove()
        db.drop_all()


def _journey(cid, aid):
    return JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json


def _forecast_sentence(j):
    return next((s for ch in j['narrative']['chapters'] for s in ch['sentences'] if s['template'] == 'forecast_statement'), None)


# ── config ────────────────────────────────────────────────────────────

def test_loader_raises_on_missing_key_and_module_names_no_vertical():
    with pytest.raises(KeyError, match='wizard_d.json has no prior/no_such_key'):
        settings.get('prior', 'no_such_key')
    assert settings.vertical_get('any_vertical', 'prior', 'base_retention_at_decision') == settings.get('prior', 'base_retention_at_decision')
    for f in ('wizards/wizard_d_foresight.py', 'wizards/wizard_d_settings.py', 'wizards/wizard_d_http.py', 'mcp_server/cs_pulse_wizard_d.py'):
        assert not re.search(r"saas|dc2|datacenter|healthcare|manufacturing", (BACKEND / f).read_text(), re.I), f
    for band in ('healthy', 'at_risk', 'critical', 'none'):
        assert band in settings.get('prior', 'health_band_factor') and band in settings.get('expansion', 'health_band_factor')
    for label in ('early_warning', 'recovery_watch', 'aligned', 'leading_only_negative', 'leading_only_positive', 'leading_only_neutral', 'none'):
        assert label in settings.get('prior', 'leading_label_factor')


def test_beta_math_matches_known_values():
    from wizards.wizard_d_foresight import beta_cdf, beta_quantile, beta_update
    assert abs(beta_cdf(2, 3, 0.4) - 0.5248) < 1e-4
    assert abs(beta_quantile(2, 3, beta_cdf(2, 3, 0.4)) - 0.4) < 1e-6
    p, lo, hi = beta_update(0.85, 10, 0, 0, 0.9)
    assert abs(p - 0.85) < 1e-9 and lo < p < hi                      # no labels: the posterior is the prior, with a real credible interval
    p2, lo2, hi2 = beta_update(0.85, 10, 0, 20, 0.9)
    assert p2 < 0.4 and hi2 < hi                                       # twenty losses pull it down and tighten it


# ── prior basis ───────────────────────────────────────────────────────

def test_prior_basis_below_the_minimum_says_so_with_counts(prior_tenant):
    cid, ids = prior_tenant
    with app.app_context():
        from wizards.wizard_d_foresight import run_wizard_d
        res = run_wizard_d(cid)
        assert res['status'] == 'completed' and res['accounts'] == 6 and res['vertical'] == 'saas_premium'
        assert res['labels']['n'] == 2 and res['labels']['negative'] == 1 and res['labels']['positive'] == 1
        assert res['labels']['eligible'] is False and res['basis_counts'] == {'prior': 6}
        blocks = {r.account_id: r.forecast_json for r in AccountForecast.query.filter_by(run_id=res['run_id']).all()}
        assert len(blocks) == 6
        for b in blocks.values():
            assert b['basis'] == 'prior' and b['retention']['interval_semantics'] == 'template_range'
            assert b['retention']['low'] < b['retention']['p'] < b['retention']['high']
            assert b['labels'] == {**b['labels'], 'n': 2, 'needed': settings.get('calibration', 'min_labels')}
            assert 'not calibrated' in b['basis_note'] and '2 labelled decision(s)' in b['basis_note']
            assert b['revenue']['low'] <= b['revenue']['expected_arr_end'] <= b['revenue']['high']
            assert b['separation'].startswith('kpi_only')
        cl, st, gr = blocks[ids['champion_lost']], blocks[ids['steady']], blocks[ids['growth']]
        # the drivers are the config values, applied and named
        d = {x['factor']: x for x in cl['drivers']}
        assert d['health_band']['key'] == 'at_risk' and d['health_band']['value'] == settings.get('prior', 'health_band_factor')['at_risk']
        assert d['arc']['key'] == 'exec_sponsor_change' and d['arc']['value'] == settings.get('prior', 'arc_factor')['exec_sponsor_change']
        assert cl['retention']['p'] < st['retention']['p']
        assert cl['decision_point']['status'] == 'inside_horizon' and cl['retention']['mode'] == 'decision_in_horizon'
        assert cl['template']['arc_type'] == 'exec_sponsor_change' and cl['revenue']['loss_severity_basis'] == 'story_arc_template'
        assert st['decision_point']['status'] == 'beyond_horizon' and st['retention']['mode'] == 'midterm'
        assert st['revenue']['loss_severity_basis'] == 'config_default'
        assert gr['inputs']['expansion_intent_present'] and gr['expansion']['p'] > st['expansion']['p']
        assert blocks[ids['no_renewal']]['decision_point']['status'] == 'unknown'
        lost = blocks[ids['lost']]
        assert lost['decision_point']['status'] == 'resolved' and lost['decision_point']['resolved_by']
        # thin evidence widens the template range by the configured extra, and says why
        hw, extra = settings.get('interval', 'half_width_p'), settings.get('interval', 'thin_evidence_extra')
        nr = blocks[ids['no_renewal']]
        assert 'widened for thin evidence (0 episodes)' in nr['basis_note'] and 'widened' not in cl['basis_note']
        assert abs((cl['retention']['p'] - cl['retention']['low']) - hw) < 1e-6
        assert abs((nr['retention']['p'] - nr['retention']['low']) - (hw + extra)) < 1e-6


def test_portfolio_range_is_propagated_not_summed(prior_tenant):
    cid, ids = prior_tenant
    with app.app_context():
        run = ForecastRun.query.filter_by(customer_id=cid).order_by(ForecastRun.id.desc()).first()
        rows = AccountForecast.query.filter_by(run_id=run.run_id).all()
        p = run.portfolio
        assert p['accounts'] == 6 and p['basis'] == 'prior'
        assert abs(p['arr'] - sum(float(r.arr) for r in rows)) < 0.01
        assert abs(p['expected_arr_end'] - sum(float(r.expected_arr_end) for r in rows)) < 0.01
        ind, cor = p['ranges']['independent'], p['ranges']['correlated']
        assert cor['low'] <= ind['low'] < p['expected_arr_end'] < ind['high'] <= cor['high']
        assert abs(cor['low'] - sum(float(r.expected_arr_low) for r in rows)) < 0.01
        assert p['headline_assumption'] == settings.get('portfolio', 'headline_assumption') and p['low'] == ind['low']
        assert p['nrr'] == round(p['expected_arr_end'] / p['arr'], 4) and p['nrr_low'] < p['nrr'] < p['nrr_high']


# ── journey embedding + narrative + read surface ──────────────────────

def test_forecast_is_embedded_and_the_narrative_cites_it(prior_tenant):
    cid, ids = prior_tenant
    with app.app_context():
        run = ForecastRun.query.filter_by(customer_id=cid).order_by(ForecastRun.id.desc()).first()
        j = _journey(cid, ids['champion_lost'])
        fc = j['forecast']
        assert fc['status'] == 'forecast' and fc['run_id'] == run.run_id and fc['stale'] is False
        s = _forecast_sentence(j)
        assert s is not None, j['narrative']['omitted']
        ids_in_journey = {e['episode_id'] for e in j['episodes']}
        assert s['cites'] and set(s['cites']) <= ids_in_journey and set(s['cites']) <= set(fc['cites'])
        assert s['text'].startswith(f"Foresight (prior basis — 2 of {settings.get('calibration', 'min_labels')} labelled decisions needed")
        assert 'at the renewal on 15 June 2026' in s['text'] and 'against $1,200,000 today' in s['text']
        # an account with nothing to cite: the sentence is not written, and the omission is listed
        j2 = _journey(cid, ids['no_renewal'])
        assert _forecast_sentence(j2) is None
        assert any(o['template'] == 'forecast_statement' and o['reason'] == 'no_citation' for o in j2['narrative']['omitted'])
        # the portfolio row and the read tools carry it
        from journeys.read import list_journeys
        row = next(r for r in list_journeys(cid) if r['account_id'] == ids['champion_lost'])
        assert row['forecast']['basis'] == 'prior' and row['forecast']['p_retain'] == fc['retention']['p'] and row['forecast']['run_id'] == run.run_id
        from wizards.wizard_d_foresight import get_forecast
        port = get_forecast(cid)
        assert port['run_id'] == run.run_id and len(port['accounts']) == 6 and port['portfolio']['nrr'] == run.portfolio['nrr']
        one = get_forecast(cid, ids['champion_lost'])
        assert one['forecast']['retention']['p'] == fc['retention']['p'] and get_forecast(cid, 999_999) is None


def test_rebuild_keeps_the_forecast_and_new_evidence_marks_it_stale(prior_tenant):
    cid, ids = prior_tenant
    with app.app_context():
        from journeys.wizard_a import run_wizard_a
        run_wizard_a(cid, [ids['champion_lost']], evaluate_playbooks=False)
        j = _journey(cid, ids['champion_lost'])
        assert j['forecast']['status'] == 'forecast' and j['forecast']['stale'] is False and _forecast_sentence(j)
        db.session.add(ContextNode(customer_id=cid, account_id=ids['champion_lost'], node_type='SIGNAL', node_subtype='budget_pressure',
                                   source='observed', title='procurement wants a cut', tier=2, occurred_at=datetime(2026, 5, 20),
                                   properties={'sentiment_score': '-0.5'}, source_platform='csv_import', source_event_id='late_signal'))
        db.session.commit()
        run_wizard_a(cid, [ids['champion_lost']], evaluate_playbooks=False)
        j = _journey(cid, ids['champion_lost'])
        assert j['forecast']['stale'] is True and '2026-05-20' in j['forecast']['stale_reason']
        assert 'stale: evidence has arrived since this forecast' in _forecast_sentence(j)['text']
        # a fresh run clears it, and history is kept
        from wizards.wizard_d_foresight import run_wizard_d
        run_wizard_d(cid)
        assert _journey(cid, ids['champion_lost'])['forecast']['stale'] is False
        assert ForecastRun.query.filter_by(customer_id=cid).count() == 2


def test_ask_ai_context_carries_the_forecast_block(prior_tenant):
    cid, ids = prior_tenant
    with app.app_context():
        from ask_ai.answer import account_context
        ctx, gaps, meta, narrative = account_context(cid, ids['champion_lost'], 'will they renew?', None, None)
        assert '"forecast":{' in ctx.text() and '"basis":"prior"' in ctx.text()
        assert any(s['template'] == 'forecast_statement' for ch in narrative['chapters'] for s in ch['sentences'])


# ── tools, route, pipeline ────────────────────────────────────────────

def test_trigger_wizard_d_writes_a_run_and_the_tool_reads_it(prior_tenant):
    cid, ids = prior_tenant
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import trigger_wizard
        from mcp_server.cs_pulse_wizard_d import get_forecast
        out = trigger_wizard(cid, 'd')
        assert out['status'] == 'completed' and out['result_summary']['basis_counts'] == {'prior': 6}
        assert out['result_summary']['labels']['n'] == 2 and 'nrr_low' in out['result_summary']['portfolio']
        run = WizardRun.query.filter_by(customer_id=cid, wizard='d').order_by(WizardRun.id.desc()).first()
        assert run and run.status == 'completed' and run.results['lens'] == 'foresight'
        port = get_forecast(cid)
        assert port['data_origin'] == 'synthetic_test' and port['synthetic'] is True and port['portfolio']['basis'] == 'prior'
        one = get_forecast(cid, ids['steady'])
        assert one['forecast']['retention']['mode'] == 'midterm'


def test_tool_and_route_are_keyed(prior_tenant, monkeypatch):
    cid, ids = prior_tenant
    import mcp_server.auth as auth
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    assert 'get_forecast' in KEYED_TOOLS and 'get_forecast' not in ONBOARDING_TOOLS and 'trigger_wizard' in auth.WRITE_TOOLS
    monkeypatch.setenv('MCP_TRANSPORT', 'http')
    monkeypatch.setattr(auth, 'MCP_AUTH_REQUIRED', True)
    monkeypatch.setattr(auth, 'MCP_SERVER_API_KEY', 'srv-' + uuid.uuid4().hex)
    from fastmcp.exceptions import ToolError
    with app.app_context():
        with pytest.raises(ToolError, match='requires an API key'):
            auth.require_auth_if_key_present('get_forecast', cid)
    # the HTTP route through the same authorizer, on a minimal Starlette app
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from wizards.wizard_d_http import register_forecast_routes
    handlers = {}

    class _Mcp:
        def custom_route(self, path, methods, name=None):
            def deco(fn):
                handlers[path] = (fn, methods)
                return fn
            return deco
    register_forecast_routes(_Mcp())
    (fn, methods), = handlers.values()
    client = TestClient(Starlette(routes=[Route('/api/forecast', fn, methods=methods)]))
    assert client.get(f'/api/forecast?customer_id={cid}').status_code == 401
    tok = auth._current_api_key_var.set(auth.MCP_SERVER_API_KEY)
    try:
        r = client.get(f'/api/forecast?customer_id={cid}')
        assert r.status_code == 200 and r.json()['portfolio']['basis'] == 'prior' and len(r.json()['accounts']) == 6
        r = client.get(f'/api/forecast?customer_id={cid}&account_id={ids["growth"]}')
        assert r.status_code == 200 and r.json()['forecast']['inputs']['expansion_intent_present'] is True
        assert client.get(f'/api/forecast?customer_id={cid}&account_id=999999').status_code == 404
    finally:
        auth._current_api_key_var.reset(tok)


def test_process_data_step_reports_counts_and_honours_the_switch(prior_tenant, monkeypatch):
    cid, ids = prior_tenant
    from mcp_server.process_data_pipeline import run_wizard_d_step
    with app.app_context():
        step, dur = run_wizard_d_step(cid)
        assert step == f"wizard_d_6_forecasts_6_prior_0_calibrated_2_of_{settings.get('calibration', 'min_labels')}_labels"
        real_get = settings.get
        monkeypatch.setattr(settings, 'get', lambda *k: False if k == ('run_in_process_data',) else real_get(*k))
        assert run_wizard_d_step(cid)[0] is None


# ── calibrated basis, on a second vertical ────────────────────────────

def test_labels_are_point_in_time_and_grouped_within_the_window(calibrated_tenant):
    cid, declining, healthy = calibrated_tenant
    with app.app_context():
        from wizards.wizard_d_foresight import collect_labels, calibration_gate
        labels = collect_labels([_journey(cid, a) for a in declining + healthy])
        assert len(labels) == 36
        mine = sorted((l for l in labels if l['account_id'] == declining[0]), key=lambda l: l['decided_at'])
        assert [l['retained'] for l in mine] == [1, 0, 0]
        assert [l['stratum'] for l in mine] == ['healthy|no_warning', 'at_risk|no_warning', 'at_risk|no_warning']   # the month BEFORE each decision
        gate = calibration_gate(labels)
        assert gate['eligible'] and gate['n'] == 36 and gate['positive'] == 24 and gate['negative'] == 12
        assert gate['by_stratum']['at_risk|no_warning'] == {'n': 12, 'retained': 0, 'not_retained': 12, 'expanded': 0}
        assert gate['by_stratum']['healthy|no_warning']['n'] == 24 and gate['by_stratum']['healthy|no_warning']['expanded'] == 6
        # two outcomes inside the window are one decision
        j = dict(_journey(cid, healthy[0]))
        j['episodes'] = j['episodes'] + [{**next(e for e in j['episodes'] if e['kind'] == 'outcome'), 'episode_id': 'out:twin',
                                          'date': '2025-12-20T00:00:00', 'revenue_bucket': 'lost'}]
        twin = [l for l in collect_labels([j]) if l['decided_at'] == '2025-12-10']
        assert len(twin) == 1 and twin[0]['retained'] == 0 and set(twin[0]['episode_ids']) >= {'out:twin'}


def test_calibrated_basis_updates_the_prior_on_the_tenants_own_outcomes(calibrated_tenant):
    cid, declining, healthy = calibrated_tenant
    with app.app_context():
        from wizards.wizard_d_foresight import run_wizard_d
        res = run_wizard_d(cid)
        assert res['vertical'] == 'datacenter_v1' and res['basis_counts'] == {'calibrated': 12} and res['labels']['eligible']
        blocks = {r.account_id: r.forecast_json for r in AccountForecast.query.filter_by(run_id=res['run_id']).all()}
        slide, solid = blocks[declining[0]], blocks[healthy[0]]
        assert slide['basis'] == 'calibrated' and slide['retention']['interval_semantics'] == 'beta_credible'
        assert slide['labels']['stratum'] == 'at_risk|no_warning' and slide['labels']['stratum_used'] == 'own' and slide['labels']['stratum_n'] == 12
        assert slide['retention']['p'] < slide['retention']['prior_p'] and slide['retention']['p'] < 0.4      # twelve losses in its stratum
        assert solid['labels']['stratum_used'] == 'own' and solid['retention']['p'] > solid['retention']['prior_p']
        assert solid['expansion']['p'] > solid['expansion']['prior_p']                                       # six expansions in its stratum
        for b in blocks.values():
            assert b['retention']['low'] < b['retention']['p'] < b['retention']['high']
            assert 'prior updated on' in b['basis_note']
        assert res['portfolio']['basis'] == 'calibrated'
        s = _forecast_sentence(_journey(cid, declining[0]))
        assert s and s['text'].startswith('Foresight (calibrated on 12 of 36 labelled decisions in its own stratum at_risk|no_warning)')
        # a stratum the tenant has never observed falls back to the pooled counts, and says so
        from wizards.wizard_d_foresight import forecast_account, calibration_gate, collect_labels
        j = dict(_journey(cid, healthy[0]))
        j['leading_vs_trailing'] = dict(j['leading_vs_trailing'])
        j['leading_vs_trailing']['series'] = j['leading_vs_trailing']['series'][:-1] + [{**j['leading_vs_trailing']['series'][-1], 'kpi_only': 30.0}]
        gate = calibration_gate(collect_labels([_journey(cid, a) for a in declining + healthy]))
        acct = db.session.get(Account, healthy[0])
        pooled = forecast_account(j, acct, 'datacenter_v1', gate, 'test_run', settings.get('horizon_days'))
        assert pooled['labels']['stratum'] == 'critical|no_warning' and pooled['labels']['stratum_used'] == 'pooled'


def test_intervention_in_flight_is_a_labelled_template_lift():
    from wizards.wizard_d_foresight import interventions_in_flight
    j = {'phases': [{'name': 'deterioration', 'entered_at': '2026-03-01'}],
         'episodes': [{'episode_id': 'int:1', 'kind': 'intervention', 'date': '2026-03-10T00:00:00', 'meta': {'closed_state': None}},
                      {'episode_id': 'int:2', 'kind': 'intervention', 'date': '2026-03-12T00:00:00', 'meta': {'closed_state': 'done'}},
                      {'episode_id': 'int:3', 'kind': 'intervention', 'date': '2026-01-12T00:00:00', 'meta': {'closed_state': None}}]}
    assert [e['episode_id'] for e in interventions_in_flight(j)] == ['int:1']
