"""
seed_platform_benchmarks (utils/csv_ingest.py) — the platform-curated
config/industry_benchmarks/{vertical}.csv seed wired into process_data()
(mcp_server/cs_pulse_onboarding.py::_process_data_impl, Stage 1c).

  * a non-test-origin tenant (real / synthetic_demo / synthetic_replay) gets
    seeded automatically the first time it has an account and calls
    process_data() — this is what makes a brand-new tenant actually have
    industry_benchmark nodes, closing the gap this file exists to cover
  * a synthetic_test tenant is never seeded — the automated suite (this
    file included) needs to be able to observe an unbenchmarked tenant
  * a tenant that uploads its own industry_benchmarks.csv keeps its own
    data; the seed sees the resulting node(s) and no-ops (the schema's own
    "customer can override by uploading their own" promise)
  * seeding is idempotent — a second process_data() call does not duplicate
    nodes
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
from models import ContextNode                            # noqa: E402

ACCOUNT_DETAILS_CSV = (
    "source_account_id,account_name,industry,region,arr\n"
    "ACC-1,Northwind,SaaS,NA,2000000\n"
)
KPI_CSV = (
    "source_account_id,kpi_code,measured_at,value\n"
    "ACC-1,P5-KPI1,2026-07-01,105\n"
)
SAAS_SEED_KPI_CODES = {'P5-KPI1', 'P5-KPI2', 'P3-KPI3', 'P1-KPI5', 'P1-KPI3'}


@pytest.fixture(scope='module', autouse=True)
def _schema():
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def _create(data_origin, vertical='saas_premium'):
    from mcp_server.cs_pulse_onboarding import create_customer
    tag = uuid.uuid4().hex[:8]
    return create_customer(
        name=f'BenchSeed {tag}', domain=f'benchseed-{tag}.test', vertical=vertical,
        admin_email=f'bs_{tag}@t.test', admin_name='B', data_origin=data_origin,
    )['customer_id']


def _benchmark_nodes(cid):
    return ContextNode.query.filter_by(
        customer_id=cid, node_type='EXTERNAL_CONTEXT', node_subtype='industry_benchmark',
    ).all()


def test_real_tenant_gets_seeded_on_first_process_data():
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        cid = _create('real')
        upload_csv(cid, 'account_details.csv', ACCOUNT_DETAILS_CSV)
        upload_csv(cid, 'kpi_measurements.csv', KPI_CSV)
        res = process_data(cid)
        assert res['status'] == 'success', res
        assert any('benchmarks_seeded' in s for s in res['steps_completed']), res['steps_completed']

        nodes = _benchmark_nodes(cid)
        assert {n.properties['kpi_code'] for n in nodes} == SAAS_SEED_KPI_CODES
        nrr = next(n for n in nodes if n.properties['kpi_code'] == 'P5-KPI1')
        assert [nrr.properties[k] for k in ('p25', 'p50', 'p75')] == ['97', '102', '111']
        assert 'SaaS Capital' in nrr.properties['benchmark_source']

        # readable by the ROI headroom reader this seed exists to feed
        from roi.benchmarks import load_customer_benchmarks
        bench = load_customer_benchmarks(cid)
        assert 'P5-KPI1' in bench and bench['P5-KPI1']['points'] == [(0.25, 97.0), (0.5, 102.0), (0.75, 111.0)]


def test_synthetic_demo_tenant_is_also_seeded():
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        cid = _create('synthetic_demo')
        upload_csv(cid, 'account_details.csv', ACCOUNT_DETAILS_CSV)
        upload_csv(cid, 'kpi_measurements.csv', KPI_CSV)
        res = process_data(cid)
        assert res['status'] == 'success', res
        assert len(_benchmark_nodes(cid)) == len(SAAS_SEED_KPI_CODES)


def test_synthetic_test_tenant_is_never_seeded():
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        cid = _create('synthetic_test')
        upload_csv(cid, 'account_details.csv', ACCOUNT_DETAILS_CSV)
        upload_csv(cid, 'kpi_measurements.csv', KPI_CSV)
        res = process_data(cid)
        assert res['status'] == 'success', res
        assert _benchmark_nodes(cid) == []
        assert not any('benchmarks_seeded' in s for s in res['steps_completed'])


def test_customers_own_upload_wins_over_the_seed():
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        cid = _create('synthetic_demo')
        own_csv = (
            "kpi_code,kpi_name,pillar,unit,p25,p50,p75,p90,source\n"
            "P5-KPI1,Net Revenue Retention (NRR),P5,percentage,90,100,110,120,Customer's Own CRM Export\n"
        )
        upload_csv(cid, 'account_details.csv', ACCOUNT_DETAILS_CSV)
        upload_csv(cid, 'industry_benchmarks.csv', own_csv)
        res = process_data(cid)
        assert res['status'] == 'success', res

        nodes = _benchmark_nodes(cid)
        assert len(nodes) == 1
        assert nodes[0].properties['benchmark_source'] == "Customer's Own CRM Export"
        # the platform seed saw the customer's node already in place (loaded by the same
        # process_data run, before Stage 1c) and never ran
        assert not any('benchmarks_seeded' in s for s in res['steps_completed'])


def test_seed_is_idempotent_across_repeated_process_data_calls():
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        cid = _create('real')
        upload_csv(cid, 'account_details.csv', ACCOUNT_DETAILS_CSV)
        upload_csv(cid, 'kpi_measurements.csv', KPI_CSV)
        process_data(cid)
        first_count = len(_benchmark_nodes(cid))
        assert first_count == len(SAAS_SEED_KPI_CODES)

        res2 = process_data(cid)
        assert res2['status'] == 'success', res2
        assert len(_benchmark_nodes(cid)) == first_count
        assert not any('benchmarks_seeded' in s for s in res2['steps_completed'])


def test_every_vertical_seed_file_is_valid_and_loadable():
    """Every SUPPORTED_VERTICALS catalog either has a real seed file that loads cleanly, or
    has none yet — either is fine, but a seed file that exists must not silently no-op."""
    from utils.vertical_registry import SUPPORTED_VERTICALS
    from utils.csv_ingest import parse_rows, load_benchmarks, _BENCHMARK_SEED_DIR

    with app.app_context():
        from mcp_server.cs_pulse_onboarding import upload_csv, process_data
        for vertical in sorted(SUPPORTED_VERTICALS):
            path = os.path.join(_BENCHMARK_SEED_DIR, f'{vertical}.csv')
            if not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as f:
                rows = parse_rows(f.read())
            assert rows, f'{vertical}.csv has no data rows'
            for r in rows:
                assert r.get('kpi_code') and r.get('source'), f'{vertical}.csv row missing kpi_code/source: {r}'

            cid = _create('real', vertical=vertical)
            upload_csv(cid, 'account_details.csv', ACCOUNT_DETAILS_CSV)
            res = process_data(cid)
            assert res['status'] == 'success', (vertical, res)
            nodes = _benchmark_nodes(cid)
            assert len(nodes) == len(rows), (vertical, 'not every seed row became a node')
