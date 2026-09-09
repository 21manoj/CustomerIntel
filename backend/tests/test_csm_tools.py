"""
CSM scorecard / team capacity / daily actions / ranking (roi/csm.py, mcp_server/cs_pulse_csm.py)
on a real Postgres tenant:

  * CSM identity is User.role == 'csm' + User.allowed_account_ids (unset = entire tenant, matching
    app_api.auth.allows_account exactly) -- there is no separate CSM table in this schema, and this
    suite proves the real column is read correctly rather than any fabricated roster.
  * get_csm_scorecard: book resolution (explicit list vs unset = entire portfolio), revenue managed,
    health distribution, risk/opportunity (reused from get_investment_priorities, not re-derived),
    intervention/outcome performance (reused from list_interventions) -- and fails closed for an
    unknown csm_user_id, a non-csm role, or another tenant's user id.
  * get_team_capacity: per-CSM workload, coverage/overlap across the whole tenant, the no_csm_users path.
  * get_csm_daily_actions: reuses get_investment_priorities' own ranking; pending approvals surface as
    actions, a stuck/undelivered intervention surfaces separately regardless of ranking, portfolio-wide
    vs one CSM's book, and the no_journeys hand-off.
  * get_csm_ranking: sorts by each of the 4 supported metrics, is internally consistent with
    get_team_capacity's own numbers (same underlying rollup), None-handling, the no_csm_users path,
    and rejects an unrecognized metric.
  * tenant isolation: one tenant's tools never surface another tenant's CSM/account data, and a real
    CSM id from tenant A is refused (not silently substituted) when asked of tenant B.
"""
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from flask import Flask                                   # noqa: E402
from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_csm_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common                       # noqa: E402
_common._flask_app = app
from models import Account, Intervention, User             # noqa: E402

SAAS_ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,renewal_date,primary_champion_name\n"
    "NOR,Northstar Mutual,Insurance,NA,1200000,2026-08-20,Dana Whitfield\n"
    "ORC,Orchard Retail,Retail,NA,900000,2027-03-01,Ivy Chen\n"
    "QUI,Quiet Co,Media,EU,600000,2027-01-01,\n"
)
SAAS_KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "NOR,P1-KPI1,2026-05-01,62\nNOR,P5-KPI1,2026-05-01,104\n"
    "NOR,P1-KPI1,2026-06-01,55\nNOR,P5-KPI1,2026-06-01,101\n"
    "NOR,P1-KPI1,2026-07-01,44\nNOR,P5-KPI1,2026-07-01,97\n"
    "ORC,P1-KPI1,2026-07-01,95\nORC,P5-KPI1,2026-07-01,118\n"
    "QUI,P1-KPI1,2026-07-01,80\nQUI,P5-KPI1,2026-07-01,110\n"
)
OTHER_ACCOUNTS = "source_account_id,account_name,industry,region,arr,renewal_date\nFAR,Faraway Inc,Media,EU,300000,2027-05-01\n"
OTHER_KPIS = "source_account_id,kpi_code,measured_at,value\nFAR,P1-KPI1,2026-07-01,80\nFAR,P5-KPI1,2026-07-01,90\n"


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


def _csm(cid, name, account_ids=None, active=True):
    tag = uuid.uuid4().hex[:6]
    u = User(customer_id=cid, user_name=name, email=f'{name.lower().replace(" ", ".")}_{tag}@t.test', role='csm',
             active=active, allowed_account_ids=account_ids, allowed_customer_ids=[cid])
    db.session.add(u); db.session.commit()
    return u.user_id


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        cid, ids = _tenant('CsmSaas', 'saas_premium', SAAS_ACCOUNTS, SAAS_KPIS)
        _sig(cid, ids['NOR'], 'champion_departure', '2026-07-20T10:00:00Z', 'Dana Whitfield is leaving at the end of the month')
        _sig(cid, ids['NOR'], 'budget_pressure', '2026-07-24T10:00:00Z', 'Procurement wants a 20% reduction at renewal')
        _sig(cid, ids['ORC'], 'expansion_interest', '2026-07-22T10:00:00Z', 'Ops asked for 40 more seats for the new region')
        _rebuild(cid)
        admin_id = User.query.filter_by(customer_id=cid, role='admin').first().user_id
        csm_pair = _csm(cid, 'Csm Pair', [ids['NOR'], ids['ORC']])     # book: the protect + the grow account
        csm_quiet = _csm(cid, 'Csm Quiet', [ids['QUI']])               # book: the one quiet account
        csm_all = _csm(cid, 'Csm All', None)                           # unset -> entire tenant
        csm_inactive = _csm(cid, 'Csm Gone', [ids['NOR']], active=False)
        yield {'cid': cid, 'ids': ids, 'admin_id': admin_id,
               'csm_pair': csm_pair, 'csm_quiet': csm_quiet, 'csm_all': csm_all, 'csm_inactive': csm_inactive}
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope='module')
def other_tenant(tenant):
    """A second real tenant, no CSM users at all -- both the no_csm_users path and the far side of
    the tenant-isolation checks."""
    with app.app_context():
        cid, ids = _tenant('CsmOther', 'saas_premium', OTHER_ACCOUNTS, OTHER_KPIS)
        yield {'cid': cid, 'ids': ids}


