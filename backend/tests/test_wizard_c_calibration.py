"""
Wizard C — weight calibration from logged outcomes (docs/design/wizard-c-calibration.md), end to end on real
Postgres tenants on two verticals (saas_premium with a tier default, datacenter_v1 on the bare catalog):
  * config loads; a missing key raises
  * the outcome gate: below it → insufficient_outcomes with the counts, NO row, still a WizardRun
  * a proposal: labels from OUTCOME buckets, per-KPI / per-pillar evidence with counts, effect, direction,
    confidence; a discriminating KPI goes up, a flat one is untouched; weights sum to 1; the before/after is
    computed and NOT written; the proposal is audited with the key kind
  * a second proposal supersedes the open one (audited)
  * approval writes CustomerConfig (customized_by='wizard_c:<id>', config_version bumped, weights_origin='wizard_c'),
    recomputes through the pipeline, every health row carries weight_source='wizard_c', and the stored scores
    equal the proposal's 'after' (one number, two paths)
  * rejection changes nothing; a decided proposal cannot be decided again
  * weight_source labels: 'vertical_default' after create_customer, 'customer_config' for a hand-set config
  * process_data never runs Wizard C; tools are keyed (write scope for approve/reject); routes pinned + registered
  * the 0003 back-fill labels existing rows by who set them
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
from models import Account, CustomerConfig, HealthScore, ToolAuditLog, WeightCalibration, WizardRun   # noqa: E402

MONTHS = ('2026-01-01', '2026-02-01', '2026-03-01', '2026-04-01')
OUTCOME_DAY = '2026-05-10'          # inside the 120-day window after the KPI months


# ── fixture tenants ───────────────────────────────────────────────────

def _val(kdef: dict, kind: str) -> float:
    """A KPI value squarely inside the catalog's healthy / critical band, or exactly on target ('flat')."""
    r = kdef['ranges']
    if kind == 'flat':
        return float(kdef['target']['value'])
    band = r['healthy'] if kind == 'healthy' else r['critical']
    return round((float(band['min']) + float(band['max'])) / 2, 2)


def _kpi_csv(kpis: dict, plan: dict, good: list, bad: list) -> str:
    """plan: {kpi_code: 'discriminates' | 'flat'}; good accounts get healthy values on the discriminating KPIs, bad get critical."""
    lines = ['source_account_id,kpi_code,measured_at,value']
    for m in MONTHS:
        for ext in good + bad:
            for code, kind in plan.items():
                if kind == 'flat':
                    v = _val(kpis[code], 'flat')
                else:
                    v = _val(kpis[code], 'healthy' if ext in good else 'critical')
                lines.append(f'{ext},{code},{m},{v}')
    return '\n'.join(lines) + '\n'


def _accounts_csv(exts: list) -> str:
    rows = ['source_account_id,account_name,industry,region,arr']
    rows += [f'{e},{e} Corp,Software,NA,{100000 + i * 10000}' for i, e in enumerate(exts)]
    return '\n'.join(rows) + '\n'


def _make_tenant(vertical: str, plan: dict, good: list, bad: list, extra_no_kpi: list = ()) -> dict:
    from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
    from utils.vertical_registry import get_kpis
    kpis = get_kpis(vertical)
    for code in plan:
        assert code in kpis and kpis[code].get('ranges'), code
    tag = uuid.uuid4().hex[:8]
    cid = create_customer(name=f'WC {vertical} {tag}', domain=f'wc-{tag}.test', vertical=vertical, admin_email=f'wc_{tag}@t.test',
                          admin_name='W', data_origin='synthetic_test')['customer_id']
    upload_csv(cid, 'account_details.csv', _accounts_csv(good + bad + list(extra_no_kpi)))
    upload_csv(cid, 'kpi_measurements.csv', _kpi_csv(kpis, plan, good, bad))
    res = process_data(cid)
    assert res['status'] == 'success', res
    ids = {a.external_account_id: a.account_id for a in Account.query.filter_by(customer_id=cid).all()}
    return {'cid': cid, 'ids': ids, 'good': good, 'bad': bad, 'plan': plan, 'vertical': vertical}


