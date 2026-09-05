"""
Customer extensions: 'attributes' on every file, unknown columns fold into it, the
per-tenant column map renames/promotes at upload, oversized attributes are rejected,
attributes reach the read surface and never the model prompt. Plus the account trim
(revenue alias, dropped speculative columns fold into attributes) and the KPI file's
catalog-owned warnings.
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
from models import Account, KPIMeasurement, QualitativeSignal, ContextNode   # noqa: E402


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Ext {tag}', domain=f'ext-{tag}.test', vertical='saas_premium',
                              admin_email=f'e_{tag}@t.test', admin_name='E', data_origin='synthetic_test')['customer_id']
        yield cid
        db.session.remove()
        db.drop_all()


def test_account_unknown_columns_fold_into_attributes_and_revenue_alias(tenant):
    cid = tenant
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data, get_journey, list_journeys
    acc = ("source_account_id,account_name,industry,region,revenue,employee_count,cloud_provider,attributes\n"
           "ACC-1,Harbor Analytics,Software,NA,900000,900,AWS,\"{\"\"deal_stage\"\": \"\"expansion\"\", \"\"segment\"\": \"\"mid\"\"}\"\n")
    r = upload_csv(cid, 'account_details.csv', acc)
    assert r['status'] == 'success'
    assert any('folded into attributes' in w and 'employee_count' in w and 'cloud_provider' in w for w in r['warnings']), r['warnings']
    assert not any('revenue' in w for w in r['warnings'])                  # alias of arr, not unknown
    process_data(cid)
    with app.app_context():
        a = Account.query.filter_by(customer_id=cid, external_account_id='ACC-1').one()
        assert float(a.revenue) == 900000.0
        assert a.profile_metadata['attributes'] == {'deal_stage': 'expansion', 'segment': 'mid', 'employee_count': 900, 'cloud_provider': 'AWS'}
        assert 'employee_count' not in a.profile_metadata                    # no longer a named field
        aid = a.account_id
    row = next(x for x in list_journeys(cid)['journeys'] if x['account_id'] == aid)
    assert row['account_name'] == 'Harbor Analytics'
    j = get_journey(cid, aid)
    assert j['account']['attributes']['cloud_provider'] == 'AWS'


def test_oversized_attributes_row_is_rejected_with_a_warning(tenant):
    cid = tenant
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data
    big = 'x' * 5000
    acc = f"source_account_id,account_name,industry,region,arr,blob\nACC-BIG,Too Big Co,Software,NA,1,{big}\n"
    r = upload_csv(cid, 'account_details.csv', acc)
    assert any('more than 4096 bytes of attributes' in w for w in r['warnings']), r['warnings']
    process_data(cid)
    with app.app_context():
        assert Account.query.filter_by(customer_id=cid, external_account_id='ACC-BIG').first() is None


def test_column_map_renames_and_promotes_at_upload(tenant):
    cid = tenant
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_onboarding import configure_column_map, get_column_map, upload_csv, process_data
    from mcp_server import audit
    with pytest.raises(ToolError, match='not columns of'):
        configure_column_map(cid, 'signals', {'body': 'not_a_real_column'})
    out = configure_column_map(cid, 'signals', {'body': 'content', 'when': 'occurred_at', 'acct': 'source_account_id',
                                                'attributes.deal_stage': 'use_case'})
    assert out['file_type'] == 'enhanced_qualitative_signals.csv' and out['column_map']['body'] == 'content'
    assert get_column_map(cid)['column_map']['enhanced_qualitative_signals.csv']['when'] == 'occurred_at'
    assert 'attributes' in get_column_map(cid)['accepted_columns']['outcomes.csv']
    with app.app_context():
        assert audit.query(cid, tool='configure_column_map')
    csv_text = ("acct,when,body,deal_stage,ticket_priority,signal_type\n"
                "ACC-1,2026-03-05T09:00:00,Finance asked us to justify the renewal line by line,Renewal defence,P2,budget_pressure\n")
    r = upload_csv(cid, 'signals.csv', csv_text)
    assert r['status'] == 'success', r
    assert any('column map applied' in w and 'body → content' in w for w in r['warnings']), r['warnings']
    assert any('folded into attributes' in w and 'ticket_priority' in w for w in r['warnings'])
    process_data(cid)
    with app.app_context():
        s = QualitativeSignal.query.filter_by(customer_id=cid, signal_type='budget_pressure').one()
        assert s.use_case == 'Renewal defence' and s.attributes == {'ticket_priority': 'P2'}
        n = db.session.get(ContextNode, s.cg_node_id)
        assert n.properties['attributes'] == {'ticket_priority': 'P2'} and n.properties['use_case'] == 'Renewal defence'


def test_kpi_file_catalog_owned_columns_and_attributes(tenant):
    cid = tenant
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data
    kp = ("source_account_id,kpi_code,measured_at,value,target,region_code,attributes\n"
          "ACC-1,P1-KPI1,2026-01-01,62,99,EMEA,\"{\"\"source_table\"\": \"\"fact_usage\"\"}\"\n"
          "ACC-1,NOT-A-KPI,2026-01-01,5,,,\n")
    r = upload_csv(cid, 'kpi_measurements.csv', kp)
    assert any('target differs from the catalog' in w for w in r['warnings']), r['warnings']
    assert any("kpi_code not in this tenant's catalog" in w and 'NOT-A-KPI' in w for w in r['warnings'])
    assert any('folded into attributes' in w and 'region_code' in w for w in r['warnings'])
    process_data(cid)
    with app.app_context():
        a = Account.query.filter_by(customer_id=cid, external_account_id='ACC-1').one()
        row = KPIMeasurement.query.filter_by(account_id=a.account_id, kpi_code='P1-KPI1').one()
        assert row.attributes == {'source_table': 'fact_usage', 'region_code': 'EMEA'} and float(row.value) == 62.0


def test_attributes_never_reach_the_model_prompt():
    from signal_engine.enrichment import USER_PROMPT, SYSTEM_PROMPT, roster_block, use_cases_block
    assert 'attributes' not in USER_PROMPT and 'attributes' not in SYSTEM_PROMPT
    assert 'attributes' not in roster_block([{'name': 'A', 'title': 'T', 'role': 'champion', 'attributes': {'x': 1}}])
    assert 'attributes' not in use_cases_block([{'name': 'DR', 'attributes': {'x': 1}}])
