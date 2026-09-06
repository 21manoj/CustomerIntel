"""
Tier 2B checkpoint: Wizard B (Hindsight) on the three demo tenants,
registered through the real tools. Every number below is checkable from
the manifests. Also covers trigger_wizard for 'a'/'b' and the WizardRun
audit row, and the auto-run inside process_data.
"""
import os
import sys
import uuid

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

from models import WizardRun, Account
from fastmcp.exceptions import ToolError
from demo.generate import generate, register, load_manifest, MANIFESTS_DIR


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


@pytest.fixture(scope='module')
def tenants():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    out = {}
    with app.app_context():
        db.create_all()
        for mid in ('demo_silent_displacement_dc', 'demo_champion_departure_saas'):
            m = load_manifest(MANIFESTS_DIR / f'{mid}.json')
            reg = register(m, generate(m), name_suffix=uuid.uuid4().hex[:6])
            assert reg['status'] == 'success', reg
            out[mid] = (m, reg)
        yield out
        db.session.remove()
        db.drop_all()


class TestAutoRunInPipeline:
    def test_process_data_ran_wizard_b_and_persisted_a_run(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        assert any(s.startswith('wizard_b_12_journeys_') for s in reg['steps']), reg['steps']
        with app.app_context():
            runs = WizardRun.query.filter_by(customer_id=reg['customer_id'], wizard='b').all()
            assert len(runs) == 1 and runs[0].status == 'completed'
            assert runs[0].results['lens'] == 'hindsight'
            assert runs[0].results['evidence_label'] != 'measured'


class TestHindsight:
    def test_pattern_profiles_keyed_by_journey_arc(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        with app.app_context():
            from wizards.wizard_b_hindsight import run_wizard_b
            res = run_wizard_b(reg['customer_id'], min_events=1, persist=False)
        prof = res['pattern_profiles']
        assert 'unclassified' in prof and prof['unclassified']['kind'] == 'state'
        assert 'Orion Models' in prof['unclassified']['accounts']
        arc_of_meridian = next(k for k, p in prof.items() if 'Meridian AI' in p['accounts'])
        assert prof[arc_of_meridian]['kind'] == 'arc'
        assert prof[arc_of_meridian]['avg_ending_health'] < prof[arc_of_meridian]['avg_starting_health']
        assert sum(p['n_accounts'] for p in prof.values()) == 12
        # every profile's phase mix sums to ~100
        for p in prof.values():
            assert abs(sum(p['phase_distribution_pct'].values()) - 100) < 0.5

    def test_transitions_carry_triggers(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        with app.app_context():
            from wizards.wizard_b_hindsight import run_wizard_b
            res = run_wizard_b(reg['customer_id'], min_events=1, persist=False)
        tr = res['transitions']
        into_decline = [v for k, v in tr.items() if v['to_phase'] in ('deterioration', 'intervention')]
        assert into_decline, tr
        assert any(t['triggers'] for t in into_decline)
        assert all(0 < t['probability'] <= 1 for t in tr.values())

    def test_realized_nrr_counts_only_lost_and_expansion(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        with app.app_context():
            from wizards.wizard_b_hindsight import run_wizard_b
            res = run_wizard_b(reg['customer_id'], min_events=1, persist=False)
            total_arr = sum(float(a.revenue) for a in Account.query.filter_by(customer_id=reg['customer_id']).all())
        nrr = res['realized_nrr']
        assert nrr['portfolio']['starting_arr'] == total_arr
        assert nrr['portfolio']['lost'] == 1_400_000.0 and nrr['portfolio']['expansion'] == 0.0
        assert nrr['portfolio']['nrr'] == round((total_arr - 1_400_000) / total_arr, 4)
        arc_of_meridian = next(k for k, g in nrr['by_arc'].items() if g['lost'] == 1_400_000.0)
        assert nrr['by_arc'][arc_of_meridian]['nrr'] < 1.0

    def test_interventions_and_expected_path(self, tenants):
        m, reg = tenants['demo_champion_departure_saas']
        with app.app_context():
            from wizards.wizard_b_hindsight import run_wizard_b
            res = run_wizard_b(reg['customer_id'], min_events=1, persist=False)
        iv = res['interventions']
        assert iv['n'] >= 1
        row = next(r for r in iv['rows'] if r['account'] == 'Northwind Analytics')
        assert row['arc'] == 'exec_sponsor_change'
        assert row['expected_path_end_health'] is not None          # the template's typical end
        assert 'not a causal estimate' in iv['basis']

    def test_backtest_and_derived_rules(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        with app.app_context():
            from wizards.wizard_b_hindsight import run_wizard_b
            res = run_wizard_b(reg['customer_id'], min_events=1, persist=False)
        h1 = res['backtest']['results']['H1_retention']
        assert h1['events'] == 1 and h1['leading']['n'] == 1
        rules = [r for r in res['early_warning_rules'] if r['hypothesis'] == 'H1_retention']
        assert rules, res['early_warning_rules']
        roles = {r['role'] for r in rules}
        assert roles & {'usage_decline', 'engagement_decline', 'commercial_pressure'}
        assert all(r['rule_semantics'].startswith('observed frequency') for r in rules)
        assert all(r['median_lead_days'] is not None for r in rules)


class TestTriggerWizard:
    def test_trigger_b_returns_summary_and_writes_run(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        from mcp_server.cs_pulse_onboarding import trigger_wizard
        out = trigger_wizard(reg['customer_id'], 'b')
        assert out['status'] == 'completed' and out['wizard'] == 'b'
        assert out['result_summary']['journeys'] == 12
        assert out['result_summary']['portfolio_nrr'] < 1.0
        with app.app_context():
            run = WizardRun.query.filter_by(run_id=out['run_id']).first()
            assert run and run.status == 'completed' and run.results['lens'] == 'hindsight'

    def test_trigger_a_rebuilds_journeys(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        from mcp_server.cs_pulse_onboarding import trigger_wizard
        out = trigger_wizard(reg['customer_id'], 'a')
        assert out['status'] == 'completed' and out['result_summary']['processed'] == 12

    def test_unported_wizard_is_a_clear_error(self, tenants):
        m, reg = tenants['demo_silent_displacement_dc']
        from mcp_server.cs_pulse_onboarding import trigger_wizard
        with pytest.raises(ToolError, match="Available in this build"):
            trigger_wizard(reg['customer_id'], 'z')      # 'c' and 'd' are both in this build now (wizard_c_calibration, wizard_d_foresight)

    def test_too_few_journeys_is_skipped_not_failed(self):
        from mcp_server.cs_pulse_onboarding import create_customer, trigger_wizard
        with app.app_context():
            tag = uuid.uuid4().hex[:6]
            cid = create_customer(data_origin='synthetic_test', name=f'Tiny {tag}', domain=f'tiny-{tag}.test', vertical='saas_premium',
                                  admin_email=f'tiny_{tag}@t.test', admin_name='T')['customer_id']
        out = trigger_wizard(cid, 'b')
        assert out['status'] == 'completed' and out['result_summary']['status'] == 'skipped'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