def _log(cid, aid, otype, day=OUTCOME_DAY, revenue=None):
    from journeys.outcomes import log_outcome
    return log_outcome(cid, aid, otype, day, revenue=revenue, note='test outcome', rebuild=False)


@pytest.fixture(scope='module')
def tenants():
    with app.app_context():
        db.create_all()
        # A — saas_premium, starter tier (4 pillars at 0.25 written by create_customer → 'vertical_default').
        # P1-KPI1 and P3-KPI1 separate good from bad; the rest sit on target for everyone.
        A = _make_tenant('saas_premium', {'P1-KPI1': 'discriminates', 'P1-KPI3': 'flat', 'P2-KPI1': 'flat', 'P3-KPI1': 'discriminates',
                                          'P5-KPI1': 'flat', 'P5-KPI2': 'flat'},
                         good=['G1', 'G2', 'G3', 'G4'], bad=['B1', 'B2', 'B3', 'B4'], extra_no_kpi=['N1'])
        for e in A['good']:
            _log(A['cid'], A['ids'][e], 'renewal_secured', revenue=120000)
        for e in A['good'][:2]:
            _log(A['cid'], A['ids'][e], 'expansion_closed', '2026-05-15', revenue=30000)
        for e in A['bad']:
            _log(A['cid'], A['ids'][e], 'churn_lost', revenue=100000)
        for e in A['bad'][:2]:
            _log(A['cid'], A['ids'][e], 'renewal_at_risk', '2026-05-12')
        _log(A['cid'], A['ids']['N1'], 'churn_lost', revenue=50000)          # an outcome with no KPI row before it
        # B — datacenter_v1, no tier: pillar_weights NULL → the catalog's weight_l2 ('catalog')
        B = _make_tenant('datacenter_v1', {'P1-KPI1': 'discriminates', 'P2-KPI1': 'discriminates', 'P3-KPI1': 'flat',
                                           'P5-KPI1': 'flat', 'P6-KPI1': 'flat'},
                         good=['G1', 'G2', 'G3'], bad=['B1', 'B2', 'B3'])
        for e in B['good']:
            _log(B['cid'], B['ids'][e], 'renewal_secured', revenue=900000)
            _log(B['cid'], B['ids'][e], 'expansion_closed', '2026-05-20', revenue=200000)
        for e in B['bad']:
            _log(B['cid'], B['ids'][e], 'churn_lost', revenue=800000)
            _log(B['cid'], B['ids'][e], 'contraction', '2026-05-20', revenue=100000)
        # C — saas_premium with two outcomes: the gate stays shut
        C = _make_tenant('saas_premium', {'P1-KPI1': 'discriminates', 'P3-KPI1': 'flat'}, good=['G1', 'G2'], bad=['B1'])
        _log(C['cid'], C['ids']['G1'], 'renewal_secured')
        _log(C['cid'], C['ids']['B1'], 'churn_lost')
        yield {'A': A, 'B': B, 'C': C}
        db.session.remove()
        db.drop_all()


def _rows(cid):
    return WeightCalibration.query.filter_by(customer_id=cid).order_by(WeightCalibration.id).all()


def _audit(cid, transition):
    return ToolAuditLog.query.filter_by(customer_id=cid, tool=f'calibration.{transition}').order_by(ToolAuditLog.id).all()


def _health(cid):
    ids = [a.account_id for a in Account.query.filter_by(customer_id=cid).all()]
    return HealthScore.query.filter(HealthScore.account_id.in_(ids)).all()


# ── config ────────────────────────────────────────────────────────────

def test_config_loads_and_a_missing_key_raises(tmp_path, monkeypatch):
    from wizards import wizard_c_calibration as wc
    cfg = wc.config()
    assert cfg['gate']['min_outcomes_total'] > 0 and set(cfg['label_buckets']) == {'negative', 'positive'}
    broken = dict(cfg); broken.pop('gate')
    p = tmp_path / 'wizard_c.json'; p.write_text(json.dumps(broken))
    monkeypatch.setattr(wc, 'CONFIG_PATH', str(p)); wc.reset_cache()
    with pytest.raises(wc.WizardCConfigError, match='gate'):
        wc.config()
    monkeypatch.undo(); wc.reset_cache()
    assert wc.config()['gate']


