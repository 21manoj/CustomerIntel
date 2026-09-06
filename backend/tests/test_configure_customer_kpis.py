"""
configure_customer_kpis — a human directly setting a tenant's KPI/pillar weights, the
missing writer for weights_origin='customer_config' (docs/design/wizard-c-calibration.md
noted it as "listed in both tool registries but has no implementation in this build").

Modeled on test_wizard_c_calibration.py (real Postgres tenants, real recompute), since
approve() there is the closest existing analogue — same shape of write (CustomerConfig →
config_version bump → weights_origin → recompute), but for a human's direct input
instead of an approved calibration proposal.

Covers:
  * a valid weight set is applied: weights_origin='customer_config', config_version
    bumped, customized_by set, health actually recomputed (weight_source stamped,
    stored scores reflect the new weights)
  * overlay semantics: only the fields passed change; the rest of CustomerConfig
    (and any pre-existing kpi_overrides key, e.g. an llm_budget block) survives
  * unknown pillar / KPI codes are rejected with ValueError, checked against the
    tenant's actual vertical catalog, not a hardcoded list
  * bad weights are rejected: negative, a group summing to <= 0, a KPI weight of
    exactly 0 (the scorer treats that as *unset*, not zero — a real footgun)
  * a tenant previously labelled 'wizard_c' can be overwritten by a direct call, and
    Wizard C's own bookkeeping (WeightCalibration rows) is untouched by it
  * kpi_dependencies.json warnings fire for dc2_s (the vertical it's written for) and
    are suppressed for a vertical whose same KPI codes mean something else
  * the tool is registered onboarding+write and keyed like every other write tool
"""
import os
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
from models import Account, CustomerConfig, HealthScore   # noqa: E402


def _accounts_csv(exts):
    rows = ['source_account_id,account_name,industry,region,arr']
    rows += [f'{e},{e} Corp,Software,NA,{100000 + i * 10000}' for i, e in enumerate(exts)]
    return '\n'.join(rows) + '\n'


def _kpi_csv(kpis, codes, exts, month='2026-01-01'):
    lines = ['source_account_id,kpi_code,measured_at,value']
    for e in exts:
        for code in codes:
            v = float(kpis[code]['target']['value'])
            lines.append(f'{e},{code},{month},{v}')
    return '\n'.join(lines) + '\n'


def _make_tenant(vertical: str, codes: list, exts=('A1', 'A2')) -> dict:
    from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
    from utils.vertical_registry import get_kpis
    kpis = get_kpis(vertical)
    tag = uuid.uuid4().hex[:8]
    cid = create_customer(name=f'CK {vertical} {tag}', domain=f'ck-{tag}.test', vertical=vertical,
                          admin_email=f'ck_{tag}@t.test', admin_name='W', data_origin='synthetic_test')['customer_id']
    upload_csv(cid, 'account_details.csv', _accounts_csv(list(exts)))
    upload_csv(cid, 'kpi_measurements.csv', _kpi_csv(kpis, codes, list(exts)))
    res = process_data(cid)
    assert res['status'] == 'success', res
    ids = {a.external_account_id: a.account_id for a in Account.query.filter_by(customer_id=cid).all()}
    return {'cid': cid, 'ids': ids, 'vertical': vertical}


@pytest.fixture(scope='module')
def tenants():
    with app.app_context():
        db.create_all()
        # dc2_s: kpi_dependencies.json is written against this vertical's own codes/names.
        D = _make_tenant('dc2_s', ['P1-KPI1', 'P2-KPI1', 'P3-KPI1', 'P4-KPI1', 'P5-KPI1'])
        # saas_premium: same P1-KPI1/P3-KPI1 codes exist but mean different KPIs entirely —
        # the dependency-warning guard must not fire dc2_s-flavoured text here.
        S = _make_tenant('saas_premium', ['P1-KPI1', 'P1-KPI3', 'P3-KPI1'])
        yield {'D': D, 'S': S}
        db.session.remove()
        db.drop_all()


def _cc(cid):
    return CustomerConfig.query.filter_by(customer_id=cid).one()


def _health(cid):
    ids = [a.account_id for a in Account.query.filter_by(customer_id=cid).all()]
    return HealthScore.query.filter(HealthScore.account_id.in_(ids)).all()


# ── happy path ───────────────────────────────────────────────────────

