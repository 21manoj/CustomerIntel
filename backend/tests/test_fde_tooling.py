"""
FDE tooling (build step 4), on real Postgres tenants:

  * validate_vertical — every shipped vertical passes; a broken file is reported in the FDE's words
  * preflight — the report of what staged files would do, read-only, before process_data
  * the load itself says which rows it dropped (result, steps, log) instead of loading fewer rows quietly
"""
import json
import logging
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
from models import Account, CsvUploadStaging, KPIMeasurement   # noqa: E402

ACCOUNTS = (
    "source_account_id,account_name,industry,region,arr,criticality_tier,regulated_critical_service,holds_customer_data,assurance_report_expiry,contract_end\n"
    "V1,Northwind Cloud,Cloud,NA,400000,critical,true,true,2027-01-15,2027-06-30\n"
    "V2,Baltic Payments,Payments,EU,250000,important,false,true,2027-03-01,\n"
    "V3,Cedar Print,Facilities,NA,30000,,false,false,,\n"
)
KPIS = (
    "source_account_id,kpi_code,measured_at,value\n"
    "V1,R-KPI2,2026-09-01,2\nV1,R-KPI2,2026-10-01,3\nV1,P-KPI1,2026-10-01,99\n"
    "V2,R-KPI2,2026-10-01,0\n"
    "GHOST,R-KPI2,2026-10-01,5\nGHOST,P-KPI1,2026-10-01,91\nPHANTOM,R-KPI2,2026-10-01,1\n"   # accounts that do not exist
    "V1,NOT-A-KPI,2026-10-01,7\n"                                                          # code the catalog lacks
    "V2,P-KPI1,not-a-date,97\n"                                                            # unreadable date
    "V2,P-KPI1,2026-10-01,\n"                                                              # blank value
)


@pytest.fixture(scope='module')
def ctx():
    with app.app_context():
        db.create_all()
        yield


def _new(vertical='tprm_v1'):
    from mcp_server.cs_pulse_onboarding import create_customer
    tag = uuid.uuid4().hex[:8]
    return create_customer(name=f'Fde {tag}', domain=f'fde-{tag}.test', vertical=vertical,
                           admin_email=f'fde_{tag}@t.test', admin_name='F', data_origin='synthetic_test')['customer_id']


# ── A. validate-my-vertical ──────────────────────────────────────────────────

def test_every_shipped_vertical_validates():
    from utils.vertical_registry import SUPPORTED_VERTICALS
    from utils.vertical_validation import validate_vertical
    for v in sorted(SUPPORTED_VERTICALS):
        report = validate_vertical(v)
        assert report['ok'], report
        ids = [c['id'] for c in report['checks']]
        assert {'catalog_file', 'catalog', 'capabilities', 'labels', 'taxonomy', 'playbooks', 'investment', 'ask_ai_questions'} <= set(ids)


def test_an_unknown_vertical_fails_on_the_first_check_and_names_the_file():
    from utils.vertical_validation import validate_vertical
    report = validate_vertical('no_such_vertical')
    assert not report['ok'] and len(report['checks']) == 1
    assert 'config/no_such_vertical_kpi_catalog.json' in report['checks'][0]['detail']


def test_the_exposure_vertical_reports_the_account_fields_its_economics_read():
    from utils.vertical_validation import validate_vertical
    econ = next(c for c in validate_vertical('tprm_v1')['checks'] if c['id'] == 'economics')
    assert econ['status'] == 'pass' and 'criticality_tier' in econ['detail']


def test_a_playbook_without_a_cost_entry_is_a_warning_and_a_stray_cost_entry_fails(monkeypatch):
    from roi import settings
    from utils.vertical_validation import validate_vertical
    real = settings.investment('saas_premium')
    first = sorted(real['playbooks'])[0]
    monkeypatch.setattr(settings, 'investment', lambda v: {**real, 'playbooks': {k: x for k, x in real['playbooks'].items() if k != first}})
    inv = next(c for c in validate_vertical('saas_premium')['checks'] if c['id'] == 'investment')
    assert inv['status'] == 'warn' and first in inv['detail']
    monkeypatch.setattr(settings, 'investment', lambda v: {**real, 'playbooks': {**real['playbooks'], 'made_up_playbook': {}}})
    report = validate_vertical('saas_premium')
    inv = next(c for c in report['checks'] if c['id'] == 'investment')
    assert inv['status'] == 'fail' and 'made_up_playbook' in inv['detail'] and not report['ok']