# ── labels after create_customer ──────────────────────────────────────

def test_create_customer_tier_default_is_labelled_vertical_default(tenants):
    A, B = tenants['A'], tenants['B']
    with app.app_context():
        cc = CustomerConfig.query.filter_by(customer_id=A['cid']).one()
        assert cc.weights_origin == 'vertical_default' and set(cc.pillar_weights) == {'P1', 'P2', 'P3', 'P5'}
        assert {h.weight_source for h in _health(A['cid'])} == {'vertical_default'}
        ccb = CustomerConfig.query.filter_by(customer_id=B['cid']).one()
        assert ccb.pillar_weights is None and ccb.weights_origin is None
        assert {h.weight_source for h in _health(B['cid'])} == {'catalog'}


# ── the gate ──────────────────────────────────────────────────────────

def test_below_the_gate_is_insufficient_outcomes_with_counts_and_no_row(tenants):
    C = tenants['C']
    with app.app_context():
        from wizards.wizard_c_calibration import propose, config
        out = propose(C['cid'])
        assert out['status'] == 'insufficient_outcomes' and out['proposal_id'] is None
        assert out['outcome_counts']['total'] == 2 and out['outcome_counts']['positive'] == 1 and out['outcome_counts']['negative'] == 1
        assert out['outcome_counts']['accounts'] == 2 and out['gate'] == config()['gate']
        assert any('min_outcomes_total' in s for s in out['short_by']) and any('min_outcomes_per_class' in s for s in out['short_by'])
        assert 'never health scores' in out['note']
        assert _rows(C['cid']) == [] and _audit(C['cid'], 'propose') == []
        # through the wizards' entry point: a completed WizardRun that says the same
        from mcp_server.cs_pulse_onboarding import trigger_wizard
        t = trigger_wizard(C['cid'], 'c')
        assert t['status'] == 'completed' and t['result_summary']['status'] == 'insufficient_outcomes'
        run = WizardRun.query.filter_by(customer_id=C['cid'], wizard='c').one()
        assert run.results['status'] == 'insufficient_outcomes' and run.results['outcome_counts']['total'] == 2


def test_unknown_vertical_fails_closed():
    with app.app_context():
        from models import Customer
        from wizards.wizard_c_calibration import propose
        c = Customer(customer_name='no config', email=f'nc_{uuid.uuid4().hex[:6]}@t.test')
        db.session.add(c); db.session.commit()
        with pytest.raises(ValueError, match='Cannot resolve vertical'):
            propose(c.customer_id)


# ── a proposal ────────────────────────────────────────────────────────