def test_valid_weights_applied_labelled_and_recomputed(tenants):
    D = tenants['D']
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
        cc = _cc(D['cid'])
        before_version = cc.config_version
        n_health = len(_health(D['cid']))
        out = _configure_customer_kpis_impl(D['cid'], pillar_weights={'P1': 0.5, 'P2': 0.3, 'P3': 0.2},
                                            customized_by='cro@tenant.test')
        assert out['weights_origin'] == 'customer_config' and out['customized_by'] == 'cro@tenant.test'
        assert out['config_version'] != before_version and out['fields_changed'] == ['pillar_weights']
        cc = _cc(D['cid'])
        assert cc.pillar_weights == {'P1': 0.5, 'P2': 0.3, 'P3': 0.2} and cc.weights_origin == 'customer_config'
        assert cc.customized_by == 'cro@tenant.test'
        rec = out['recompute']
        assert rec['mode'] == 'full_recalc' and rec['status'] == 'success' and rec['run_id']
        assert rec['health_rows_customer_config'] == n_health
        rows = _health(D['cid'])
        assert {h.weight_source for h in rows} == {'customer_config'}
        assert all(h.pillar_weights == {'P1': 0.5, 'P2': 0.3, 'P3': 0.2} for h in rows)


def test_customized_by_defaults_to_actor_label_when_omitted(tenants):
    D = tenants['D']
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
        out = _configure_customer_kpis_impl(D['cid'], enabled_kpis=['P1-KPI1', 'P2-KPI1', 'P3-KPI1', 'P4-KPI1', 'P5-KPI1'])
        assert out['customized_by'] == 'local'          # current_actor() over stdio: {'label': 'local'}
        assert out['fields_changed'] == ['enabled_kpis']


# ── overlay semantics ────────────────────────────────────────────────

def test_overlay_only_touches_fields_passed_and_merges_kpi_overrides(tenants):
    D = tenants['D']
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
        cc = _cc(D['cid'])
        cc.kpi_overrides = {'llm_budget': {'daily_calls': 50}}   # pre-existing, unrelated to this call
        db.session.commit()
        pw_before = dict(_cc(D['cid']).pillar_weights)
        out = _configure_customer_kpis_impl(D['cid'], kpi_overrides={'P1-KPI1': {'target': 95}})
        assert out['fields_changed'] == ['kpi_overrides']
        cc = _cc(D['cid'])
        assert cc.pillar_weights == pw_before                                       # untouched
        assert cc.kpi_overrides == {'llm_budget': {'daily_calls': 50}, 'P1-KPI1': {'target': 95}}   # merged, not replaced
        # calling again with a different KPI key still keeps llm_budget and the first override
        _configure_customer_kpis_impl(D['cid'], kpi_overrides={'P2-KPI1': {'target': 80}})
        cc = _cc(D['cid'])
        assert cc.kpi_overrides == {'llm_budget': {'daily_calls': 50}, 'P1-KPI1': {'target': 95}, 'P2-KPI1': {'target': 80}}


# ── validation ───────────────────────────────────────────────────────

def test_no_fields_raises(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='at least one of'):
        _configure_customer_kpis_impl(tenants['D']['cid'])


def test_missing_customer_config_raises():
    with app.app_context():
        from models import Customer
        from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
        c = Customer(customer_name='no config', email=f'nc_{uuid.uuid4().hex[:6]}@t.test')
        db.session.add(c); db.session.commit()
        with pytest.raises(ValueError, match='no CustomerConfig row'):
            _configure_customer_kpis_impl(c.customer_id, pillar_weights={'P1': 1.0})


def test_unknown_pillar_code_rejected(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='unknown pillar codes'):
        _configure_customer_kpis_impl(tenants['D']['cid'], pillar_weights={'P1': 0.5, 'P9': 0.5})


def test_unknown_kpi_code_rejected_in_enabled_kpis_and_kpi_weights(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    cid = tenants['D']['cid']
    with app.app_context():
        with pytest.raises(ValueError, match='unknown KPI codes'):
            _configure_customer_kpis_impl(cid, enabled_kpis=['P1-KPI1', 'NOPE-1'])
        with pytest.raises(ValueError, match='unknown KPI code'):
            _configure_customer_kpis_impl(cid, kpi_weights={'P1': {'NOPE-1': 1.0}})


def test_kpi_weight_pillar_mismatch_rejected(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='belongs to pillar'):
        _configure_customer_kpis_impl(tenants['D']['cid'], kpi_weights={'P1': {'P2-KPI1': 1.0}})


def test_negative_weight_rejected(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='negative'):
        _configure_customer_kpis_impl(tenants['D']['cid'], pillar_weights={'P1': -0.1, 'P2': 1.1})


def test_group_summing_to_zero_rejected(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='sums to <= 0'):
        _configure_customer_kpis_impl(tenants['D']['cid'], pillar_weights={'P1': 0.0, 'P2': 0.0})


def test_zero_kpi_weight_rejected_as_footgun(tenants):
    """A KPI weight of 0 is silently treated as *unset* by the scorer (falls back to 1.0),
    the opposite of what a caller asking for 0 almost certainly means."""
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='treated as \\*unset\\*'):
        _configure_customer_kpis_impl(tenants['D']['cid'], kpi_weights={'P1': {'P1-KPI1': 0}})


