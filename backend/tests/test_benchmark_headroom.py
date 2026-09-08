"""
Peer headroom in the investment ranking (roi/benchmarks.py), on real Postgres tenants.

The industry_benchmark nodes have existed since the CSV ingest was ported and had no reader
anywhere in roi/, journeys/ or ask_ai/. These tests pin the reader that closes that gap:

  * the benchmarks are read back out of the real graph nodes real ingestion writes
  * a real account's RANKING changes when — and only when — real benchmark data lands:
    the same tenant, same journeys, same revenue, ranked before and after the upload
  * revenue_weighted itself is untouched; the discount lives in a separate addressable_weighted
  * basis discipline: peer percentiles are `assumed` (weakest link), and every headroom row
    cites the node id and the publishing source it came from
  * no benchmark => not_covered => multiplier 1.0, `derived`, order unchanged — never guessed
  * a lower-is-better KPI is mirrored; unusable benchmark rows are dropped, not repaired
  * the floor bounds how far peer (trailing) data may discount a journey-derived risk
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
from models import Account, ContextNode                   # noqa: E402

# Two datacenter_v1 accounts, same signal, same renewal date, same phase -> the same journey-derived
# priority_factor. They differ in revenue and in where their (all healthy-band) KPI values sit against
# the peer distribution below. Elmwood is the bigger account and the better operator; Foundry is
# smaller and mid-pack.
ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,renewal_date\n"
    "ELM,Elmwood Grid,AI/ML,NA,5000000,2026-11-15\n"
    "FOR,Foundry Labs,AI/ML,NA,3600000,2026-11-15\n"
)
# P1-KPI1 realized $/GPU-hr (healthy 2.5-4.0), P2-KPI1 GPU utilization (healthy 70-100),
# P2-KPI2 goodput (healthy 90-100). Every value below is inside its catalog healthy band on BOTH
# accounts, so the health band — and therefore the leading factor — is the same for both.
KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "ELM,P1-KPI1,2026-07-01,3.6\nELM,P2-KPI1,2026-07-01,90\nELM,P2-KPI2,2026-07-01,96\n"
    "FOR,P1-KPI1,2026-07-01,2.7\nFOR,P2-KPI1,2026-07-01,78\nFOR,P2-KPI2,2026-07-01,90.5\n"
)
# Real percentile bands, tight enough that a healthy-band value can still be top-decile among peers —
# which is the whole point: "at risk by our thresholds, better than 90% of the market" and its inverse
# are different investment decisions.
BENCHMARKS = (
    "kpi_code,kpi_name,pillar,unit,p25,p50,p75,p90,source\n"
    "P1-KPI1,Realized $/GPU-hour,P1,$/gpu-hr,2.8,3.0,3.2,3.4,Uptime Institute 2025 Data Center Survey\n"
    "P2-KPI1,GPU Utilization (allocated),P2,%,80,82,84,86,MLPerf Fleet Benchmark 2025\n"
    "P2-KPI2,Effective utilization (goodput),P2,%,91,92,93,94,MLPerf Fleet Benchmark 2025\n"
)
# Deliberately unusable rows, uploaded alongside the good ones: one percentile only (below
# min_percentile_points), percentiles that do not increase, and a KPI this catalog does not define.
BAD_BENCHMARKS = (
    "kpi_code,kpi_name,pillar,unit,p25,p50,p75,p90,source\n"
    "P2-KPI4,Reserved-cluster utilization,P2,%,,60,,,Thin Source\n"
    "P2-KPI7,GPU memory efficiency,P2,%,95,80,70,60,Backwards Source\n"
    "P9-KPI9,Not In This Catalog,P9,%,10,20,30,40,Phantom Source\n"
)
# Idle GPU-hour rate: lower_is_better. Elmwood is LOW (good) and Foundry HIGH (bad) on the raw value.
LOWER_IS_BETTER_KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "ELM,P2-KPI3,2026-07-01,4\nFOR,P2-KPI3,2026-07-01,16\n"
)
LOWER_IS_BETTER_BENCHMARK = (
    "kpi_code,kpi_name,pillar,unit,p25,p50,p75,p90,source\n"
    "P2-KPI3,Idle GPU-hour rate,P2,%,5,9,13,17,ITIC Fleet Idle Study 2025\n"
)


def _tenant(name, vertical, accounts_csv, kpi_csv):
    from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
    tag = uuid.uuid4().hex[:8]
    cid = create_customer(name=f'{name} {tag}', domain=f'{name.lower()}-{tag}.test', vertical=vertical,
                          admin_email=f'{name.lower()}_{tag}@t.test', admin_name='B', data_origin='synthetic_test')['customer_id']
    upload_csv(cid, 'account_details.csv', accounts_csv)
    upload_csv(cid, 'kpi_measurements.csv', kpi_csv)
    res = process_data(cid)
    assert res['status'] == 'success', res
    ids = {a.external_account_id: a.account_id for a in Account.query.filter_by(customer_id=cid).all()}
    return cid, ids


def _sig(cid, aid, subtype, when, text):
    from mcp_server.cs_pulse_onboarding import submit_signal
    return submit_signal(cid, aid, text, source_type='crm_activity', signal_type=subtype, occurred_at=when, process_now=False)


def _rebuild(cid):
    from signal_engine.pipeline import process_pending
    from journeys.wizard_a import run_wizard_a
    process_pending(customer_id=cid, limit=100, rebuild_journeys=False)
    return run_wizard_a(cid, evaluate_playbooks=True)


def _load_benchmarks(cid, csv_text):
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data
    upload_csv(cid, 'industry_benchmarks.csv', csv_text)
    res = process_data(cid)
    assert res['status'] == 'success', res
    return res


@pytest.fixture(scope='module')
def tenant():
    """One datacenter_v1 tenant, scored BEFORE any benchmark exists, then again after the real
    industry_benchmarks.csv is ingested through the real pipeline. The `before` snapshot is the
    control: same accounts, same journeys, same revenue, no peer data."""
    with app.app_context():
        db.create_all()
        cid, ids = _tenant('Bench', 'datacenter_v1', ACCOUNTS, KPIS)
        # the same signal on both accounts, same day: identical phase / leading / urgency / renewal
        for aid in (ids['ELM'], ids['FOR']):
            _sig(cid, aid, 'capacity_warning', '2026-07-15T10:00:00Z', 'The reserved cluster is running hot again')
        _rebuild(cid)
        from roi.priorities import investment_priorities
        before = investment_priorities(cid)
        _load_benchmarks(cid, BENCHMARKS + '\n'.join(BAD_BENCHMARKS.splitlines()[1:]) + '\n')
        after = investment_priorities(cid)
        yield {'cid': cid, 'ids': ids, 'before': before, 'after': after}
        db.session.remove()
        db.drop_all()


# ── the nodes are real, and read back out of the real graph ───────────

def test_benchmarks_are_real_graph_nodes(tenant):
    with app.app_context():
        nodes = ContextNode.query.filter_by(customer_id=tenant['cid'], node_type='EXTERNAL_CONTEXT',
                                            node_subtype='industry_benchmark').all()
        assert {(n.properties or {}).get('kpi_code') for n in nodes} == {'P1-KPI1', 'P2-KPI1', 'P2-KPI2',
                                                                        'P2-KPI4', 'P2-KPI7', 'P9-KPI9'}
        gpu = next(n for n in nodes if (n.properties or {})['kpi_code'] == 'P1-KPI1')
        assert [gpu.properties[k] for k in ('p25', 'p50', 'p75', 'p90')] == ['2.8', '3.0', '3.2', '3.4']
        assert gpu.properties['benchmark_source'] == 'Uptime Institute 2025 Data Center Survey'

        from roi.benchmarks import load_customer_benchmarks
        bench = load_customer_benchmarks(tenant['cid'])
        # only the three usable rows survive: one percentile is below the minimum, one set decreases,
        # and the phantom KPI has no catalog definition to weight or direct it
        assert set(bench) == {'P1-KPI1', 'P2-KPI1', 'P2-KPI2', 'P9-KPI9'}
        assert bench['P1-KPI1']['points'] == [(0.25, 2.8), (0.5, 3.0), (0.75, 3.2), (0.9, 3.4)]
        assert bench['P1-KPI1']['node_id'] == gpu.node_id


def test_unusable_benchmark_rows_are_dropped_not_repaired(tenant):
    with app.app_context():
        from roi.benchmarks import load_customer_benchmarks
        bench = load_customer_benchmarks(tenant['cid'])
        assert 'P2-KPI4' not in bench                    # one percentile only: below min_percentile_points
        assert 'P2-KPI7' not in bench                    # p25 > p50 > p75 > p90: not a distribution
        # the phantom KPI parses, but has no catalog definition, so it never reaches an account's headroom
        for row in tenant['after']['rows']:
            assert 'P9-KPI9' not in {k['kpi'] for k in row['benchmark_headroom']['kpis']}


# ── the ranking really changes, and only because of the benchmarks ────

def test_real_benchmark_data_reorders_a_real_account(tenant):
    ids, before, after = tenant['ids'], tenant['before'], tenant['after']
    b = {r['account_id']: r for r in before['rows']}
    a = {r['account_id']: r for r in after['rows']}

    # control: with no peer data, both accounts are not_covered, undiscounted, and Elmwood leads on revenue
    assert [r['benchmark_headroom']['status'] for r in before['rows']] == ['not_covered', 'not_covered']
    for r in before['rows']:
        assert r['addressable_weighted']['value'] == r['revenue_weighted']['value']
        assert r['addressable_weighted']['basis'] == 'derived'
    assert [r['account_id'] for r in before['rows']] == [ids['ELM'], ids['FOR']]

    # the two accounts carry the same journey-derived factor: revenue is the only thing separating them
    assert b[ids['ELM']]['priority_factor'] == b[ids['FOR']]['priority_factor']
    assert b[ids['ELM']]['revenue_weighted']['value'] > b[ids['FOR']]['revenue_weighted']['value']

    # nothing but the benchmarks changed: the journeys, factors and exposure numbers are identical
    for aid in (ids['ELM'], ids['FOR']):
        assert a[aid]['risk_factor'] == b[aid]['risk_factor']
        assert a[aid]['opportunity_factor'] == b[aid]['opportunity_factor']
        assert a[aid]['revenue']['value'] == b[aid]['revenue']['value']
        assert a[aid]['revenue_weighted'] == b[aid]['revenue_weighted']

    # Elmwood is top-decile on every benchmarked KPI, Foundry is at or below the 25th percentile,
    # so the smaller account now has the larger realistically-addressable exposure and ranks first
    assert a[ids['ELM']]['benchmark_headroom']['factor'] < a[ids['FOR']]['benchmark_headroom']['factor']
    assert a[ids['ELM']]['addressable_weighted']['value'] < a[ids['FOR']]['addressable_weighted']['value']
    assert [r['account_id'] for r in after['rows']] == [ids['FOR'], ids['ELM']]
    assert [r['account_id'] for r in after['listed']] == [ids['FOR'], ids['ELM']]

    # and the discount is arithmetic anyone can check, not a black box
    for aid in (ids['ELM'], ids['FOR']):
        hr = a[aid]['benchmark_headroom']
        assert a[aid]['addressable_weighted']['value'] == pytest.approx(
            a[aid]['revenue_weighted']['value'] * hr['multiplier'], abs=1)


def test_headroom_is_assumed_and_cites_the_real_nodes(tenant):
    ids, after = tenant['ids'], tenant['after']
    with app.app_context():
        row = next(r for r in after['rows'] if r['account_id'] == ids['FOR'])
        hr = row['benchmark_headroom']
        assert hr['status'] == 'covered' and hr['covered_kpis'] == 3
        # peer percentiles are a population this tenant did not measure: assumed wins the weakest link
        assert row['addressable_weighted']['basis'] == 'assumed'
        assert row['revenue_weighted']['basis'] == 'derived'
        chain = row['addressable_weighted']['basis_chain']
        assert chain[0] == 'derived: Account.revenue' and chain[-1].startswith('assumed: peer headroom')
        assert 'Uptime Institute 2025 Data Center Survey' in chain[-1] or 'Uptime Institute 2025 Data Center Survey' in hr['basis']
        assert hr['sources'] == ['MLPerf Fleet Benchmark 2025', 'Uptime Institute 2025 Data Center Survey']
        # every KPI row names the node it was read from, and those nodes are this tenant's
        live = {n.node_id for n in ContextNode.query.filter_by(customer_id=tenant['cid'], node_type='EXTERNAL_CONTEXT',
                                                              node_subtype='industry_benchmark').all()}
        assert hr['benchmark_node_ids'] and set(hr['benchmark_node_ids']) <= live
        gpu = next(k for k in hr['kpis'] if k['kpi'] == 'P1-KPI1')
        assert gpu['value'] == 2.7 and gpu['percentiles'] == {'p25': 2.8, 'p50': 3.0, 'p75': 3.2, 'p90': 3.4}
        assert gpu['headroom'] == 0.75 and gpu['higher_is_better'] is True     # at or below p25: the most room peers evidence
        assert sum(k['weight'] for k in hr['kpis']) == pytest.approx(1.0, abs=1e-3)


def test_not_covered_accounts_are_absent_not_zero(tenant):
    """An account with no peer benchmark is NOT discounted — absence of peer evidence is not evidence
    of no headroom — and says so in the figure's own note."""
    with app.app_context():
        from roi.priorities import investment_priorities
        cid, ids = _tenant('BenchNone', 'saas_premium',
                           "source_account_id,account_name,industry,region,arr,renewal_date\n"
                           "AAA,Aster Analytics,SaaS,NA,900000,2026-12-01\n",
                           "source_account_id,kpi_code,measured_at,value\nAAA,P1-KPI1,2026-07-01,62\n")
        _sig(cid, ids['AAA'], 'champion_departure', '2026-07-20T10:00:00Z', 'Our champion is moving on')
        _rebuild(cid)
        row = investment_priorities(cid)['rows'][0]
        hr = row['benchmark_headroom']
        assert hr['status'] == 'not_covered' and hr['factor'] is None and hr['multiplier'] == 1.0
        assert hr['covered_kpis'] == 0 and hr['kpis'] == []
        assert row['addressable_weighted']['value'] == row['revenue_weighted']['value']
        assert row['addressable_weighted']['basis'] == 'derived'
        assert 'not discounted' in row['addressable_weighted']['note']
        cov = investment_priorities(cid)['portfolio']['benchmark_coverage']
        assert cov == {'covered_accounts': 0, 'accounts': 1, 'benchmarked_kpis': 0, 'sources': []}