def test_a_loader_refusal_is_reported_not_raised(monkeypatch):
    from playbooks import definitions
    from utils.vertical_validation import render, validate_vertical

    def boom(v):
        raise definitions.PlaybookConfigError('x: trigger kpi code \'ZZ\' is not in the saas_premium KPI catalog')
    monkeypatch.setattr(definitions, 'load_vertical', boom)
    monkeypatch.setattr(definitions, 'reset_cache', lambda: None)
    report = validate_vertical('saas_premium')
    pb = next(c for c in report['checks'] if c['id'] == 'playbooks')
    assert pb['status'] == 'fail' and 'ZZ' in pb['detail'] and not report['ok']
    # the cost check does not pile a second, misleading failure on top of the unreadable playbook file
    assert next(c for c in report['checks'] if c['id'] == 'investment')['status'] != 'fail'
    assert '[FAIL] playbooks' in render(report) and 'NOT OK' in render(report)


def test_the_script_exits_nonzero_for_a_bad_vertical():
    import subprocess
    env = {**os.environ, 'PYTHONPATH': str(BACKEND)}
    good = subprocess.run([sys.executable, str(BACKEND / 'scripts' / 'validate_vertical.py'), 'tprm_v1'], capture_output=True, text=True, env=env)
    assert good.returncode == 0 and 'Vertical tprm_v1: OK' in good.stdout
    bad = subprocess.run([sys.executable, str(BACKEND / 'scripts' / 'validate_vertical.py'), 'nope'], capture_output=True, text=True, env=env)
    assert bad.returncode == 1 and 'NOT OK' in bad.stdout


# ── B. pre-flight ────────────────────────────────────────────────────────────

def test_preflight_with_nothing_staged_says_so(ctx):
    from utils.preflight import preflight
    out = preflight(_new())
    assert not out['ok'] and out['problems'][0]['code'] == 'nothing_staged'


def test_preflight_reports_what_the_load_would_do_and_writes_nothing(ctx):
    from mcp_server.cs_pulse_onboarding import preflight_data_report, upload_csv
    cid = _new()
    upload_csv(cid, 'account_details.csv', ACCOUNTS)
    upload_csv(cid, 'kpi_measurements.csv', KPIS)
    out = preflight_data_report(cid)

    assert out['vertical'] == 'tprm_v1' and out['staged_files'] == ['account_details.csv', 'kpi_measurements.csv']
    assert out['files']['account_details.csv']['rows'] == 3
    # a vertical's own columns are not platform columns: they are kept, in attributes
    assert 'criticality_tier' in out['files']['account_details.csv']['columns_kept_as_attributes']
    assert out['accounts']['new'] == 3 and out['accounts']['already_loaded'] == 0

    un = out['unmatched']['kpi_measurements.csv']
    assert un['unmatched_account'] == {'rows': 3, 'distinct': 2, 'examples': ['GHOST', 'PHANTOM'], 'more': 0}
    assert un['unknown_kpi_code']['examples'] == ['NOT-A-KPI']
    assert un['no_date']['rows'] == 1 and un['blank_value']['rows'] == 1

    k = out['kpis']
    assert k['months'] == {'first': '2026-09', 'last': '2026-10', 'count': 2}
    assert 'R-KPI2' not in k['core_kpis_without_a_reading'] and k['core_kpis_without_a_reading']
    assert k['staged_accounts_without_a_reading']['examples'] == ['Cedar Print']

    f = out['vertical_fields']
    assert f['criticality_tier']['accounts_with_a_value'] == 2 and f['criticality_tier']['accounts_missing']['examples'] == ['Cedar Print']
    assert any('exposure economics' in u for u in f['criticality_tier']['used_by'])
    assert any('assurance_lapse' in u for u in f['assurance_report_expiry']['used_by'])
    assert f['contract_end']['accounts_with_a_value'] == 1
    assert f['failure_cost']['accounts_with_a_value'] == 0

    dead = {d['playbook_id']: d for d in out['playbook_facts']['kpi_triggers_without_enough_readings']}
    assert 'remediation_overdue' not in dead or all(d['kpi'] != 'R-KPI2' for d in dead.values() if d['playbook_id'] == 'remediation_overdue')
    assert dead['sla_recovery']['months_in_file'] == 1 and dead['sla_recovery']['months_needed'] == 2

    codes = {p['code'] for p in out['problems']}
    assert {'rows_not_usable', 'core_kpis_missing', 'accounts_without_kpis', 'playbook_cannot_fire', 'vertical_field_partial'} <= codes
    assert not out['ok']                                  # dropped rows are an error before the load
    assert any('GHOST' in p['message'] and 'will NOT be loaded' in p['message'] for p in out['problems'])

    # read-only: nothing loaded, everything still staged
    assert Account.query.filter_by(customer_id=cid).count() == 0
    assert CsvUploadStaging.query.filter_by(customer_id=cid).count() == 2


