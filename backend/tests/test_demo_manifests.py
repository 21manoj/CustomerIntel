"""
Acceptance for the protocol-shaped demo manifests
(docs/design/demo-narratives.md): each generates schema-valid CSVs,
registers through the real MCP tools stamped synthetic — v2 manifests
submit their communications through the signal engine — and the harness
reads the constructed story back: featured lead times, the CRM flag as
comparator, the false-alarm account counted, the unclassified account
unclassified — and never labels any of it "measured".

Extraction here is the ORACLE (the manifest's labels played back through
the engine, demo/oracle.py): the narratives are about the journey and the
backtest, not about the model. What a real extractor reads out of the
same texts is tests/test_demo_v2.py's scorecard, reported as-is.
"""
import csv
import io
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop('ANTHROPIC_API_KEY', None)
os.environ['FEATURE_SIGNAL_ENGINE'] = 'true'

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

from models import Customer, Account, JourneyData, HealthScore, QualitativeSignal, ContextNode
from demo.generate import generate, register, load_manifest, health_to_kpi_value, expand_accounts, MANIFESTS_DIR
from demo.manifest_v2 import is_v2, signals_only, plan_communications
from demo.oracle import ORACLE_MODEL_VERSION

MANIFESTS = sorted(MANIFESTS_DIR.glob('demo_*.json'))


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


@pytest.fixture(scope='module')
def registered(tmp_path_factory):
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    out = {}
    out_dir = tmp_path_factory.mktemp('demo_out')
    with app.app_context():
        db.create_all()
        from evals.lead_time_backtest import run_backtest, format_report
        for path in MANIFESTS:
            m = load_manifest(path)
            files = generate(m)
            reg = register(m, files, name_suffix=uuid.uuid4().hex[:6], extractor='oracle', out_dir=out_dir)
            assert reg['status'] == 'success', reg
            rep = None
            if not signals_only(m):
                # evals/lead_time_backtest._warning_months compares kpi_only < at_risk on every
                # month; a signals-only tenant has only live months (kpi_only None) → TypeError.
                # Known gap in evals/, not patched here (see test_demo_v2 / demo-narratives §7).
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
        assert is_v2(m)                                               # all shipped manifests are v2 now
        expected = {'account_details.csv', 'outcomes.csv'}
        if not signals_only(m):
            expected.add('kpi_measurements.csv')
        if any(a.get('crm_flag_day') is not None for a in m['accounts']):
            expected.add('enhanced_qualitative_signals.csv')
        assert set(files) == expected, set(files)
        for ft, content in files.items():
            r = _upload_csv_impl(0, ft, content, dry_run=True)
            assert r.valid, (ft, r.errors)
            assert not any('Unknown columns' in w for w in r.warnings), (ft, r.warnings)
        accts = _rows(files['account_details.csv'])
        assert len(accts) == len(m['accounts']) + len((m.get('background') or {}).get('names', []))
        outs = _rows(files['outcomes.csv'])
        assert all(o['linked_signal_id'].endswith('_comm_' + o['linked_signal_id'].rsplit('_', 1)[-1]) for o in outs)
        # v2: no behavioral signal rows on the CSV — only the CSM's declared flag
        if 'enhanced_qualitative_signals.csv' in files:
            assert {s['signal_type'] for s in _rows(files['enhanced_qualitative_signals.csv'])} == {'csm_risk_flag'}
        comms = plan_communications(m, expand_accounts(m))
        assert comms and all(c['expected_subtypes'] is not None and c['participants'] for c in comms)


