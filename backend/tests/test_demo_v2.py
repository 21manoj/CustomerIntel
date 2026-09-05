"""
Generator v2 — communications through the engine (demo/manifest_v2.py,
demo/oracle.py, demo/scorecard.py, demo/generate.register_v2).

  - manifest validation fails loudly: unknown subtype for the vertical,
    unknown source type, unsorted days, missing participants, duplicate
    text on an account, v1 event keys, a KPI-layer account without health
  - a v1 manifest still loads, generates its four CSVs and registers on
    the CSV path
  - a monkeypatched enrich_signal that returns the labels → 100% scorecard,
    journeys built (signals-only manifest: no KPI rows at all)
  - the keyword stub → misses reported as misses, labelled with the stub's
    model version, nothing crashes, journeys still built
  - the backtest on a signals-only tenant: known evals/ gap, documented
"""
import copy
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

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common
_common._flask_app = app

from models import JourneyData, QualitativeSignal, HealthScore, Account
from demo.generate import generate, register, load_manifest, expand_accounts, MANIFESTS_DIR
from demo.manifest_v2 import ManifestError, validate_manifest, is_v2, signals_only, plan_communications, comm_ref
from demo.oracle import oracle_extractor, extractor_override, ORACLE_MODEL_VERSION
from demo.scorecard import build_scorecard

SIGNALS_ONLY = MANIFESTS_DIR / 'demo_signals_only_saas.json'
CHAMPION = MANIFESTS_DIR / 'demo_champion_departure_saas.json'


def _small_v2(**overrides) -> dict:
    m = {
        'version': 2, 'manifest_id': 'unit_v2', 'vertical': 'saas_premium', 'customer_name': 'Unit', 'domain_prefix': 'unit',
        'seed': 1, 'timeline': {'t0': '2026-04-01', 'history_days': 90, 'future_days': 0},
        'accounts': [{
            'source_account_id': 'ACME', 'name': 'Acme', 'arr': 100000, 'renewal_day': 30,
            'health': {'shape': 'flat', 'start': 80},
            'communications': [
                {'day': -40, 'source_type': 'email', 'text': 'Elena has left the company.',
                 'participants': [{'name': 'Elena Rossi', 'title': 'VP Data'}], 'expected_subtypes': ['champion_departure']},
                {'day': -20, 'source_type': 'slack', 'text': 'Half the seats have not logged in since March.',
                 'participants': [{'name': 'Ravi Menon', 'title': 'Data Lead'}], 'expected_subtypes': ['seat_underutilization']},
            ],
            'events': [{'day': 0, 'type': 'contraction', 'amount': -10000, 'linked_communication_index': 0}],
        }],
    }
    m.update(overrides)
    return m


V1_MANIFEST = {
    'manifest_id': 'unit_v1', 'vertical': 'saas_premium', 'customer_name': 'Unit v1 (demo)', 'domain_prefix': 'unit-v1',
    'seed': 11, 'timeline': {'t0': '2026-04-01', 'history_days': 60, 'future_days': 0, 'kpi_cadence_days': 14},
    'accounts': [{
        'source_account_id': 'OLDCO', 'name': 'Oldco', 'arr': 500000, 'renewal_day': 40, 'champion': 'Ann Lee', 'champion_title': 'CIO',
        'health': {'shape': 'decline', 'start': 80, 'end': 55, 'trailing_cross_day': -20, 'noise': 1.0},
        'signals': [{'day': -50, 'type': 'champion_departure', 'sentiment': -0.7, 'stakeholder': 'Ann Lee', 'title': 'CIO',
                     'content': 'Champion left'},
                    {'day': -30, 'type': 'engagement_decline', 'sentiment': -0.5, 'stakeholder': 'Bo Kim', 'title': 'Lead',
                     'content': 'Syncs lapsed'}],
        'crm_flag_day': -10,
        'events': [{'day': 0, 'type': 'contraction', 'amount': -50000, 'linked_signal_index': 0}],
    }],
    'background': {'names': ['Bg One'], 'health_range': [78, 84], 'arr_choices': [300000], 'industries': ['Tech'],
                   'regions': ['EMEA']},
}


# ═══════════════════════════════════════════════════════════════════════
# Validation (no DB)
# ═══════════════════════════════════════════════════════════════════════

