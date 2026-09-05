"""
G1/G2 for the numbers a CFO acts on:
  * every health score row records what it was computed from (weights, codes,
    weight source, catalog / taxonomy / scorer versions, input upload, run)
  * every upload is a CsvUpload row (hash, size, who, warnings) that KPI rows
    and health rows point back to, and that the consuming run marks
  * every process_data run is a ProcessRun row
  * a blank KPI value is a missing measurement, not 0.0
  * a scorer that cannot be built fails the stage instead of writing 0.0
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

from models import HealthScore, KPIMeasurement, CsvUpload, CsvUploadStaging, ProcessRun, Account   # noqa: E402

ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,csm_name,csm_email,csm_manager,"
    "executive_sponsor,primary_champion_name,primary_champion_title,tier,employee_count,products,contract_end,renewal_date\n"
    "ACC-1,Titan Hyperscale Labs,Telecommunications,North America,8200000,Sarah Rivera,s@x.com,Sam Rivera,,Riley Foster,Director of IT,Enterprise,4592,,2026-06-15,2026-06-15\n"
)
KPIS = (
    "source_account_id,kpi_code,kpi_name,pillar,measured_at,value,target,weight,status\n"
    "ACC-1,P1-KPI1,Realized $/GPU-hour,P1,2025-11-01,1.22,2.5,0.25,critical\n"
    "ACC-1,P2-KPI1,Uptime,P2,2025-11-01,99.9,99.95,0.3,ok\n"
    "ACC-1,P2-KPI1,Uptime,P2,2025-12-01,,99.95,0.3,ok\n"          # blank value → skipped, not 0.0
    "ACC-1,P1-KPI1,Realized $/GPU-hour,P1,2025-12-01,1.40,2.5,0.25,critical\n"
    "ACC-1,NOT-A-KPI,Made up,P9,2025-12-01,5,,,\n"                # not in the catalog → dropped, recorded
)


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Prov {tag}', domain=f'prov-{tag}.test', vertical='datacenter_v1',
                              admin_email=f'p_{tag}@t.test', admin_name='P')['customer_id']
        u1 = upload_csv(cid, 'account_details.csv', ACCOUNTS)
        u2 = upload_csv(cid, 'kpi_measurements.csv', KPIS)
        res = process_data(cid)
        yield cid, u1, u2, res
        db.session.remove()
        db.drop_all()


def test_upload_rows_exist_and_are_consumed_by_the_run(tenant):
    cid, u1, u2, res = tenant
    assert u1['upload_id'] and u2['upload_id'] and u2['upload_id'] > u1['upload_id']
    with app.app_context():
        ups = CsvUpload.query.filter_by(customer_id=cid).order_by(CsvUpload.id).all()
        assert [u.file_type for u in ups] == ['account_details.csv', 'kpi_measurements.csv']
        k = ups[1]
        assert len(k.sha256) == 64 and k.row_count == 5 and k.byte_count == len(KPIS.encode()) and k.key_kind == 'local'
        run = ProcessRun.query.filter_by(run_id=res['run_id']).one()
        assert all(u.consumed_at is not None and u.process_run_id == run.id for u in ups)
        assert CsvUploadStaging.query.filter_by(customer_id=cid).count() == 0            # staging cleared, record kept
        assert run.status == 'success' and run.upload_ids == [u1['upload_id'], u2['upload_id']]
        assert run.counts['kpi_rows_skipped_blank'] == 1 and run.counts['accounts'] == 1
        assert any(s.startswith('health_scores_auto_') for s in run.steps) and run.generator_version and run.finished_at


def test_kpi_rows_carry_their_upload_and_blank_is_not_zero(tenant):
    cid, u1, u2, res = tenant
    with app.app_context():
        aid = Account.query.filter_by(customer_id=cid).one().account_id
        rows = KPIMeasurement.query.filter_by(account_id=aid).all()
        assert rows and all(r.upload_id == u2['upload_id'] for r in rows)
        dec_uptime = [r for r in rows if r.kpi_code == 'P2-KPI1' and r.measured_at.month == 12]
        assert dec_uptime == []                                                          # the blank row never became 0.0
        assert 'kpis_loaded_4_blank_skipped_1' in res['steps_completed']                 # 3 catalog KPIs + NOT-A-KPI stored (dropped at scoring), blank skipped


def test_health_rows_cite_what_they_were_computed_from(tenant):
    cid, u1, u2, res = tenant
    with app.app_context():
        aid = Account.query.filter_by(customer_id=cid).one().account_id
        hs = {h.measurement_month.month: h for h in HealthScore.query.filter_by(account_id=aid).all()}
        assert set(hs) == {11, 12}
        run = ProcessRun.query.filter_by(run_id=res['run_id']).one()
        for h in hs.values():
            assert h.pillar_weights and h.kpi_weights and h.kpi_codes_used
            assert h.weight_source == 'catalog' and h.catalog_version and h.taxonomy_version and h.scorer_version == '2.0'
            assert h.input_upload_id == u2['upload_id'] and h.process_run_id == run.id
        assert hs[11].kpi_codes_used == ['P1-KPI1', 'P2-KPI1'] and hs[11].kpi_codes_dropped == []
        assert hs[12].kpi_codes_used == ['P1-KPI1'] and hs[12].kpi_codes_dropped == ['NOT-A-KPI']       # named, not silent
        assert set(hs[11].pillar_weights) == {'P1', 'P2'} and set(hs[11].kpi_weights) == {'P1-KPI1', 'P2-KPI1'}


def test_customer_config_weights_are_recorded_as_the_source(tenant):
    cid, u1, u2, res = tenant
    from mcp_server.cs_pulse_onboarding import process_data
    with app.app_context():
        from models import CustomerConfig
        cc = CustomerConfig.query.filter_by(customer_id=cid).first()
        cc.pillar_weights = {'P1': 0.9, 'P2': 0.1}
        db.session.commit()
    res2 = process_data(cid, mode='full_recalc')
    with app.app_context():
        aid = Account.query.filter_by(customer_id=cid).one().account_id
        h = HealthScore.query.filter_by(account_id=aid).order_by(HealthScore.measurement_month).first()
        run2 = ProcessRun.query.filter_by(run_id=res2['run_id']).one()
        assert h.weight_source == 'customer_config' and h.pillar_weights == {'P1': 0.9, 'P2': 0.1} and h.process_run_id == run2.id
        assert ProcessRun.query.filter_by(customer_id=cid).count() == 2


def test_missing_scorer_fails_the_stage_instead_of_writing_zero():
    from mcp_server.common import get_health_functions
    with app.app_context():
        with pytest.raises(RuntimeError, match='scorer unavailable'):
            get_health_functions(999999999)