# ── get_csm_scorecard ──────────────────────────────────────────────────

def test_scorecard_explicit_book(tenant):
    from mcp_server.cs_pulse_csm import get_csm_scorecard
    cid, ids = tenant['cid'], tenant['ids']
    with app.app_context():
        out = get_csm_scorecard(cid, tenant['csm_pair'])
        assert out['customer_id'] == cid
        assert out['csm'] == {'user_id': tenant['csm_pair'], 'name': 'Csm Pair', 'email': out['csm']['email'], 'active': True}
        assert out['book']['scope'] == 'explicit allowed_account_ids'
        assert set(out['book']['account_ids']) == {ids['NOR'], ids['ORC']}
        assert out['revenue_managed'] == {'value': 2_100_000.0, 'basis': 'derived',
                                          'basis_chain': ['derived: Σ Account.revenue over the book (get_account_arr)']}
        assert out['health']['accounts_scored'] == 2 and out['health']['accounts_unscored'] == []
        assert sum(out['health']['by_band'].values()) == 2
        assert out['risk_opportunity']['status'] == 'ok'
        assert out['risk_opportunity']['accounts_with_journey'] == 2 and out['risk_opportunity']['accounts_without_journey'] == []
        # both NOR (champion_departure_sponsor_rebuild) and ORC (expansion_intent_handoff) auto-propose, still pending
        assert out['risk_opportunity']['pending_approvals'] == 2
        assert {a['account_id'] for a in out['risk_opportunity']['top_accounts']} == {ids['NOR'], ids['ORC']}
        assert out['interventions']['total'] == 2 and out['interventions']['by_state']['proposed'] == 2
        assert out['interventions']['realized_revenue']['value'] is None    # nothing reported yet
        assert out['interventions']['exposure_revenue_open']['value'] == 2_100_000.0


def test_scorecard_entire_portfolio_book(tenant):
    from mcp_server.cs_pulse_csm import get_csm_scorecard
    cid, ids = tenant['cid'], tenant['ids']
    with app.app_context():
        out = get_csm_scorecard(cid, tenant['csm_all'])
        assert out['book']['scope'].startswith('entire_portfolio')
        assert set(out['book']['account_ids']) == {ids['NOR'], ids['ORC'], ids['QUI']}
        assert out['revenue_managed']['value'] == 2_700_000.0
        assert out['health']['accounts_scored'] == 3


def test_scorecard_quiet_book_has_nothing_actionable(tenant):
    from mcp_server.cs_pulse_csm import get_csm_scorecard
    cid, ids = tenant['cid'], tenant['ids']
    with app.app_context():
        out = get_csm_scorecard(cid, tenant['csm_quiet'])
        assert out['book']['account_ids'] == [ids['QUI']]
        assert out['interventions']['total'] == 0
        assert out['risk_opportunity']['pending_approvals'] == 0
        assert out['interventions']['realized_revenue']['value'] is None
        assert out['interventions']['exposure_revenue_open']['value'] == 0.0


def test_scorecard_fails_closed(tenant, other_tenant):
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_csm import get_csm_scorecard
    cid = tenant['cid']
    with app.app_context():
        with pytest.raises(ToolError, match='not found'):
            get_csm_scorecard(cid, 999_999_999)                       # no such user at all
        with pytest.raises(ToolError, match="not 'csm'"):
            get_csm_scorecard(cid, tenant['admin_id'])                 # real user, wrong role
        with pytest.raises(ToolError, match='not found'):
            get_csm_scorecard(other_tenant['cid'], tenant['csm_pair'])  # real csm, WRONG tenant
        with pytest.raises(ToolError, match='not found'):
            get_csm_scorecard(999_999_999, tenant['csm_pair'])         # unknown customer entirely


# ── get_team_capacity ────────────────────────────────────────────────────