def test_a_clean_upload_is_ok_and_a_field_inside_attributes_counts(ctx):
    from mcp_server.cs_pulse_onboarding import upload_csv
    from utils.preflight import preflight
    cid = _new()
    attrs = json.dumps({'criticality_tier': 'critical', 'regulated_critical_service': True, 'holds_customer_data': True,
                        'assurance_report_expiry': '2027-01-15', 'contract_end': '2027-06-30', 'failure_cost': 900000}).replace('"', '""')
    upload_csv(cid, 'account_details.csv', f'source_account_id,account_name,industry,region,arr,attributes\nV1,Northwind Cloud,Cloud,NA,400000,"{attrs}"\n')
    from utils.vertical_registry import get_kpis
    core = sorted(c for c, k in get_kpis('tprm_v1').items() if k.get('core'))
    rows = ''.join(f'V1,{c},{m},50\n' for c in core for m in ('2026-09-01', '2026-10-01'))
    upload_csv(cid, 'kpi_measurements.csv', 'source_account_id,kpi_code,measured_at,value\n' + rows)
    out = preflight(cid)
    assert out['unmatched'] == {} and out['kpis']['core_kpis_without_a_reading'] == []
    assert all(f['accounts_with_a_value'] == 1 for f in out['vertical_fields'].values()), out['vertical_fields']
    errors = [p for p in out['problems'] if p['severity'] == 'error']
    assert out['ok'] and not errors, out['problems']
    assert 'Safe to run process_data.' in out['summary'][-1]


def test_preflight_for_a_revenue_vertical_reads_only_what_its_config_reads(ctx):
    from mcp_server.cs_pulse_onboarding import upload_csv
    from utils.preflight import preflight
    cid = _new('saas_premium')
    upload_csv(cid, 'account_details.csv', 'source_account_id,account_name,industry,region,arr,renewal_date\nA,Acme,SaaS,NA,100000,2027-02-01\nB,Birch,SaaS,NA,50000,\n')
    upload_csv(cid, 'kpi_measurements.csv', 'source_account_id,kpi_code,measured_at,value\nA,P1-KPI1,2026-09-01,35\nA,P1-KPI1,2026-10-01,30\n')
    out = preflight(cid)
    assert set(out['vertical_fields']) == {'renewal_date'}        # no exposure fields for a revenue vertical
    assert out['vertical_fields']['renewal_date']['accounts_with_a_value'] == 1
    assert out['playbook_facts']['kpi_triggers_without_enough_readings'] == []


def test_the_preflight_tool_is_registered_and_keyed():
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS
    assert 'preflight_data_report' in KEYED_TOOLS


# ── C. the load says what it dropped ─────────────────────────────────────────

def test_process_data_names_the_rows_it_dropped(ctx, caplog):
    from mcp_server.cs_pulse_onboarding import process_data, upload_csv
    cid = _new()
    upload_csv(cid, 'account_details.csv', ACCOUNTS)
    upload_csv(cid, 'kpi_measurements.csv', KPIS)
    with caplog.at_level(logging.WARNING, logger='utils.csv_ingest'):
        res = process_data(cid)
    assert res['status'] == 'success', res                 # a warning, not a failure
    assert 'UNMATCHED_ROWS_kpi_measurements.csv_3' in res['steps_completed']
    w = ' '.join(res['data_warnings'])
    assert '3 of 10 rows name an account that is not in this tenant and were NOT loaded' in w and 'GHOST' in w and 'PHANTOM' in w
    assert 'NOT-A-KPI' in w and 'no readable date' in w
    assert 'DATA WARNINGS' in res['message']
    assert any('GHOST' in r.getMessage() for r in caplog.records)
    # and the count in the warning is the truth: none of the ghost rows landed
    ids = [a.account_id for a in Account.query.filter_by(customer_id=cid).all()]
    assert len(ids) == 3
    loaded = KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(ids)).count()
    assert loaded <= 5
    from models import ProcessRun
    run = ProcessRun.query.filter_by(run_id=res['run_id']).one()
    assert run.counts['unmatched_rows']['kpi_measurements.csv']['unmatched_account']['rows'] == 3


def test_a_clean_load_has_no_data_warnings(ctx):
    from mcp_server.cs_pulse_onboarding import process_data, upload_csv
    cid = _new()
    upload_csv(cid, 'account_details.csv', ACCOUNTS)
    upload_csv(cid, 'kpi_measurements.csv', 'source_account_id,kpi_code,measured_at,value\nV1,R-KPI2,2026-10-01,2\nV2,R-KPI2,2026-10-01,0\n')
    res = process_data(cid)
    assert res['data_warnings'] == [] and not any(s.startswith('UNMATCHED_ROWS') for s in res['steps_completed'])
    assert 'DATA WARNINGS' not in res['message']