# ── direction, and the floor ──────────────────────────────────────────

def test_lower_is_better_kpi_is_mirrored(tenant):
    """Idle GPU-hour rate: a LOW raw value is a GOOD one, so it must read as little headroom, not much."""
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        from roi.benchmarks import load_customer_benchmarks, kpi_headroom
        from utils.vertical_registry import get_kpis
        cid, ids = tenant['cid'], tenant['ids']
        upload_csv(cid, 'kpi_measurements.csv', LOWER_IS_BETTER_KPIS)
        _load_benchmarks(cid, LOWER_IS_BETTER_BENCHMARK)
        bench, kdef = load_customer_benchmarks(cid)['P2-KPI3'], get_kpis('datacenter_v1')['P2-KPI3']
        assert kdef['higher_is_better'] is False
        good, bad = kpi_headroom(4, kdef, bench), kpi_headroom(16, kdef, bench)   # 4 <= p25, 16 near p90
        assert good['raw_percentile'] == 0.25 and good['position'] == 0.75 and good['headroom'] == 0.25
        assert bad['raw_percentile'] > 0.75 and bad['position'] < 0.25 and bad['headroom'] > 0.75
        # Elmwood is the good operator here too, so the mirrored KPI keeps pushing it down the list
        from roi.priorities import investment_priorities
        rows = {r['account_id']: r for r in investment_priorities(cid)['rows']}
        idle = {aid: next(k for k in rows[aid]['benchmark_headroom']['kpis'] if k['kpi'] == 'P2-KPI3')
                for aid in (ids['ELM'], ids['FOR'])}
        assert idle[ids['ELM']]['headroom'] < idle[ids['FOR']]['headroom']
        assert [r['account_id'] for r in investment_priorities(cid)['rows']] == [ids['FOR'], ids['ELM']]


