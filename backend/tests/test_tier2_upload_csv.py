"""
Tier 2A-2 checkpoint: upload_csv and the DB-staging replacement for the
old repo's disk-based CSV storage (see models.CsvUploadStaging's
docstring). Real DB execution — inserts a customer, uploads CSVs, asserts
on the actual staged rows and the upsert-on-reupload behavior.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from extensions import db


def _make_app():
    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test'
    )
    db.init_app(_app)
    return _app


app = _make_app()

import mcp_server.common as _common
_common._flask_app = app

from models import Customer, CsvUploadStaging
from fastmcp.exceptions import ToolError

ACCOUNTS_CSV = (
    "source_account_id,customer_id,account_name,industry,region,arr,renewal_date\n"
    "ACC-1,1,Acme Corp,Tech,NA,500000,2027-01-01\n"
    "ACC-2,1,Globex Inc,Tech,EMEA,750000,2027-03-15\n"
)

ACCOUNTS_CSV_MISSING_COLUMN = (
    "source_account_id,account_name,industry,region,arr,renewal_date\n"
    "ACC-1,Acme Corp,Tech,NA,500000,2027-01-01\n"
)


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_upload_csv.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


@pytest.fixture(scope='module')
def customer_id():
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    _assert_isolated_test_db(db_uri)
    with app.app_context():
        db.create_all()
        c = Customer(customer_name='Upload Test Co', email=f'upload_{uuid.uuid4().hex[:8]}@test.com',
                     domain=f'upload-{uuid.uuid4().hex[:8]}.test')
        db.session.add(c)
        db.session.commit()
        cid = c.customer_id
        yield cid
        db.session.remove()
        db.drop_all()


class TestUploadCsvDryRun:
    def test_dry_run_valid_csv_does_not_persist(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        result = upload_csv(customer_id, 'accounts.csv', ACCOUNTS_CSV, dry_run=True)
        assert result['scope'] == 'validation'
        assert result['valid'] is True
        assert result['row_count'] == 2
        with app.app_context():
            assert CsvUploadStaging.query.filter_by(customer_id=customer_id, file_type='accounts.csv').first() is None

    def test_dry_run_missing_required_column_reported(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        result = upload_csv(customer_id, 'accounts.csv', ACCOUNTS_CSV_MISSING_COLUMN, dry_run=True)
        assert result['valid'] is False
        assert any('customer_id' in e for e in result['missing_required'])


class TestUploadCsvPersist:
    def test_valid_upload_stages_to_db(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        result = upload_csv(customer_id, 'accounts.csv', ACCOUNTS_CSV)
        assert result['scope'] == 'customer'
        assert result['row_count'] == 2
        with app.app_context():
            staged = CsvUploadStaging.query.filter_by(customer_id=customer_id, file_type='accounts.csv').first()
            assert staged is not None
            assert staged.row_count == 2
            assert 'ACC-1' in staged.csv_content

    def test_reupload_same_file_type_replaces_not_duplicates(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        updated_csv = ACCOUNTS_CSV + "ACC-3,1,Initech,Tech,APAC,300000,2027-06-01\n"
        upload_csv(customer_id, 'accounts.csv', updated_csv)
        with app.app_context():
            rows = CsvUploadStaging.query.filter_by(customer_id=customer_id, file_type='accounts.csv').all()
            assert len(rows) == 1  # upsert, not a second row
            assert rows[0].row_count == 3
            assert 'ACC-3' in rows[0].csv_content

    def test_alias_resolves_to_canonical_filename(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        result = upload_csv(customer_id, 'kpis', (
            "source_account_id,kpi_code,measured_at,value\n"
            "ACC-1,P1-KPI1,2026-08-01,80\n"
        ))
        assert result['canonical_filename'] == 'kpi_measurements.csv'
        with app.app_context():
            assert CsvUploadStaging.query.filter_by(
                customer_id=customer_id, file_type='kpi_measurements.csv',
            ).first() is not None

    def test_load_driver_shaped_file_passes_strict_validation(self, customer_id):
        """account_id satisfies the source_account_id requirement, and the
        schema's recommended columns are known, not 'ignored' — the exact
        shape the load-driver emits (see utils/csv_upload._COLUMN_ALIASES)."""
        from mcp_server.cs_pulse_onboarding import upload_csv
        result = upload_csv(customer_id, 'account_details.csv', (
            "account_id,account_name,industry,region,arr,csm_name,products,renewal_date\n"
            '359001,Titan,Telco,NA,8200000,Sarah,"[{""name"": ""K8s""}]",2026-06-15\n'
        ), dry_run=True)
        assert result['valid'] is True, result
        assert not result.get('missing_required')
        assert not any('Unknown columns' in w for w in result.get('warnings', [])), result['warnings']

    def test_outcomes_linked_signal_id_is_a_known_column(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        result = upload_csv(customer_id, 'outcomes.csv', (
            "account_id,outcome_date,outcome_type,title,revenue_value,linked_signal_id\n"
            "359001,2026-03-01,revenue_at_risk,At risk,-100,narrative_sig_1\n"
        ), dry_run=True)
        assert result['valid'] is True, result
        assert not any('Unknown columns' in w for w in result.get('warnings', [])), result['warnings']

    def test_schema_defines_each_file_once_with_origin_flags(self):
        """csv_schemas.json used to define every context-graph file twice
        (a flat `files` block shadowing the categorized sections), so
        columns added to the visible definition were silently ignored and
        the auto_generated/platform_curated flags were lost. Lock the
        single-definition shape and the flags the registry derives from it."""
        import json
        from utils.csv_upload import get_registry, _SCHEMAS_PATH
        schema = json.load(open(_SCHEMAS_PATH))
        ctx = schema['context_graph_model']
        assert 'files' not in ctx
        seen = [f for k in ('customer_provided', 'auto_generated', 'platform_curated')
                for f in ctx.get(k, {}) if f.endswith('.csv')]
        assert len(seen) == len(set(seen)) == 7
        reg = get_registry()
        assert reg['decisions.csv'].auto_generated and reg['signal_edges.csv'].auto_generated
        assert reg['industry_benchmarks.csv'].platform_curated
        assert not reg['outcomes.csv'].auto_generated
        assert 'linked_signal_id' in reg['outcomes.csv'].optional_columns

    def test_invalid_csv_raises_tool_error_not_dry_run(self, customer_id):
        from mcp_server.cs_pulse_onboarding import upload_csv
        with pytest.raises(ToolError):
            upload_csv(customer_id, 'accounts.csv', ACCOUNTS_CSV_MISSING_COLUMN)

    def test_unknown_customer_errors(self):
        from mcp_server.cs_pulse_onboarding import upload_csv
        with pytest.raises(ToolError):
            upload_csv(999999999, 'accounts.csv', ACCOUNTS_CSV)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
