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

from extensions import db


from mcp_server.common import get_flask_app
app = get_flask_app()

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


class TestWaypointCurves:
    """The `waypoints` health shape — the curve a multi-phase story needs when
    one dip and one recovery (dip_recover) cannot express it."""

    def test_interpolates_between_points_and_holds_flat_outside_them(self):
        from demo.generate import health_at
        spec = {'shape': 'waypoints', 'points': [[-400, 70], [-200, 40], [0, 80]]}
        at = lambda d: health_at(d, spec, -400, 50)
        assert at(-500) == 70 and at(-400) == 70                     # held before the first point
        assert at(0) == 80 and at(100) == 80                         # held after the last
        assert abs(at(-300) - 55) < 0.01                             # midpoint of the fall
        assert abs(at(-100) - 60) < 0.01                             # midpoint of the rise
        assert at(-200) == 40                                        # the trough itself

    def test_unsorted_points_are_walked_in_day_order(self):
        from demo.generate import health_at
        jumbled = {'shape': 'waypoints', 'points': [[0, 80], [-400, 70], [-200, 40]]}
        ordered = {'shape': 'waypoints', 'points': [[-400, 70], [-200, 40], [0, 80]]}
        assert [health_at(d, jumbled, -400, 50) for d in range(-400, 1, 25)] == \
               [health_at(d, ordered, -400, 50) for d in range(-400, 1, 25)]

    @pytest.mark.parametrize('bad, msg', [
        ({'shape': 'waypoints'}, "needs ['points']"),
        ({'shape': 'waypoints', 'points': [[-10, 50]]}, 'at least two'),
        ({'shape': 'waypoints', 'points': [[-10, 50], [-10, 60]]}, 'duplicate days'),
        ({'shape': 'waypoints', 'points': [[-10, 50], [0, 140]]}, 'within [0, 100]'),
        ({'shape': 'waypoints', 'points': [[-10, 50], [0, 60, 70]]}, 'must be [day, health]'),
        ({'shape': 'sawtooth', 'start': 60}, 'is not one of'),
    ])
    def test_a_bad_curve_fails_at_load_with_the_account_named(self, bad, msg):
        from demo.manifest_v2 import ManifestError, validate_health
        with pytest.raises(ManifestError) as e:
            validate_health(bad, 'm/ACCT')
        assert 'm/ACCT' in str(e.value) and msg in str(e.value)


class TestTrancheSlicing:
    """slice_manifest: the tenant as it stood on a day. Data arrives over time,
    and a playbook only ever evaluates the latest leading month, so a story with
    two intervention cycles months apart is fed in slices."""

    def _manifest(self):
        return load_manifest(MANIFESTS_DIR / 'aurelia_datacenter_portfolio.json')

    def test_drops_everything_after_the_day_and_keeps_everything_before(self):
        from demo.generate import slice_manifest
        m = self._manifest()
        through = -224
        s = slice_manifest(m, through)
        assert {a['source_account_id'] for a in s['accounts']} == {a['source_account_id'] for a in m['accounts']}
        for full, cut in zip(m['accounts'], s['accounts']):
            kept = [c for c in full['communications'] if c['day'] <= through]
            assert cut['communications'] == kept                      # prefix, in order, unmodified
            assert all(e['day'] <= through for e in cut['events'])
            assert 'crm_flag_day' not in cut or cut['crm_flag_day'] <= through

    def test_kpi_values_are_identical_to_the_full_run_for_the_days_it_emits(self):
        from demo.generate import slice_manifest
        m = self._manifest()
        full = _rows(generate(m)['kpi_measurements.csv'])
        part = _rows(generate(slice_manifest(m, -224))['kpi_measurements.csv'])
        key = lambda r: (r['source_account_id'], r['kpi_code'], r['measured_at'])
        by_key = {key(r): r['value'] for r in full}
        assert part and len(part) < len(full)
        assert all(by_key[key(r)] == r['value'] for r in part)        # the curve keeps its anchor
        assert max(r['measured_at'] for r in part) < max(r['measured_at'] for r in full)

    def test_the_last_slice_reproduces_the_whole_manifest(self):
        from demo.generate import slice_manifest
        m = self._manifest()
        last = max(t['through_day'] for t in m['tranches'])
        assert generate(slice_manifest(m, last)) == generate(m)

    def test_an_event_cannot_outlive_the_evidence_it_cites(self):
        from demo.generate import slice_manifest
        from demo.manifest_v2 import ManifestError
        m = self._manifest()
        a = next(x for x in m['accounts'] if x['events'] and x['events'][0].get('linked_communication_index') is not None)
        ev, idx = a['events'][0], a['events'][0]['linked_communication_index']
        cut = a['communications'][idx]['day'] - 1                     # keeps the event, drops its citation
        ev['day'] = cut
        with pytest.raises(ManifestError) as e:
            slice_manifest(m, cut)
        assert 'does not have yet' in str(e.value)