def test_team_capacity_workload_and_coverage(tenant):
    from mcp_server.cs_pulse_csm import get_team_capacity
    cid, ids = tenant['cid'], tenant['ids']
    with app.app_context():
        out = get_team_capacity(cid)
        assert out['status'] == 'ok'
        by_id = {c['user_id']: c for c in out['csms']}
        assert tenant['csm_inactive'] not in by_id                     # inactive excluded entirely
        assert set(by_id) == {tenant['csm_pair'], tenant['csm_quiet'], tenant['csm_all']}
        pair = by_id[tenant['csm_pair']]
        assert pair['account_count'] == 2 and pair['revenue_managed']['value'] == 2_100_000.0
        assert pair['pending_approvals'] == 2 and pair['open_interventions'] == 2
        cov = out['coverage']
        assert cov['tenant_accounts'] == 3
        assert cov['covered_accounts'] == 3 and cov['uncovered_count'] == 0 and cov['uncovered_account_ids'] == []
        assert cov['any_csm_scoped_to_entire_portfolio'] is True
        # every account sits in both its explicit-book CSM and csm_all's entire-portfolio book
        assert set(cov['accounts_covered_by_multiple_csms']) == {ids['NOR'], ids['ORC'], ids['QUI']}


def test_team_capacity_no_csm_users(other_tenant):
    from mcp_server.cs_pulse_csm import get_team_capacity
    with app.app_context():
        out = get_team_capacity(other_tenant['cid'])
        assert out['status'] == 'no_csm_users' and out['csms'] == []
        assert out['coverage']['tenant_accounts'] == 1 and out['coverage']['uncovered_count'] == 1


# ── get_csm_daily_actions ────────────────────────────────────────────────

def test_daily_actions_portfolio_wide(tenant):
    from mcp_server.cs_pulse_csm import get_csm_daily_actions
    cid, ids = tenant['cid'], tenant['ids']
    with app.app_context():
        out = get_csm_daily_actions(cid)
        assert out['status'] == 'ok' and out['book_scope'] == 'portfolio_wide' and out['csm'] is None
        by_account = {a['account_id']: a for a in out['actions']}
        assert by_account[ids['NOR']]['action_type'] == 'approve_intervention'
        assert by_account[ids['ORC']]['action_type'] == 'approve_intervention'
        assert ids['QUI'] not in by_account                            # quiet account: nothing to do today
        assert out['summary']['pending_approvals'] == 2
        assert out['stuck_interventions'] == []                        # nothing sent yet


def test_daily_actions_scoped_to_one_csm_book(tenant):
    from mcp_server.cs_pulse_csm import get_csm_daily_actions
    cid = tenant['cid']
    with app.app_context():
        quiet = get_csm_daily_actions(cid, tenant['csm_quiet'])
        assert quiet['csm']['user_id'] == tenant['csm_quiet']
        assert quiet['actions'] == [] and quiet['actions_total_before_limit'] == 0

        capped = get_csm_daily_actions(cid, tenant['csm_pair'], top_n=1)
        assert len(capped['actions']) == 1 and capped['actions_total_before_limit'] == 2


def test_daily_actions_no_journeys_yet_hands_off_cleanly():
    from mcp_server.cs_pulse_onboarding import create_customer
    from mcp_server.cs_pulse_csm import get_csm_daily_actions
    with app.app_context():
        tag = uuid.uuid4().hex[:6]
        cid = create_customer(name=f'Empty {tag}', domain=f'empty-{tag}.test', vertical='saas_premium',
                              admin_email=f'e_{tag}@t.test', admin_name='E', data_origin='synthetic_test')['customer_id']
        out = get_csm_daily_actions(cid)
        assert out['status'] == 'no_journeys' and out['actions'] == [] and out['hint']


def test_stuck_intervention_surfaces_regardless_of_ranking(tenant):
    from playbooks.governance import approve
    from mcp_server.cs_pulse_csm import get_csm_daily_actions, get_csm_scorecard
    cid, ids = tenant['cid'], tenant['ids']
    with app.app_context():
        row = Intervention.query.filter_by(customer_id=cid, account_id=ids['NOR'], playbook_id='champion_departure_sponsor_rebuild').first()
        assert row is not None
        approve(cid, row.id, note='go')                 # proposed -> approved -> sent (auto-send), this codebase's own flow
        row = db.session.get(Intervention, row.id)
        row.sent_at = datetime.utcnow() - timedelta(days=20)   # older than governance.json's stuck_after_days (14)
        db.session.commit()
        try:
            out = get_csm_daily_actions(cid)
            stuck_ids = {s['intervention_id'] for s in out['stuck_interventions']}
            assert row.id in stuck_ids
            by_account = {a['account_id']: a for a in out['actions']}
            assert ids['NOR'] not in by_account             # sent (even if stuck) is "already in flight", not a new action

            card = get_csm_scorecard(cid, tenant['csm_pair'])
            assert card['interventions']['stuck'] == 1
            assert card['interventions']['by_state']['sent'] == 1
            assert card['interventions']['total'] == 2       # NOR (sent) + ORC (still proposed)
        finally:
            db.session.remove()


# ── get_csm_ranking ──────────────────────────────────────────────────────