class TestRegisteredTenants:
    def test_stamped_synthetic_and_never_measured(self, registered):
        with app.app_context():
            for mid, (m, files, reg, rep) in registered.items():
                assert db.session.get(Customer, reg['customer_id']).data_origin == 'synthetic_demo'
                if rep is not None:
                    assert rep['evidence_label'] != 'measured'
                    assert rep['data_origin'] == 'synthetic_demo'

    def test_communications_went_through_the_engine_not_the_csv(self, registered):
        """Every behavioral signal on a v2 tenant is a QualitativeSignal the
        pipeline ingested (source_type set, dated by the event) with an
        OBSERVED SIGNAL node; the only CSV-born signal is the CSM flag."""
        with app.app_context():
            for mid, (m, files, reg, rep) in registered.items():
                cid = reg['customer_id']
                comms = plan_communications(m, expand_accounts(m))
                engine_sigs = QualitativeSignal.query.filter(QualitativeSignal.customer_id == cid,
                                                             QualitativeSignal.source_type.isnot(None)).all()
                assert len(engine_sigs) == len(comms), mid
                assert all(s.cg_node_id is not None and s.occurred_at is not None for s in engine_sigs)
                assert all(s.llm_model_version == ORACLE_MODEL_VERSION for s in engine_sigs)
                csv_sigs = QualitativeSignal.query.filter(QualitativeSignal.customer_id == cid,
                                                          QualitativeSignal.source_type.is_(None)).all()
                assert {s.signal_type for s in csv_sigs} <= {'csm_risk_flag'}, mid
                nodes = ContextNode.query.filter_by(customer_id=cid, node_type='SIGNAL').all()
                assert all(n.source == 'observed' for n in nodes)
                by_event = {n.source_event_id for n in nodes}
                assert {s.signal_id for s in engine_sigs} <= by_event

    def test_scorecard_is_perfect_under_the_oracle_and_says_so(self, registered):
        for mid, (m, files, reg, rep) in registered.items():
            sc = reg['scorecard']
            comms = plan_communications(m, expand_accounts(m))
            assert sc['communications'] == len(comms) and sc['exact'] == len(comms) and sc['miss'] == 0, mid
            assert sc['hit_rate'] == 1.0 and sc['subtype']['precision'] == 1.0 and sc['subtype']['recall'] == 1.0
            assert sc['pending'] == 0 and sc['duplicates'] == 0 and not sc['errors']
            assert sc['model_version'] == ORACLE_MODEL_VERSION and 'not a model result' in sc['label']
            assert all(v['precision'] == 1.0 and v['recall'] == 1.0 for v in sc['roles'].values())
            paths = reg['outputs']
            assert Path(paths['scorecard']).exists() and Path(paths['labelled']).exists()
            lines = [json.loads(l) for l in Path(paths['labelled']).read_text().splitlines()]
            assert len(lines) == len(comms)
            assert all(l['text'] and l['source_type'] and l['model_version'] == ORACLE_MODEL_VERSION
                       and l['extracted_subtypes'] == l['expected_subtypes'] for l in lines)

    def test_outcomes_link_to_the_engine_signal(self, registered):
        """emit_outcomes rewrote the manifest ref to the engine's signal id:
        the LED_TO edge goes from the ingested communication's node."""
        with app.app_context():
            from models import ContextEdge
            for mid, (m, files, reg, rep) in registered.items():
                cid = reg['customer_id']
                n_events = sum(len(a.get('events', [])) for a in m['accounts'])
                edges = ContextEdge.query.filter_by(customer_id=cid, edge_type='LED_TO').all()
                assert len(edges) == n_events, (mid, len(edges), n_events)
                for e in edges:
                    src = db.session.get(ContextNode, e.from_node_id)
                    assert src.node_type == 'SIGNAL' and src.source_platform in ('email', 'slack', 'ticket', 'meeting', 'crm_activity', 'transcript', 'manual')

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
            # the champion episode is the ingested communication, person resolved against the roster
            eps = [e for e in j['episodes'] if e['kind'] == 'signal' and e['role'] == 'champion_change']
            assert eps and eps[0]['meta']['stakeholder'] == 'Elena Rossi'
            # Blue Harbor's internal champion move recovered without intervention → on record as
            # a false alarm (or still open, if too little data follows it)
            assert h1['leading']['false_alarm_months'] + h1['leading']['censored_warning_months'] >= 1

    def test_scenario_d_signals_only_builds_journeys_from_evidence_alone(self, registered):
        """P1: no KPI rows, no health scores — every month is live, kpi_only is
        None throughout, the leading series carries the composite, and the
        champion-departure story classifies through the health-free rule."""
        m, files, reg, rep = registered['demo_signals_only_saas']
        assert reg['signals_only'] and rep is None
        with app.app_context():
            cid = reg['customer_id']
            aids = [a.account_id for a in Account.query.filter_by(customer_id=cid).all()]
            assert len(aids) == 6
            assert HealthScore.query.filter(HealthScore.account_id.in_(aids)).count() == 0
            journeys = {jd.journey_json['account_name']: jd.journey_json
                        for jd in JourneyData.query.filter_by(customer_id=cid).all()}
            assert len(journeys) == 6
            for name, j in journeys.items():
                series = j['leading_vs_trailing']['series']
                assert series, name
                assert all(s['kpi_only'] is None and s['live'] for s in series), name
                assert all(s['early_warning'] in ('leading_only', None) for s in series)
                assert any(s['qual'] is not None for s in series), name
                assert j['summary']['months_scored'] == 0 and j['live_months']
            halcyon = journeys['Halcyon Health']
            assert halcyon['arc']['arc_type'] == 'exec_sponsor_change'
            assert halcyon['leading_vs_trailing']['first_leading_warning_at'] is not None
            assert all(s['qual'] is not None and s['qual'] < 50 for s in halcyon['leading_vs_trailing']['series'])
            # the expansion story is evidence-complete but the expansion arcs need a health predicate
            # (very_healthy / healthy) that a signals-only tenant cannot satisfy — P1's open half
            orchard = journeys['Orchard Retail']
            roles = set()
            for s in orchard['leading_vs_trailing']['series']:
                roles |= set(s['roles'])
            assert {'expansion_intent', 'advocacy'} <= roles and orchard['state'] == 'unclassified'
            assert reg['wizard_a']['coverage']['unclassified'] >= 4


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
