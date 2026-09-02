"""
Acceptance for the three protocol-shaped demo manifests
(docs/design/demo-narratives.md): each generates schema-valid CSVs,
registers through the real MCP tools stamped synthetic, and the harness
reads the constructed story back — featured lead times, the CRM flag as
comparator, the false-alarm account counted, the unclassified account
unclassified — and never labels any of it "measured".
"""
import csv
import io
import os
import sys
import uuid
from pathlib import Path

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

from models import Customer, Account, JourneyData, HealthScore
from demo.generate import generate, register, load_manifest, health_to_kpi_value, MANIFESTS_DIR

MANIFESTS = sorted(MANIFESTS_DIR.glob('demo_*.json'))


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


@pytest.fixture(scope='module')
def registered():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    out = {}
    with app.app_context():
        db.create_all()
        from evals.lead_time_backtest import run_backtest, format_report
        for path in MANIFESTS:
            m = load_manifest(path)
            files = generate(m)
            reg = register(m, files, name_suffix=uuid.uuid4().hex[:6])
            assert reg['status'] == 'success', reg
            rep = run_backtest(reg['customer_id'], min_events=1)
            print(f"\n### {m['manifest_id']}\n" + format_report(rep))
            out[m['manifest_id']] = (m, files, reg, rep)
        yield out
        db.session.remove()
        db.drop_all()


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class TestInversion:
    def test_health_to_kpi_value_round_trips_through_the_scorer(self):
        from utils.generic_scorer import score_kpi
        from utils.vertical_registry import get_kpis
        for vertical in ('datacenter_v1', 'saas_premium'):
            for code, kdef in list(get_kpis(vertical).items())[:12]:
                for h in (10, 45, 50, 60, 70, 85, 99):
                    v = health_to_kpi_value(h, kdef)
                    assert abs(score_kpi(v, kdef) - h) < 0.5, (vertical, code, h, v)


class TestGeneration:
    @pytest.mark.parametrize('path', MANIFESTS, ids=lambda p: p.stem)
    def test_files_are_schema_valid_and_deterministic(self, path):
        from utils.csv_upload import _upload_csv_impl
        m = load_manifest(path)
        files = generate(m)
        assert generate(m) == files                                   # seeded
        assert set(files) == {'account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'outcomes.csv'}
        for ft, content in files.items():
            r = _upload_csv_impl(0, ft, content, dry_run=True)
            assert r.valid, (ft, r.errors)
            assert not any('Unknown columns' in w for w in r.warnings), (ft, r.warnings)
        accts = _rows(files['account_details.csv'])
        assert len(accts) == len(m['accounts']) + len(m['background']['names'])
        outs = _rows(files['outcomes.csv'])
        assert all(o['linked_signal_id'] for o in outs)               # every event links to its signal
        sigs = _rows(files['enhanced_qualitative_signals.csv'])
        assert all(s['signal_ref'] == s['signal_id'] for s in sigs)


class TestRegisteredTenants:
    def test_stamped_synthetic_and_never_measured(self, registered):
        with app.app_context():
            for mid, (m, files, reg, rep) in registered.items():
                assert db.session.get(Customer, reg['customer_id']).data_origin == 'synthetic_demo'
                assert rep['evidence_label'] != 'measured'
                assert rep['data_origin'] == 'synthetic_demo'

    def test_scenario_a_silent_displacement(self, registered):
        m, files, reg, rep = registered['demo_silent_displacement_dc']
        h1 = rep['results']['H1_retention']
        assert h1['events'] == 1
        ev = h1['per_event'][0]
        assert ev['account'] == 'Meridian AI' and ev['event'] == 'contraction'
        # composite crossed in Jan (signals from T-104) → warned at Jan's end, ~75 days out;
        # trailing crossed at T-48 (Mar 3) → month-end Mar 31, 19 days; CSM flag T-14, dated exactly
        assert 60 <= ev['leading_lead_days'] <= 95, ev
        assert 15 <= ev['trailing_lead_days'] <= 75, ev
        assert ev['crm_lead_days'] == 14, ev
        assert ev['leading_lead_days'] > ev['trailing_lead_days'] > ev['crm_lead_days']
        # Quantum Labs' idle spike (T-60) recovered — a false alarm, or still open
        # depending on how much data follows it; Helix (live twin) is open
        assert h1['leading']['false_alarm_months'] + h1['leading']['censored_warning_months'] >= 2
        cov = reg['wizard_a']['coverage']
        assert cov['unclassified'] >= 1
        arcs = {a['account_name']: a for a in reg['wizard_a']['arcs'].values()}
        assert arcs['Orion Models']['state'] == 'unclassified'
        assert arcs['Meridian AI']['arc_type'] in ('silent_churn', 'competitive_displacement')
        assert arcs['Helix Compute']['arc_type'] in ('silent_churn', 'competitive_displacement')   # the live twin

    def test_scenario_b_expansion_intent(self, registered):
        m, files, reg, rep = registered['demo_expansion_intent_dc']
        h2 = rep['results']['H2_growth']
        assert h2['events'] == 1
        ev = h2['per_event'][0]
        assert ev['account'] == 'Stellar Inference' and ev['event'] == 'expansion_closed'
        # funding_raised at T-72 (Jan 28) → expansion-intent warning at Jan's end → ~69 days
        assert ev['leading_lead_days'] is not None and 40 <= ev['leading_lead_days'] <= 90, ev
        arcs = {a['account_name']: a for a in reg['wizard_a']['arcs'].values()}
        assert arcs['Stellar Inference']['arc_type'] in ('expansion_champion', 'land_and_expand')
        assert arcs['Zenith Training']['arc_type'] in ('expansion_champion', 'land_and_expand')
        # Cirrus AI's deferred pilot (T-70) and Zenith/Vector's open stories
        assert h2['leading']['false_alarm_months'] + h2['leading']['censored_warning_months'] >= 2

    def test_scenario_c_champion_departure_with_intervention(self, registered):
        m, files, reg, rep = registered['demo_champion_departure_saas']
        h1 = rep['results']['H1_retention']
        assert h1['events'] == 1
        ev = h1['per_event'][0]
        assert ev['account'] == 'Northwind Analytics'
        assert 60 <= ev['leading_lead_days'] <= 100, ev                  # champion left at T-106
        assert ev['crm_lead_days'] is not None and ev['leading_lead_days'] > ev['crm_lead_days']
        arcs = {a['account_name']: a for a in reg['wizard_a']['arcs'].values()}
        assert arcs['Northwind Analytics']['arc_type'] == 'exec_sponsor_change'
        assert arcs['Cascade Retail']['arc_type'] == 'exec_sponsor_change'
        assert arcs['Granite Insurance']['state'] == 'unclassified'
        with app.app_context():
            acct = Account.query.filter_by(customer_id=reg['customer_id'], account_name='Northwind Analytics').first()
            j = JourneyData.query.filter_by(account_id=acct.account_id).first().journey_json
            hooks = j['counterfactual_hooks']
            assert any('exec sponsor rebuild' in h['title'] for h in hooks)
            assert j['expected_path']['arc_type'] == 'exec_sponsor_change'
            # Blue Harbor's internal champion move recovered without intervention → on record as
            # a false alarm (or still open, if too little data follows it)
            assert h1['leading']['false_alarm_months'] + h1['leading']['censored_warning_months'] >= 1


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
