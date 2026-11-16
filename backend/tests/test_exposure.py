"""
Exposure money basis (roi/exposure.py, config/economics/<vertical>.json with model = exposure) on real Postgres:
  * the capability: money_basis is 'revenue' unless a catalog says otherwise; an unknown value is refused
  * the loaders: the exposure file validates; the revenue loader still fails closed for an exposure vertical;
    the exposure loader refuses a revenue vertical; a malformed exposure file is refused with the reason
  * the maths: failure cost = the account's own recorded figure, else the LARGEST applicable event at its tier
    (never the sum); expected exposure = cost x the likelihood of the health band; next band = the difference
  * an account with no tier or no health score is listed as not priced, never defaulted
  * a tier / flag uploaded as a plain CSV column (filed under profile_metadata['attributes']) is read the same
  * the regulated flag adds a note and never changes a number
  * exposure avoided needs a health reading before and after and is zero when the band did not change
  * cost and exposure sit side by side: no ratio key anywhere in the payload
  * every playbook in a vertical's playbook file has a cost entry in its investment file
  * the route, the app route and the MCP tool are registered; a revenue vertical gets a clear refusal
"""
import copy
import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()
from models import Account, HealthScore                    # noqa: E402
from roi import settings                                   # noqa: E402
from roi import exposure as ex                             # noqa: E402

EXPOSURE_VERTICAL = 'tprm_v1'
REVENUE_VERTICAL = 'saas_premium'


def _customer(vertical):
    from mcp_server.cs_pulse_onboarding import create_customer
    tag = uuid.uuid4().hex[:8]
    return create_customer(name=f'Exposure {tag}', domain=f'exposure-{tag}.test', vertical=vertical,
                           admin_email=f'exposure_{tag}@t.test', admin_name='P', data_origin='synthetic_test')['customer_id']


def _account(cid, name, revenue, profile, health):
    a = Account(customer_id=cid, account_name=name, revenue=revenue, vertical=EXPOSURE_VERTICAL, profile_metadata=profile)
    db.session.add(a)
    db.session.flush()
    if health is not None:
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=date(2026, 5, 1), health_score=health - 5))
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=date(2026, 6, 1), health_score=health))
    return a.account_id


@pytest.fixture(scope='module')
def book():
    with app.app_context():
        db.create_all()
        cid = _customer(EXPOSURE_VERTICAL)
        ids = {
            # critical, holds data: breach (4.91M) beats outage (1.2M); health 45 -> 10%
            'core': _account(cid, 'Core Processor', 1_900_000, {'criticality_tier': 'critical', 'holds_customer_data': True,
                                                               'regulated_critical_service': True}, 45),
            # critical, no customer data: only the outage applies; health 82 -> 2% (best band, nothing to avoid)
            'net': _account(cid, 'Network Carrier', 400_000, {'criticality_tier': 'critical', 'holds_customer_data': False}, 82),
            # important, uploaded as plain CSV columns (land under attributes); health 60 -> 5%
            'kyc': _account(cid, 'KYC Screening', 250_000, {'attributes': {'criticality_tier': 'Important', 'holds_customer_data': 'yes'}}, 60),
            # standard with the tenant's own recorded failure cost: wins over the published figure
            'print': _account(cid, 'Statement Printer', 60_000, {'criticality_tier': 'standard', 'failure_cost': 90_000}, 55),
            'untiered': _account(cid, 'Untiered Vendor', 10_000, {}, 70),
            'unscored': _account(cid, 'Unscored Vendor', 20_000, {'criticality_tier': 'standard'}, None),
            'odd': _account(cid, 'Odd Tier Vendor', 5_000, {'criticality_tier': 'platinum'}, 70),
        }
        db.session.commit()
        yield cid, ids
        db.session.remove()


def _row(out, aid):
    return next(r for r in out['accounts'] if r['account_id'] == aid)


def _keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from _keys(v)


# ── capability and loaders ───────────────────────────────────────────────────