def test_proposal_cites_outcomes_and_writes_nothing_to_health(tenants):
    A = tenants['A']
    with app.app_context():
        from wizards.wizard_c_calibration import propose, get_calibration
        out = propose(A['cid'])
        assert out['status'] == 'proposed' and out['proposal_id']
        c = out['outcome_counts']
        assert c == {**c, 'total': 13, 'positive': 6, 'negative': 7, 'accounts': 9, 'unfeatured': 1, 'unbucketed': 0}
        assert c['by_bucket'] == {'protected': 4, 'expansion': 2, 'lost': 5, 'at_risk': 2}
        assert len(out['outcome_node_ids']) == 13
        ev = out['evidence']
        dau = ev['kpis']['P1-KPI1']
        assert dau['n_pos'] == 6 and dau['n_neg'] == 6 and dau['accounts_pos'] == 4 and dau['accounts_neg'] == 4
        assert dau['direction'] == 'discriminates' and dau['confidence'] == 'high' and dau['effect_pts'] > 20 and dau['d'] > 0
        assert dau['factor'] > 1.0 and dau['proposed_weight'] > dau['current_weight']
        flat = ev['kpis']['P5-KPI1']
        assert flat['direction'] == 'flat' and flat['confidence'] == 'none' and flat['factor'] == 1.0 and flat['effect_pts'] == 0.0
        assert ev['pillars']['P1']['confidence'] == 'high' and ev['pillars']['P1']['proposed_weight'] > ev['pillars']['P1']['current_weight']
        assert ev['pillars']['P5']['direction'] == 'flat' and ev['pillars']['P5']['proposed_weight'] < ev['pillars']['P5']['current_weight']
        pw, kw = out['proposed']['pillar_weights'], out['proposed']['kpi_weights']
        assert set(pw) == {'P1', 'P2', 'P3', 'P5'} and abs(sum(pw.values()) - 1.0) < 1e-6
        assert all(abs(sum(ws.values()) - 1.0) < 1e-6 for ws in kw.values()) and set(kw['P1']) == {'P1-KPI1', 'P1-KPI3'}
        assert out['current']['pillar_weights'] == {'P1': 0.25, 'P2': 0.25, 'P3': 0.25, 'P5': 0.25} and out['current']['origin'] == 'vertical_default'
        imp = out['impact']
        assert imp['summary']['accounts_scored'] == 8 and imp['summary']['accounts_unscored'] == 1
        assert all(a['stored_matches_recompute'] for a in imp['accounts']) and imp['summary']['stored_vs_recompute_mismatches'] == 0
        assert all(a['month'] == '2026-04-01' and a['band_before'] and a['band_after'] for a in imp['accounts'])
        # nothing written: the config and every health row are as they were
        cc = CustomerConfig.query.filter_by(customer_id=A['cid']).one()
        assert cc.weights_origin == 'vertical_default' and cc.pillar_weights == {'P1': 0.25, 'P2': 0.25, 'P3': 0.25, 'P5': 0.25} and cc.kpi_weights is None
        assert {h.weight_source for h in _health(A['cid'])} == {'vertical_default'}
        row = _rows(A['cid'])[0]
        assert row.state == 'proposed' and row.proposed_by == 'local' and row.method_version and row.catalog_version
        a = _audit(A['cid'], 'propose')
        assert len(a) == 1 and a[0].surface == 'wizard_c' and a[0].key_kind == 'local' and 'outcomes=13' in a[0].detail
        g = get_calibration(A['cid'])
        assert g['proposal']['proposal_id'] == row.id and g['in_force']['origin'] == 'vertical_default' and g['count'] == 1


def test_a_second_proposal_supersedes_the_open_one(tenants):
    A = tenants['A']
    with app.app_context():
        from wizards.wizard_c_calibration import propose
        first = _rows(A['cid'])[0]
        out = propose(A['cid'])
        assert out['status'] == 'proposed' and out['superseded'] == [first.id]
        db.session.refresh(first)
        assert first.state == 'superseded' and first.superseded_by == out['proposal_id']
        assert _audit(A['cid'], 'superseded')[-1].detail == f'#{first.id} superseded by #{out["proposal_id"]}'
        assert [r.state for r in _rows(A['cid'])] == ['superseded', 'proposed']


# ── approve ───────────────────────────────────────────────────────────

def test_approval_writes_weights_recomputes_and_labels_wizard_c(tenants):
    A = tenants['A']
    with app.app_context():
        from wizards.wizard_c_calibration import approve
        row = [r for r in _rows(A['cid']) if r.state == 'proposed'][0]
        proposed_pw, proposed_kw, impact = dict(row.proposed_pillar_weights), dict(row.proposed_kpi_weights), dict(row.impact)
        n_health = len(_health(A['cid']))
        out = approve(A['cid'], row.id, note='CFO review 2026-09: adoption and sentiment carry the renewals')
        assert out['state'] == 'approved' and out['decided_by'] == 'local' and out['decision_note'].startswith('CFO review')
        cc = CustomerConfig.query.filter_by(customer_id=A['cid']).one()
        assert cc.pillar_weights == proposed_pw and cc.kpi_weights == proposed_kw
        assert cc.customized_by == f'wizard_c:{row.id}' and cc.config_version == '1.1' == out['applied_config_version']
        assert cc.weights_origin == 'wizard_c'
        rec = out['recompute']
        assert rec['mode'] == 'full_recalc' and rec['status'] == 'success' and rec['run_id'] and rec['health_rows_wizard_c'] == n_health
        rows = _health(A['cid'])
        assert {h.weight_source for h in rows} == {'wizard_c'} and all(h.pillar_weights == proposed_pw for h in rows)
        assert all(h.kpi_weights['P1-KPI1'] == proposed_kw['P1']['P1-KPI1'] for h in rows)
        # one number, two paths: the stored latest month equals the proposal's 'after'
        stored = {h.account_id: float(h.health_score) for h in rows if h.measurement_month.isoformat() == '2026-04-01'}
        for a in impact['accounts']:
            assert abs(stored[a['account_id']] - a['after']) <= 0.011, (a, stored[a['account_id']])
        assert [x.tool for x in _audit(A['cid'], 'approve')] == ['calibration.approve'] and _audit(A['cid'], 'approve')[0].key_kind == 'local'
        assert 'wizard_c_rows=' in _audit(A['cid'], 'recompute')[-1].detail
        with pytest.raises(ValueError, match='only a proposed one'):
            approve(A['cid'], row.id)
        from mcp_server.cs_pulse_onboarding import process_data
        assert 'health_scores_auto_0_written' in process_data(A['cid'])['steps_completed']      # immutable months stay wizard_c
        assert {h.weight_source for h in _health(A['cid'])} == {'wizard_c'}