def test_ranking_metrics_and_consistency_with_team_capacity(tenant):
    from mcp_server.cs_pulse_csm import get_csm_ranking, get_team_capacity
    cid = tenant['cid']
    with app.app_context():
        cap_by_id = {c['user_id']: c for c in get_team_capacity(cid)['csms']}

        rank_health = get_csm_ranking(cid, 'avg_health_weighted')
        assert rank_health['status'] == 'ok' and len(rank_health['ranked']) == 3
        for row in rank_health['ranked']:
            assert row['avg_health_score_revenue_weighted'] == cap_by_id[row['user_id']]['avg_health_score_revenue_weighted']
        values = [r['avg_health_score_revenue_weighted'] for r in rank_health['ranked']]
        assert values == sorted(values, key=lambda v: (v is None, -(v or 0)))
        assert [r['rank'] for r in rank_health['ranked']] == [1, 2, 3]

        rank_rev = get_csm_ranking(cid, 'revenue_managed')
        assert [r['user_id'] for r in rank_rev['ranked']] == [tenant['csm_all'], tenant['csm_pair'], tenant['csm_quiet']]
        assert [r['revenue_managed']['value'] for r in rank_rev['ranked']] == [2_700_000.0, 2_100_000.0, 600_000.0]

        rank_risk = get_csm_ranking(cid, 'addressable_risk_total')
        assert {r['user_id'] for r in rank_risk['ranked']} == {tenant['csm_pair'], tenant['csm_quiet'], tenant['csm_all']}
        assert [r['rank'] for r in rank_risk['ranked']] == [1, 2, 3]

        rank_realized = get_csm_ranking(cid, 'realized_revenue')
        assert all(r['realized_revenue']['value'] is None for r in rank_realized['ranked'])   # nothing reported yet
        assert [r['rank'] for r in rank_realized['ranked']] == [1, 2, 3]                        # still a stable, total order


def test_ranking_rejects_unknown_metric(tenant):
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_csm import get_csm_ranking
    with app.app_context():
        with pytest.raises(ToolError, match='metric must be one of'):
            get_csm_ranking(tenant['cid'], 'not_a_real_metric')


def test_ranking_no_csm_users(other_tenant):
    from mcp_server.cs_pulse_csm import get_csm_ranking
    with app.app_context():
        out = get_csm_ranking(other_tenant['cid'])
        assert out['status'] == 'no_csm_users' and out['ranked'] == []


# ── fail-closed for an unknown customer, every tool ───────────────────────

def test_unknown_customer_fails_closed_for_every_tool():
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_csm import get_csm_scorecard, get_team_capacity, get_csm_daily_actions, get_csm_ranking
    with app.app_context():
        with pytest.raises(ToolError, match='not found'):
            get_csm_scorecard(999_999_999, 1)
        with pytest.raises(ToolError, match='not found'):
            get_team_capacity(999_999_999)
        with pytest.raises(ToolError, match='not found'):
            get_csm_daily_actions(999_999_999)
        with pytest.raises(ToolError, match='not found'):
            get_csm_ranking(999_999_999)


# ── tenant isolation ─────────────────────────────────────────────────────

def test_tenant_isolation_no_cross_tenant_leakage(tenant, other_tenant):
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_csm import get_csm_scorecard, get_team_capacity, get_csm_ranking, get_csm_daily_actions
    with app.app_context():
        with pytest.raises(ToolError, match='not found'):
            get_csm_scorecard(other_tenant['cid'], tenant['csm_pair'])   # real id, belongs to the OTHER tenant

        for out in (get_team_capacity(tenant['cid']), get_csm_ranking(tenant['cid']), get_csm_daily_actions(tenant['cid'])):
            assert 'Faraway' not in json.dumps(out, default=str)

        for out in (get_team_capacity(other_tenant['cid']), get_csm_ranking(other_tenant['cid']), get_csm_daily_actions(other_tenant['cid'])):
            blob = json.dumps(out, default=str)
            assert 'Northstar' not in blob and 'Orchard' not in blob and 'Quiet Co' not in blob


# ── registration / auth wiring ────────────────────────────────────────────

def test_tools_are_keyed_reads(tenant):
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    for t in ('get_csm_scorecard', 'get_team_capacity', 'get_csm_daily_actions', 'get_csm_ranking'):
        assert t in KEYED_TOOLS and t not in ONBOARDING_TOOLS and t not in WRITE_TOOLS
    src = (BACKEND / 'mcp_server' / 'cs_pulse_csm.py').read_text()
    assert set(re.findall(r"_require_auth_if_key_present\('([a-z_0-9]+)'", src)) == \
        {'get_csm_scorecard', 'get_team_capacity', 'get_csm_daily_actions', 'get_csm_ranking'}
