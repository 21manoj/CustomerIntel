"""
Tier 2A checkpoint: MCP server foundation (mcp_server/common.py,
mcp_server/auth.py, mcp_server/cs_pulse_mcp_server.py helpers).

Not a syntax/import check — exercises real DB reads and proves the fix
made during the port actually works: _get_health_functions/
_get_trailing_kpi_values_generic used to read the old repo's dead
KPIScore table (zero live rows there) and always return {}. This test
inserts real KPIMeasurement rows and asserts trailing KPI values compute
from them, plus the two-tier vertical resolution and the playbook-config
safe stub.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

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

from models import Account, Customer, CustomerConfig, KPIMeasurement
from fastmcp.exceptions import ToolError


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_mcp_foundation.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


@pytest.fixture(scope='module')
def fixture():
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    _assert_isolated_test_db(db_uri)
    with app.app_context():
        db.create_all()

        # Customer resolved via CustomerConfig.vertical (tier 1)
        c1 = Customer(customer_name='MCP Tier1 Co', email=f'mcp1_{uuid.uuid4().hex[:8]}@test.com')
        db.session.add(c1)
        db.session.commit()
        db.session.add(CustomerConfig(customer_id=c1.customer_id, vertical='datacenter_v1'))

        # Customer resolved via legacy Customer.vertical short code (tier 2)
        c2 = Customer(customer_name='MCP Tier2 Co', email=f'mcp2_{uuid.uuid4().hex[:8]}@test.com',
                      vertical='saas')
        db.session.add(c2)

        # Customer with neither set — must fail closed, no dc2_s fallback
        c3 = Customer(customer_name='MCP Unresolvable Co', email=f'mcp3_{uuid.uuid4().hex[:8]}@test.com')
        db.session.add(c3)
        db.session.commit()

        account = Account(
            customer_id=c1.customer_id,
            account_name='MCP Test Account',
            revenue=1_000_000,
            external_account_id=f'MCP-{uuid.uuid4().hex[:8]}',
            account_status='active',
        )
        db.session.add(account)
        db.session.commit()

        now = datetime.utcnow()
        db.session.add_all([
            KPIMeasurement(account_id=account.account_id, kpi_code='P1-KPI1',
                           value=80.0, pillar='P1', measured_at=now - timedelta(days=2)),
            KPIMeasurement(account_id=account.account_id, kpi_code='P1-KPI1',
                           value=60.0, pillar='P1', measured_at=now - timedelta(days=10)),
            KPIMeasurement(account_id=account.account_id, kpi_code='P2-KPI1',
                           value=90.0, pillar='P2', measured_at=now - timedelta(days=1)),
        ])
        db.session.commit()

        yield {
            'c1_id': c1.customer_id, 'c2_id': c2.customer_id, 'c3_id': c3.customer_id,
            'account_id': account.account_id,
        }

        db.session.remove()
        db.drop_all()


class TestResolveCustomerVertical:
    def test_tier1_customer_config_wins(self, fixture):
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _resolve_customer_vertical
            assert _resolve_customer_vertical(fixture['c1_id']) == 'datacenter_v1'

    def test_tier2_legacy_short_code_normalized(self, fixture):
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _resolve_customer_vertical
            assert _resolve_customer_vertical(fixture['c2_id']) == 'saas_premium'

    def test_fails_closed_no_dc2s_fallback(self, fixture):
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _resolve_customer_vertical
            with pytest.raises(ToolError):
                _resolve_customer_vertical(fixture['c3_id'])


class TestHealthFunctionsUseRealKpiMeasurementTable:
    def test_trailing_kpi_values_computed_from_kpi_measurement(self, fixture):
        """The bug this test guards: _get_trailing_kpi_values_generic used to
        query the dead KPIScore table and always return {}. It should now
        return real averaged values from KPIMeasurement.
        """
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _get_health_functions
            _, get_trailing, _ = _get_health_functions(fixture['c1_id'])
            values = get_trailing(fixture['account_id'], days=30)
            assert values.get('P1-KPI1') == pytest.approx(70.0)  # avg(80, 60)
            assert values.get('P2-KPI1') == pytest.approx(90.0)

    def test_calculate_kpi_health_returns_nonzero_for_real_data(self, fixture):
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _get_health_functions
            calculate, get_trailing, _ = _get_health_functions(fixture['c1_id'])
            kpi_values = get_trailing(fixture['account_id'], days=30)
            overall, pillar_scores = calculate(kpi_values, customer_id=fixture['c1_id'])
            assert overall > 0


class TestPillarLabelsAndKpiDefinitions:
    def test_pillar_labels_resolved_for_datacenter_v1(self, fixture):
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _get_pillar_labels
            labels = _get_pillar_labels('datacenter_v1')
            assert len(labels) == 6
            assert 'P1' in labels

    def test_kpi_definitions_resolved_for_datacenter_v1(self, fixture):
        with app.app_context():
            from mcp_server.cs_pulse_mcp_server import _get_kpi_definitions
            defs = _get_kpi_definitions('datacenter_v1')
            assert len(defs) == 38


class TestPlaybookConfigSafeStub:
    def test_dc2s_does_not_crash_returns_empty_stub(self, fixture):
        """Guards against the crash risk found during the port: the old
        repo's dc2_s branch imported verticals.dc2_s.vertical_config with
        no try/except, which doesn't exist in this build's filesystem.
        """
        from mcp_server.cs_pulse_mcp_server import _get_playbook_config
        config, should_trigger = _get_playbook_config('dc2_s')
        assert config == {}
        assert should_trigger('anything') is False


class TestAuthStdioTrusted:
    def test_require_auth_is_noop_on_stdio(self, fixture, monkeypatch):
        monkeypatch.delenv('MCP_TRANSPORT', raising=False)
        from mcp_server.cs_pulse_mcp_server import _require_auth
        _require_auth(fixture['c1_id'], 'write')  # must not raise


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