class TestManifestValidation:
    def test_shipped_manifests_validate_and_are_v2(self):
        for p in sorted(MANIFESTS_DIR.glob('demo_*.json')):
            m = load_manifest(p)
            assert is_v2(m), p
            for a in m['accounts']:
                assert 'signals' not in a and isinstance(a['communications'], list)

    def test_signals_only_manifest_shape(self):
        m = load_manifest(SIGNALS_ONLY)
        assert signals_only(m) and len(m['accounts']) == 6
        comms = plan_communications(m, expand_accounts(m))
        assert 30 <= len(comms) <= 40
        assert 'kpi_measurements.csv' not in generate(m)

    def test_small_manifest_is_valid(self):
        validate_manifest(_small_v2())

    @pytest.mark.parametrize('mutate, message', [
        (lambda m: m['accounts'][0]['communications'][0]['expected_subtypes'].append('not_a_subtype'), 'not in the saas_premium taxonomy'),
        (lambda m: m['accounts'][0]['communications'][0]['expected_subtypes'].append('reserved_cluster_idle'), 'not in the saas_premium taxonomy'),   # datacenter word
        (lambda m: m['accounts'][0]['communications'][0].update(source_type='carrier_pigeon'), 'source_type'),
        (lambda m: m['accounts'][0]['communications'][0].update(day=-10), 'sorted ascending'),
        (lambda m: m['accounts'][0]['communications'][0].update(participants=[]), 'participants'),
        (lambda m: m['accounts'][0]['communications'][0].update(participants=[{'name': 'X'}]), 'participants'),
        (lambda m: m['accounts'][0]['communications'][1].update(text='  elena HAS left   the company. '), 'duplicate text'),
        (lambda m: m['accounts'][0]['communications'][0].update(text=''), 'text is required'),
        (lambda m: m['accounts'][0]['communications'][0].update(expected_subtypes='champion_departure'), 'must be a list'),
        (lambda m: m['accounts'][0]['events'][0].update(linked_communication_index=5), 'out of range'),
        (lambda m: m['accounts'][0]['events'][0].update(linked_signal_index=0), 'linked_communication_index'),
        (lambda m: m['accounts'][0].pop('health'), 'health curve is required'),
        (lambda m: m['accounts'][0].update(signals=[{'day': -1, 'type': 'advocacy'}]), 'must not carry typed'),
        (lambda m: m.update(kpis='some'), '"kpis"'),
        (lambda m: m['accounts'][0]['communications'][0].update(expected_sentiment=3), 'expected_sentiment'),
    ])
    def test_invalid_manifests_fail_loudly(self, mutate, message):
        m = _small_v2()
        mutate(m)
        with pytest.raises(ManifestError, match=message):
            validate_manifest(m)

    def test_signals_only_needs_no_health(self):
        m = _small_v2(kpis='none')
        m['accounts'][0].pop('health')
        validate_manifest(m)
        files = generate(m)
        assert set(files) == {'account_details.csv', 'outcomes.csv'}

    def test_v1_manifest_still_loads_and_generates_four_csvs(self):
        from utils.csv_upload import _upload_csv_impl
        m = copy.deepcopy(V1_MANIFEST)
        validate_manifest(m)                                   # v1 passes through
        assert not is_v2(m)
        files = generate(m)
        assert set(files) == {'account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'outcomes.csv'}
        for ft, content in files.items():
            r = _upload_csv_impl(0, ft, content, dry_run=True)
            assert r.valid, (ft, r.errors)
        assert 'oldco_sig_1' in files['outcomes.csv'] and 'champion_departure' in files['enhanced_qualitative_signals.csv']

    def test_outcomes_link_to_the_communication_ref_before_registration(self):
        files = generate(_small_v2())
        assert comm_ref('ACME', 0) == 'acme_comm_1' and 'acme_comm_1' in files['outcomes.csv']
        assert 'enhanced_qualitative_signals.csv' not in files          # no CSM flag → no CSV signals at all


# ═══════════════════════════════════════════════════════════════════════
# Oracle + scorecard mechanics (no DB)
# ═══════════════════════════════════════════════════════════════════════

class TestOracleAndScorecard:
    def test_oracle_answers_with_the_labels_and_is_restored_after(self):
        from utils.taxonomy_loader import get_taxonomy
        import signal_engine.enrichment as enrichment
        m = _small_v2()
        comms = plan_communications(m, expand_accounts(m))
        original = enrichment.enrich_signal
        with extractor_override('oracle', comms):
            out = enrichment.enrich_signal('s1', comms[0]['text'], 1, 1, 'saas_premium', taxonomy=get_taxonomy('saas_premium'))
            assert out['intent_signals'] == ['champion_departure'] and out['llm_model_version'] == ORACLE_MODEL_VERSION
            assert out['signals'][0]['sentiment_score'] < 0 and out['requires_review'] is False     # role default: negative
            none = enrichment.enrich_signal('s2', 'Sending the notes.', 1, 1, 'saas_premium')
            assert none['intent_signals'] == []
        assert enrichment.enrich_signal is original

    def test_unknown_extractor_is_refused(self):
        with pytest.raises(ValueError):
            with extractor_override('llama'):
                pass

    def test_auto_is_oracle_without_a_key_and_model_with_one(self, monkeypatch):
        from demo.oracle import resolve_extractor
        monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
        assert resolve_extractor(None) == resolve_extractor('auto') == 'oracle'
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test-not-used')      # never called: resolution only
        assert resolve_extractor('auto') == 'model' and resolve_extractor('stub') == 'stub'

    def test_scorecard_counts_exact_partial_miss_and_roles(self):
        from utils.taxonomy_loader import get_taxonomy
        m = _small_v2()
        comms = plan_communications(m, expand_accounts(m))
        ingested = {
            comms[0]['ref']: {'signal_id': 'a', 'status': 'queued', 'extracted_subtypes': ['champion_departure'],
                              'extracted_roles': ['champion_change'], 'model_version': 'x', 'unclassified': False},
            comms[1]['ref']: {'signal_id': 'b', 'status': 'queued', 'extracted_subtypes': ['usage_decline', 'pricing_concern'],
                              'extracted_roles': ['usage_decline', 'commercial_pressure'], 'model_version': 'x', 'unclassified': False},
        }
        sc = build_scorecard(m, comms, ingested, get_taxonomy('saas_premium'))
        assert (sc['exact'], sc['partial'], sc['miss']) == (1, 0, 1) and sc['hit_rate'] == 0.5
        assert sc['subtype'] == {'tp': 1, 'fp': 2, 'fn': 1, 'precision': 0.333, 'recall': 0.5, 'f1': 0.4}
        assert sc['roles']['usage_decline']['recall'] == 1.0          # right role, wrong subtype: role-level credit
        assert sc['roles']['commercial_pressure']['fp'] == 1 and sc['model_version'] == 'x'


# ═══════════════════════════════════════════════════════════════════════
# Through the DB
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def dbm():
    _assert_isolated_test_db(TEST_DB)
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


class TestRegisterV2:
    def test_monkeypatched_extractor_returns_labels_journeys_built_scorecard_100(self, dbm, monkeypatch, tmp_path):
        """The task's contract: patch signal_engine.enrichment.enrich_signal
        (the pipeline imports it at call time), ask for the engine's own
        extractor ('model' — no key, so whatever enrich_signal now is), and
        the run scores 100% with journeys built from evidence alone."""
        import signal_engine.enrichment as enrichment
        m = load_manifest(SIGNALS_ONLY)
        comms = plan_communications(m, expand_accounts(m))
        monkeypatch.setattr(enrichment, 'enrich_signal', oracle_extractor(comms))
        with app.app_context():
            reg = register(m, generate(m), name_suffix=uuid.uuid4().hex[:6], extractor='model', out_dir=tmp_path)
            assert reg['status'] == 'success' and reg['signals_only']
            sc = reg['scorecard']
            assert sc['communications'] == len(comms) == sc['exact'] and sc['hit_rate'] == 1.0
            assert sc['subtype']['precision'] == sc['subtype']['recall'] == 1.0 and sc['pending'] == 0
            assert sc['model_version'] == ORACLE_MODEL_VERSION
            assert reg['signals']['processed'] == len(comms) and reg['signals']['unclassified'] == 1   # the "notes as promised" one
            cid = reg['customer_id']
            assert JourneyData.query.filter_by(customer_id=cid).count() == 6
            aids = [a.account_id for a in Account.query.filter_by(customer_id=cid).all()]
            assert HealthScore.query.filter(HealthScore.account_id.in_(aids)).count() == 0
            for jd in JourneyData.query.filter_by(customer_id=cid).all():
                series = jd.journey_json['leading_vs_trailing']['series']
                assert series and all(s['kpi_only'] is None for s in series) and any(s['qual'] is not None for s in series)
            arcs = {a['account_name']: a for a in reg['wizard_a']['arcs'].values()}
            assert arcs['Halcyon Health']['arc_type'] == 'exec_sponsor_change'
            self.__class__.signals_only_cid = cid

    def test_backtest_on_a_signals_only_tenant_is_a_known_evals_gap(self, dbm):
        """evals/lead_time_backtest._warning_months does `s['kpi_only'] < at_risk`
        on every month; live months carry kpi_only=None. Not patched here
        (evals/ is outside this change) — recorded so the fix has a test."""
        cid = getattr(self.__class__, 'signals_only_cid', None)
        assert cid is not None
        from evals.lead_time_backtest import run_backtest
        with app.app_context():
            try:
                rep = run_backtest(cid, min_events=1)
            except TypeError as e:
                pytest.xfail(f'evals/lead_time_backtest cannot score a signals-only tenant yet: {e}')
            assert rep['results']['H1_retention']['events'] == 1        # once fixed, Halcyon's contraction is scored

    def test_stub_run_reports_misses_honestly_and_nothing_crashes(self, dbm, tmp_path):
        m = load_manifest(CHAMPION)
        comms = plan_communications(m, expand_accounts(m))
        with app.app_context():
            reg = register(m, generate(m), name_suffix=uuid.uuid4().hex[:6], extractor='stub', out_dir=tmp_path)
            assert reg['status'] == 'success'
            sc = reg['scorecard']
            assert sc['model_version'] == 'stub_keyword_v2' and 'stub' in sc['label']
            assert sc['communications'] == len(comms) and sc['pending'] == 0 and not sc['errors']
            assert sc['miss'] + sc['partial'] > 0 and sc['hit_rate'] < 1.0           # the stub is the floor, reported as such
            assert sc['subtype']['recall'] is not None and sc['subtype']['recall'] < 1.0
            print('\nstub scorecard:', json.dumps({k: sc[k] for k in ('exact', 'partial', 'miss', 'hit_rate', 'unclassified', 'subtype')}))
            cid = reg['customer_id']
            assert JourneyData.query.filter_by(customer_id=cid).count() == 12
            # communications = engine signals that did not arrive as typed CSV rows (the CSM risk flag does)
            sigs = QualitativeSignal.query.filter(QualitativeSignal.customer_id == cid, QualitativeSignal.source_type.isnot(None),
                                                  QualitativeSignal.source_type != 'csv_import').all()
            assert len(sigs) == len(comms) and all(s.cg_node_id for s in sigs)
            assert all(s.llm_model_version == 'stub_keyword_v2' and s.requires_review for s in sigs)
            lines = [json.loads(l) for l in Path(reg['outputs']['labelled']).read_text().splitlines()]
            assert len(lines) == len(comms) and all(l['model_version'] == 'stub_keyword_v2' for l in lines)
            assert any(l['extracted_subtypes'] != l['expected_subtypes'] for l in lines)
            written = json.loads(Path(reg['outputs']['scorecard']).read_text())
            assert written['model_version'] == 'stub_keyword_v2'

    def test_v1_manifest_registers_on_the_csv_path(self, dbm):
        m = copy.deepcopy(V1_MANIFEST)
        with app.app_context():
            reg = register(m, generate(m), name_suffix=uuid.uuid4().hex[:6])
            assert reg['status'] == 'success' and 'scorecard' not in reg
            cid = reg['customer_id']
            assert JourneyData.query.filter_by(customer_id=cid).count() == 2
            assert QualitativeSignal.query.filter(QualitativeSignal.customer_id == cid, QualitativeSignal.source_type.isnot(None),
                                                  QualitativeSignal.source_type != 'csv_import').count() == 0   # no communications; typed rows take the CSV lane
            arcs = {a['account_name']: a for a in reg['wizard_a']['arcs'].values()}
            assert arcs['Oldco']['arc_type'] == 'exec_sponsor_change'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