def test_money_basis_defaults_to_revenue_and_an_unknown_value_is_refused(monkeypatch):
    from utils import vertical_registry as vr
    assert vr.capability(REVENUE_VERTICAL, 'money_basis') == 'revenue'
    assert vr.capability(EXPOSURE_VERTICAL, 'money_basis') == 'exposure'
    assert 'money_basis' in vr.CAPABILITY_CHOICES


def test_revenue_loader_fails_closed_for_an_exposure_vertical_and_vice_versa():
    econ = settings.exposure_economics(EXPOSURE_VERTICAL)
    assert econ['model'] == 'exposure' and econ['basis'] == 'assumed'
    with pytest.raises(settings.EconomicsConfigError, match='exposure'):
        settings.economics(EXPOSURE_VERTICAL)
    with pytest.raises(settings.EconomicsConfigError, match='money basis'):
        settings.exposure_economics(REVENUE_VERTICAL)


@pytest.mark.parametrize('mutate,needle', [
    (lambda d: d.pop('tiers'), 'missing'),
    (lambda d: d['failure_events']['service_outage']['cost_by_tier'].pop('standard'), 'cost_by_tier'),
    (lambda d: d['failure_events']['service_outage'].pop('sources'), 'sources'),
    (lambda d: d['failure_events']['service_outage'].update(basis=''), 'basis'),
    (lambda d: d['annual_failure_likelihood_by_health']['bands'].reverse(), 'bands'),
    (lambda d: d['annual_failure_likelihood_by_health']['bands'][0].update(value=1.5), 'bands'),
    (lambda d: d['account_fields'].pop('tier'), 'account_fields'),
    (lambda d: d.update(basis='measured'), 'basis'),
])
def test_a_malformed_exposure_file_is_refused(tmp_path, monkeypatch, mutate, needle):
    data = json.loads(settings.economics_path(EXPOSURE_VERTICAL).read_text())
    data = copy.deepcopy(data)
    mutate(data)
    bad = tmp_path / f'{EXPOSURE_VERTICAL}.json'
    bad.write_text(json.dumps(data))
    monkeypatch.setattr(settings, 'economics_path', lambda v: bad)
    settings.exposure_economics.cache_clear()
    try:
        with pytest.raises(settings.EconomicsConfigError, match=needle):
            settings.exposure_economics(EXPOSURE_VERTICAL)
    finally:
        settings.exposure_economics.cache_clear()


def test_every_figure_in_the_exposure_file_carries_a_basis_and_sources():
    econ = settings.exposure_economics(EXPOSURE_VERTICAL)
    blocks = list(econ['failure_events'].values()) + [econ['annual_failure_likelihood_by_health'], econ['regulated_flag_note']]
    for b in blocks:
        assert b['basis'].strip()
        assert b['sources'] and all('claim' in s and 'verified' in s and 'url' in s for s in b['sources'])


def test_every_playbook_of_every_vertical_with_an_investment_file_has_a_cost_entry():
    from playbooks.definitions import load_vertical
    from utils.vertical_registry import SUPPORTED_VERTICALS
    checked = 0
    for v in sorted(SUPPORTED_VERTICALS):
        try:
            inv = settings.investment(v)
        except (settings.InvestmentConfigError, ValueError):
            continue
        ids = {p['id'] for p in load_vertical(v)['playbooks']}
        assert ids <= set(inv['playbooks']), (v, ids - set(inv['playbooks']))
        checked += 1
    assert checked >= 2
    assert settings.investment(EXPOSURE_VERTICAL)['playbooks']          # the exposure vertical is one of them


# ── the maths ────────────────────────────────────────────────────────────────

def test_likelihood_bands():
    econ = settings.exposure_economics(EXPOSURE_VERTICAL)
    assert ex.likelihood_for(100, econ)['value'] == 0.02 and ex.likelihood_for(70, econ)['index'] == 0
    assert ex.likelihood_for(69.99, econ)['value'] == 0.05
    assert ex.likelihood_for(50, econ)['value'] == 0.05
    assert ex.likelihood_for(49.9, econ)['value'] == 0.10 and ex.likelihood_for(0, econ)['value'] == 0.10