def test_non_numeric_weight_rejected(tenants):
    from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
    with app.app_context(), pytest.raises(ValueError, match='is not a number'):
        _configure_customer_kpis_impl(tenants['D']['cid'], pillar_weights={'P1': 'a lot', 'P2': 1.0})


# ── wizard_c interplay: overwrite doesn't corrupt Wizard C's own bookkeeping ──

def test_overwrites_wizard_c_label_without_touching_calibration_rows(tenants):
    from models import WeightCalibration
    D = tenants['D']
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
        cc = _cc(D['cid'])
        cc.pillar_weights, cc.kpi_weights = {'P1': 0.6, 'P2': 0.4}, {'P1': {'P1-KPI1': 1.0}}
        cc.weights_origin, cc.customized_by, cc.config_version = 'wizard_c', 'wizard_c:999', '1.3'
        db.session.commit()
        before_calibrations = WeightCalibration.query.filter_by(customer_id=D['cid']).count()
        out = _configure_customer_kpis_impl(D['cid'], pillar_weights={'P1': 0.2, 'P2': 0.3, 'P3': 0.5},
                                            customized_by='ops@tenant.test')
        assert out['weights_origin'] == 'customer_config' and out['customized_by'] == 'ops@tenant.test'
        cc = _cc(D['cid'])
        assert cc.weights_origin == 'customer_config' and cc.customized_by == 'ops@tenant.test'
        assert cc.pillar_weights == {'P1': 0.2, 'P2': 0.3, 'P3': 0.5}
        # Wizard C's own table is untouched by this overwrite
        assert WeightCalibration.query.filter_by(customer_id=D['cid']).count() == before_calibrations
        rows = _health(D['cid'])
        assert {h.weight_source for h in rows} == {'customer_config'}


# ── dependency warnings: dc2_s-scoped, not a false positive elsewhere ─

def test_dependency_warnings_fire_for_dc2s_only(tenants):
    D, S = tenants['D'], tenants['S']
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import _configure_customer_kpis_impl
        out = _configure_customer_kpis_impl(D['cid'], pillar_weights={'P1': 0.34, 'P2': 0.33, 'P3': 0.33})
        assert any('P4' in w or 'P5' in w for w in out['warnings']) and out['warnings']
        out_s = _configure_customer_kpis_impl(S['cid'], pillar_weights={'P1': 1.0})
        assert out_s['warnings'] == []          # same P1/P3 codes exist on saas_premium but mean different KPIs — no dc2_s warning leaks in


# ── registration: onboarding + write scope, keyed like every other write tool ─

def test_tool_is_registered_onboarding_and_write_scoped():
    from mcp_server.onboarding_tool_registry import ONBOARDING_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    assert 'configure_customer_kpis' in ONBOARDING_TOOLS
    assert 'configure_customer_kpis' in WRITE_TOOLS


def test_onboarding_tool_stays_frictionless_but_a_present_read_key_needs_write_scope(tenants, monkeypatch):
    """configure_customer_kpis is BOTH an onboarding tool (frictionless — no key needed,
    same as upload_csv) AND a write tool: over HTTP with no key it's allowed (prospect
    flow), but a key that IS present must carry write scope, exactly like every other
    onboarding write tool (upload_csv, process_data, enable_features)."""
    import mcp_server.auth as auth
    from fastmcp.exceptions import ToolError
    from api_key_service import generate_api_key
    monkeypatch.setenv('MCP_TRANSPORT', 'http')
    monkeypatch.setattr(auth, 'MCP_AUTH_REQUIRED', True)
    cid = tenants['D']['cid']
    with app.app_context():
        read_key, _rec = generate_api_key(cid, created_by=None, name='r', scopes=['read'])
        write_key, _rec2 = generate_api_key(cid, created_by=None, name='w', scopes=['write'])
    tok = auth._current_api_key_var.set('')
    try:
        with app.app_context():
            assert auth.require_auth_if_key_present('configure_customer_kpis', cid) is None   # no key: frictionless
    finally:
        auth._current_api_key_var.reset(tok)
    tok = auth._current_api_key_var.set(read_key)
    try:
        with app.app_context():
            with pytest.raises(ToolError, match='write'):
                auth.require_auth_if_key_present('configure_customer_kpis', cid)
    finally:
        auth._current_api_key_var.reset(tok)
    tok = auth._current_api_key_var.set(write_key)
    try:
        with app.app_context():
            rec = auth.require_auth_if_key_present('configure_customer_kpis', cid)
            assert rec is not None and rec.customer_id == cid
    finally:
        auth._current_api_key_var.reset(tok)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