# ── reject ────────────────────────────────────────────────────────────

def test_reject_changes_nothing_and_is_final(tenants):
    B = tenants['B']
    with app.app_context():
        from wizards.wizard_c_calibration import propose, reject, approve
        out = propose(B['cid'])
        assert out['status'] == 'proposed' and out['current']['origin'] == 'catalog' and out['vertical'] == 'datacenter_v1'
        assert set(out['proposed']['pillar_weights']) == {'P1', 'P2', 'P3', 'P4', 'P5', 'P6'}      # every catalog pillar is scored on the bare catalog
        assert out['evidence']['kpis']['P1-KPI1']['confidence'] == 'high' and out['evidence']['kpis']['P2-KPI1']['direction'] == 'discriminates'
        assert 'P4-KPI1' not in out['evidence']['kpis'] and out['evidence']['pillars']['P4']['direction'] == 'no_data'    # never measured: no data, no claim
        r = reject(B['cid'], out['proposal_id'], note='not yet — wait for the Q4 renewals')
        assert r['state'] == 'rejected' and r['decided_by'] == 'local' and r['decision_note'].startswith('not yet')
        cc = CustomerConfig.query.filter_by(customer_id=B['cid']).one()
        assert cc.pillar_weights is None and cc.kpi_weights is None and cc.weights_origin is None and cc.customized_by is None
        assert {h.weight_source for h in _health(B['cid'])} == {'catalog'}
        assert _audit(B['cid'], 'reject')[-1].detail.startswith(f"#{out['proposal_id']} by local: not yet")
        with pytest.raises(ValueError, match='only a proposed one'):
            approve(B['cid'], out['proposal_id'])
        with pytest.raises(ValueError, match='only a proposed one'):
            reject(B['cid'], out['proposal_id'])


# ── the customer_config label ─────────────────────────────────────────

def test_hand_set_weights_are_labelled_customer_config(tenants):
    B = tenants['B']
    from mcp_server.cs_pulse_onboarding import process_data
    with app.app_context():
        cc = CustomerConfig.query.filter_by(customer_id=B['cid']).one()
        cc.pillar_weights, cc.weights_origin, cc.customized_by = {'P1': 0.5, 'P2': 0.5}, 'customer_config', 'ops@tenant'
        db.session.commit()
    process_data(B['cid'], mode='full_recalc')
    with app.app_context():
        rows = _health(B['cid'])
        assert {h.weight_source for h in rows} == {'customer_config'} and all(set(h.pillar_weights) <= {'P1', 'P2'} for h in rows)
        cc = CustomerConfig.query.filter_by(customer_id=B['cid']).one()
        cc.weights_origin = None            # a direct write with no origin: a person did it
        db.session.commit()
    process_data(B['cid'], mode='full_recalc')
    with app.app_context():
        assert {h.weight_source for h in _health(B['cid'])} == {'customer_config'}
        cc = CustomerConfig.query.filter_by(customer_id=B['cid']).one()
        cc.pillar_weights, cc.customized_by = None, None
        db.session.commit()


# ── never from process_data; keyed; routes ────────────────────────────