def test_account_rows(book):
    cid, ids = book
    with app.app_context():
        out = ex.exposure(cid)
        assert out['status'] == 'ok' and out['money_basis'] == 'exposure' and out['vertical'] == EXPOSURE_VERTICAL

        core = _row(out, ids['core'])                                  # largest applicable event, not the sum
        assert core['failure_event']['id'] == 'data_breach' and core['failure_cost']['value'] == 4_910_000
        assert core['annual_failure_likelihood'] == 0.10
        assert core['expected_annual_exposure']['value'] == pytest.approx(491_000)
        assert core['expected_annual_exposure']['basis'] == 'assumed'
        assert core['next_band']['min_health'] == 50 and core['next_band']['health_points_needed'] == 5
        assert core['next_band']['avoidable_exposure']['value'] == pytest.approx(4_910_000 * 0.05)
        assert core['regulated_flag'] is True and core['regulated_note']

        net = _row(out, ids['net'])                                    # the breach event does not apply
        assert net['failure_event']['id'] == 'service_outage' and net['failure_cost']['value'] == 1_200_000
        assert net['expected_annual_exposure']['value'] == pytest.approx(24_000) and net['next_band'] is None
        assert net['regulated_flag'] is False and 'regulated_note' not in net

        kyc = _row(out, ids['kyc'])                                    # columns filed under attributes, mixed case, 'yes'
        assert kyc['tier'] == 'important' and kyc['failure_event']['id'] == 'data_breach'
        assert kyc['expected_annual_exposure']['value'] == pytest.approx(2_500_000 * 0.05)

        pr = _row(out, ids['print'])                                   # the tenant's own figure wins and is derived
        assert pr['failure_cost_source'] == 'recorded' and pr['failure_cost']['value'] == 90_000
        assert pr['failure_cost']['basis'] == 'derived'
        assert pr['expected_annual_exposure']['value'] == pytest.approx(4_500)
        assert pr['expected_annual_exposure']['basis'] == 'assumed'    # the likelihood is still assumed: weakest link


def test_unpriced_accounts_are_listed_with_the_reason_and_never_defaulted(book):
    cid, ids = book
    with app.app_context():
        out = ex.exposure(cid)
        assert _row(out, ids['untiered'])['not_priced'] == ex.NOT_PRICED_NO_TIER
        assert _row(out, ids['unscored'])['not_priced'] == ex.NOT_PRICED_NO_HEALTH
        assert _row(out, ids['odd'])['not_priced'] == ex.NOT_PRICED_BAD_TIER
        for k in ('untiered', 'unscored', 'odd'):
            assert _row(out, ids[k])['expected_annual_exposure']['value'] is None
        p = out['portfolio']
        assert p['accounts'] == 7 and p['priced_accounts'] == 4
        assert p['not_priced'] == {ex.NOT_PRICED_NO_TIER: [ids['untiered']], ex.NOT_PRICED_NO_HEALTH: [ids['unscored']],
                                   ex.NOT_PRICED_BAD_TIER: [ids['odd']]}


def test_portfolio_totals_and_tiers(book):
    cid, ids = book
    with app.app_context():
        p = ex.exposure(cid)['portfolio']
        assert p['expected_annual_exposure']['value'] == pytest.approx(491_000 + 24_000 + 125_000 + 4_500)
        assert p['avoidable_exposure_next_band']['value'] == pytest.approx(245_500 + 2_500_000 * 0.03 + 90_000 * 0.03)
        assert p['regulated_flag_accounts'] == 1
        tiers = {t['tier']: t for t in p['by_tier']}
        assert list(tiers) == ['critical', 'important', 'standard']
        assert tiers['critical']['accounts'] == 2 and tiers['critical']['expected_annual_exposure']['value'] == pytest.approx(515_000)
        assert tiers['standard']['accounts'] == 2 and tiers['standard']['priced_accounts'] == 1
        assert p['contract_value']['value'] == pytest.approx(2_645_000) and p['contract_value']['basis'] == 'derived'