class TestStoryDeclaration:
    """`tranches` + `interventions`: validated at load against this manifest's own
    accounts and the vertical's own playbooks, so the driver never discovers a typo
    half way through a tenant it has already created."""

    def _manifest(self):
        return load_manifest(MANIFESTS_DIR / 'aurelia_datacenter_portfolio.json')

    def test_the_shipped_portfolio_declares_a_coherent_story(self):
        from playbooks.definitions import load_vertical
        m = self._manifest()
        known = {p['id'] for p in load_vertical(m['vertical'])['playbooks']}
        ids = {a['source_account_id'] for a in m['accounts']}
        tranches = [t['through_day'] for t in m['tranches']]
        assert tranches == sorted(set(tranches))
        for iv in m['interventions']:
            assert iv['playbook_id'] in known and iv['source_account_id'] in ids
            assert iv['tranche'] in {t['id'] for t in m['tranches']}
        # every showcase account runs two cycles on two DIFFERENT playbooks: governance
        # suppresses a re-fire of the same playbook within window_days of a close
        cycles = {}
        for iv in m['interventions']:
            cycles.setdefault(iv['source_account_id'], []).append(iv['playbook_id'])
        assert cycles and all(len(v) == 2 and len(set(v)) == 2 for v in cycles.values()), cycles

    @pytest.mark.parametrize('mutate, msg', [
        (lambda m: m['interventions'][0].update(playbook_id='no_such_playbook'), 'is not a datacenter_v1 playbook'),
        (lambda m: m['interventions'][0].update(source_account_id='NOBODY'), 'is not an account in this manifest'),
        (lambda m: m['interventions'][0].update(tranche='t99'), 'is not one of'),
        (lambda m: m['interventions'][0]['report'].update(outcome_type='free_lunch'), 'not in the datacenter_v1 revenue buckets'),
        (lambda m: m['interventions'][0]['report'].update(state='maybe'), 'report.state must be'),
        (lambda m: m['tranches'].append({'id': 't1', 'through_day': 10}), 'duplicate tranche id'),
        (lambda m: m['tranches'].append({'id': 'tz', 'through_day': -9999}), 'must be after the previous tranche'),
        (lambda m: m.pop('tranches'), 'needs "tranches"'),
    ])
    def test_a_bad_story_fails_at_load(self, mutate, msg):
        from demo.manifest_v2 import ManifestError, validate_manifest
        m = self._manifest()
        mutate(m)
        with pytest.raises(ManifestError) as e:
            validate_manifest(m)
        assert msg in str(e.value), str(e.value)


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
                                                             QualitativeSignal.source_type.isnot(None),
                                                             QualitativeSignal.source_type != 'csv_import').all()   # typed rows (the CSM flag) take the CSV lane
                assert len(engine_sigs) == len(comms), mid
                assert all(s.cg_node_id is not None and s.occurred_at is not None for s in engine_sigs)
                assert all(s.llm_model_version == ORACLE_MODEL_VERSION for s in engine_sigs)
                csv_sigs = QualitativeSignal.query.filter(QualitativeSignal.customer_id == cid,
                                                          QualitativeSignal.source_type == 'csv_import').all()   # the CSV lane, through the engine
                assert {s.signal_type for s in csv_sigs} <= {'csm_risk_flag'}, mid
                nodes = ContextNode.query.filter_by(customer_id=cid, node_type='SIGNAL').all()
                assert all(n.source == 'observed' for n in nodes)
                # every engine signal has a node that names it (properties.signal_id); source_event_id is the source's own ref
                by_signal = {(n.properties or {}).get('signal_id') for n in nodes}
                assert {s.signal_id for s in engine_sigs} <= by_signal

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
            # P1 closed 2026-09-05: with no KPI layer the arc rules use their evidence equivalents,
            # so the expansion story classifies on the evidence alone and says so
            orchard = journeys['Orchard Retail']
            roles = set()
            for s in orchard['leading_vs_trailing']['series']:
                roles |= set(s['roles'])
            assert {'expansion_intent', 'advocacy'} <= roles
            assert orchard['arc']['arc_type'] == 'expansion_champion' and orchard['arc']['evidence_scope'] == 'evidence_only'
            assert orchard['phases_basis'] == 'evidence' and orchard['data_coverage']['kpi_layer'] in ('none', 'not_yet')
            assert reg['wizard_a']['coverage']['classified'] >= 2


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