def test_the_floor_bounds_how_far_peer_data_can_discount(tenant):
    """Benchmarks are trailing KPI data. The leading (qualitative) layer routinely diverges from them,
    so a top-decile-on-everything account must still be ranked, not multiplied to nothing."""
    with app.app_context():
        from roi import settings
        from roi.benchmarks import account_headroom
        min_mult = float(settings.get('priority', 'benchmark_headroom', 'min_multiplier'))
        kpis = {'K': {'name': 'K', 'weight_l1': 1.0, 'higher_is_better': True}}
        bench = {'K': {'points': [(0.25, 10.0), (0.5, 20.0), (0.75, 30.0), (0.9, 40.0)],
                       'source': 'S', 'unit': None, 'node_id': 1, 'percentiles': {}}}
        best = account_headroom(kpis, bench, {'K': 99.0})        # far past p90
        worst = account_headroom(kpis, bench, {'K': 0.0})        # far below p25
        assert best['factor'] == 0.1 and best['multiplier'] == pytest.approx(min_mult + (1 - min_mult) * 0.1)
        assert best['multiplier'] >= min_mult > 0                # never zero: the risk survives the discount
        assert worst['factor'] == 0.75 and worst['multiplier'] < 1.0
        # the ranking effect is bounded: at most this ratio between two otherwise identical accounts
        assert worst['multiplier'] / best['multiplier'] == pytest.approx(0.875 / 0.55, rel=1e-3)

        elm = next(r for r in tenant['after']['rows'] if r['account_id'] == tenant['ids']['ELM'])
        assert elm['addressable_weighted']['value'] >= elm['revenue_weighted']['value'] * min_mult
        assert elm['priority_factor'] == next(r for r in tenant['before']['rows']
                                              if r['account_id'] == tenant['ids']['ELM'])['priority_factor']