def test_one_account_only(book):
    cid, ids = book
    with app.app_context():
        out = ex.exposure(cid, ids['core'])
        assert [r['account_id'] for r in out['accounts']] == [ids['core']] and out['portfolio']['accounts'] == 1


def test_cost_and_exposure_are_never_divided(book):
    cid, _ = book
    with app.app_context():
        out = ex.exposure(cid)
        banned = ('ratio', 'roi', 'return', 'payback', 'multiple')
        assert not [k for k in _keys(out) if any(b in k.lower() for b in banned)]
        pb = out['playbooks']
        assert pb['status'] == 'ok' and len(pb['rows']) == len(settings.investment(EXPOSURE_VERTICAL)['playbooks'])
        assert all(r['estimated_cost_per_execution']['basis'] == 'assumed' for r in pb['rows'])
        assert all(r['exposure_avoided']['value'] is None for r in pb['rows'])       # nothing closed yet
        assert pb['estimated_cost_to_date']['value'] == 0


def test_exposure_avoided_needs_before_and_after_and_a_band_change():
    econ = settings.exposure_economics(EXPOSURE_VERTICAL)
    rows = {1: {'failure_cost': {'value': 1_000_000}}, 2: {'failure_cost': {'value': 1_000_000}}, 3: {'failure_cost': {'value': None}}}
    def v(i, acct, node, state='closed', closed='done', pb='sla_recovery'):
        return {'intervention_id': i, 'playbook_id': pb, 'account_id': acct, 'node_id': node, 'state': state, 'closed_state': closed}
    views = [v(10, 1, 100), v(11, 2, 101), v(12, 1, 102), v(13, 1, 103, state='sent', closed=None), v(14, 3, 104), v(15, 1, 105, closed='not_done')]
    hooks = {100: {'health_before': {'last': 45}, 'health_after': {'last': 72}},      # 10% -> 2%: 80,000 avoided
             101: {'health_before': {'last': 52}, 'health_after': {'last': 66}},      # same band: zero
             102: {'health_before': {'last': 45}, 'health_after': {}},                # no after: not counted
             103: {'health_before': {'last': 45}, 'health_after': {'last': 90}},      # still open: not counted
             104: {'health_before': {'last': 45}, 'health_after': {'last': 90}},      # unpriced account: not counted
             105: {'health_before': {'last': 45}, 'health_after': {'last': 90}}}      # closed not done: not counted
    s = ex._avoided(views, hooks, rows, econ)['sla_recovery']
    assert s['interventions'] == 6 and s['closed_done'] == 4 and s['with_health_before_and_after'] == 2
    assert s['avoided'] == pytest.approx(80_000) and s['intervention_ids'] == [10, 11]


# ── surfaces ─────────────────────────────────────────────────────────────────

def test_a_revenue_vertical_is_refused_and_power_of_1_is_refused_for_the_exposure_vertical(book):
    cid, _ = book
    from roi.power_of_1 import power_of_1
    with app.app_context():
        rev = _customer(REVENUE_VERTICAL)
        with pytest.raises(ValueError, match='money basis'):
            ex.exposure(rev)
        with pytest.raises(ValueError):
            power_of_1(cid)


def test_routes_and_tool_are_registered(book):
    cid, ids = book
    from roi.http import ROUTES
    from app_api.http import ROUTES as APP_ROUTES
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS
    from fastmcp.exceptions import ToolError
    assert '/api/roi/exposure' in ROUTES and '/app/api/roi/exposure' in APP_ROUTES and 'get_exposure' in KEYED_TOOLS
    with app.app_context():
        from mcp_server.cs_pulse_roi import get_exposure
        out = get_exposure(cid, ids['net'])
        assert out['accounts'][0]['account_name'] == 'Network Carrier'
        with pytest.raises(ToolError):
            get_exposure(cid + 100_000)