def test_process_data_never_runs_wizard_c(tenants):
    C = tenants['C']
    from mcp_server.cs_pulse_onboarding import process_data
    with app.app_context():
        before = len(_rows(C['cid'])) + WizardRun.query.filter_by(customer_id=C['cid'], wizard='c').count()
    res = process_data(C['cid'], mode='full_recalc')
    assert res['status'] == 'success' and not any('wizard_c' in s or 'calibration' in s for s in res['steps_completed'])
    with app.app_context():
        assert len(_rows(C['cid'])) + WizardRun.query.filter_by(customer_id=C['cid'], wizard='c').count() == before
    pipeline = (BACKEND / 'mcp_server' / 'process_data_pipeline.py').read_text()
    impl = (BACKEND / 'mcp_server' / 'cs_pulse_onboarding.py').read_text().split('def _process_data_impl')[1].split('\n@mcp.tool')[0]
    assert 'wizard_c' not in pipeline and 'calibration' not in pipeline and 'wizard_c' not in impl


def test_tools_are_keyed_and_approve_reject_need_write_scope(monkeypatch):
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    import mcp_server.auth as auth
    src = (BACKEND / 'mcp_server' / 'cs_pulse_wizard_c.py').read_text()
    registered = set(re.findall(r"_require_auth_if_key_present\('([a-z_]+)'", src))
    assert registered == {'get_calibration', 'approve_calibration', 'reject_calibration'}
    assert registered <= KEYED_TOOLS and not (registered & ONBOARDING_TOOLS)
    assert {'approve_calibration', 'reject_calibration'} <= WRITE_TOOLS and 'get_calibration' not in WRITE_TOOLS
    monkeypatch.setenv('MCP_TRANSPORT', 'http'); monkeypatch.setattr(auth, 'MCP_AUTH_REQUIRED', True)
    from fastmcp.exceptions import ToolError
    tok = auth._current_api_key_var.set('')
    try:
        with app.app_context():
            for tool in registered:
                with pytest.raises(ToolError, match='requires an API key'):
                    auth.require_auth_if_key_present(tool, 1)
    finally:
        auth._current_api_key_var.reset(tok)


def test_routes_are_pinned_and_registered():
    from wizards.wizard_c_http import ROUTES
    assert ROUTES == ('/api/calibrations', '/api/calibrations/propose', '/api/calibrations/{id}/approve', '/api/calibrations/{id}/reject')
    server = (BACKEND / 'server.py').read_text()
    assert 'import mcp_server.cs_pulse_wizard_c' in server and 'register_calibration_routes(mcp)' in server
    from mcp_server.cs_pulse_onboarding import _WIZARDS
    assert 'c' in _WIZARDS


# ── the migration back-fill ───────────────────────────────────────────

def test_0003_backfills_weights_origin_by_who_set_them():
    from sqlalchemy import create_engine, text
    from alembic import command
    from utils.schema import _config
    name = TEST_DB.rsplit('/', 1)[-1].split('?', 1)[0] + '_wizardc_mig'
    admin = create_engine(TEST_DB, isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')); conn.execute(text(f'CREATE DATABASE {name}'))
    eng = create_engine(TEST_DB.rsplit('/', 1)[0] + '/' + name)
    try:
        command.upgrade(_config(eng), '0002_reconcile_pre_alembic')
        with eng.begin() as conn:
            for i, (cb, pw) in enumerate([('ops@tenant', '{"P1": 1}'), (None, '{"P1": 1}'), (None, None)]):
                conn.execute(text("INSERT INTO customers (customer_id, customer_name, email) VALUES (:i, :n, :e)"), {'i': i + 1, 'n': f'c{i}', 'e': f'c{i}@t'})
                conn.execute(text("INSERT INTO customer_configs (customer_id, vertical, customized_by, pillar_weights) VALUES (:i, 'saas_premium', :cb, :pw)"),
                             {'i': i + 1, 'cb': cb, 'pw': pw})
        command.upgrade(_config(eng), 'head')
        with eng.connect() as conn:
            got = dict(conn.execute(text('SELECT customer_id, weights_origin FROM customer_configs ORDER BY customer_id')).all())
        assert got == {1: 'customer_config', 2: 'vertical_default', 3: None}
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'))
