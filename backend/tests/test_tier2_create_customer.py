"""
Tier 2A-1 checkpoint: create_customer and its private helpers.

Real DB execution, not an import check: creates a customer end-to-end via
create_customer(data_origin='synthetic_test', ) and asserts on the actual rows written (Customer, User,
CustomerConfig, FeatureToggle), including the SaaS-tier KPI/pillar-weight
side effects and the duplicate-domain/email guards.
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

# create_customer(data_origin='synthetic_test', ) opens its own app context via _get_flask_app() (a
# module-level singleton in mcp_server/common.py) — point that singleton
# at this same test app/DB rather than letting it build its own from
# DATABASE_URL a second time.
import mcp_server.common as _common
_common._flask_app = app

from models import Customer, User, CustomerConfig, FeatureToggle
from fastmcp.exceptions import ToolError


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_create_customer.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


@pytest.fixture(scope='module', autouse=True)
def _setup_db():
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    _assert_isolated_test_db(db_uri)
    with app.app_context():
        db.create_all()
    yield
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _unique_domain(prefix='cc'):
    return f'{prefix}-{uuid.uuid4().hex[:8]}.test'


class TestCreateCustomerBasics:
    def test_creates_customer_admin_user_config(self):
        from mcp_server.cs_pulse_onboarding import create_customer
        domain = _unique_domain()
        result = create_customer(data_origin='synthetic_test', 
            name='Acme DC Co', domain=domain, vertical='datacenter_v1',
            admin_email=f'admin_{uuid.uuid4().hex[:8]}@{domain}',
            admin_name='Admin Person',
        )
        assert result['scope'] == 'customer'
        assert result['vertical'] == 'datacenter_v1'
        assert result['domain'] == domain

        with app.app_context():
            customer = Customer.query.get(result['customer_id'])
            assert customer is not None
            assert customer.domain == domain
            assert customer.vertical == 'datacenter_v1'

            user = User.query.get(result['admin_user_id'])
            assert user is not None
            assert user.role == 'admin'
            assert user.password_hash is not None

            config = CustomerConfig.query.filter_by(customer_id=result['customer_id']).first()
            assert config is not None
            assert config.vertical == 'datacenter_v1'
            # Non-SaaS vertical — no tier applied, full catalog
            assert config.enabled_kpis is None
            assert 'tier' not in result

    def test_all_seven_feature_toggles_created(self):
        from mcp_server.cs_pulse_onboarding import create_customer
        domain = _unique_domain()
        result = create_customer(data_origin='synthetic_test', 
            name='Feature Toggle Co', domain=domain, vertical='datacenter_v1',
            admin_email=f'admin_{uuid.uuid4().hex[:8]}@{domain}',
            admin_name='Admin Person',
        )
        with app.app_context():
            toggles = FeatureToggle.query.filter_by(customer_id=result['customer_id']).all()
            names = {t.feature_name for t in toggles}
            assert names == {
                'context_graph', 'story_arcs', 'signal_edges',
                'stakeholder_tracking', 'decision_lifecycle',
                'outcome_economics', 'industry_benchmarks',
            }
            assert all(t.enabled for t in toggles)
            cg = next(t for t in toggles if t.feature_name == 'context_graph')
            assert cg.config.get('story_arcs') is True

    def test_duplicate_domain_rejected(self):
        from mcp_server.cs_pulse_onboarding import create_customer
        domain = _unique_domain()
        create_customer(data_origin='synthetic_test', 
            name='First Co', domain=domain, vertical='datacenter_v1',
            admin_email=f'admin1_{uuid.uuid4().hex[:8]}@{domain}', admin_name='A',
        )
        with pytest.raises(ToolError):
            create_customer(data_origin='synthetic_test', 
                name='Second Co', domain=domain, vertical='datacenter_v1',
                admin_email=f'admin2_{uuid.uuid4().hex[:8]}@{domain}', admin_name='B',
            )

    def test_duplicate_admin_email_rejected(self):
        from mcp_server.cs_pulse_onboarding import create_customer
        email = f'dup_{uuid.uuid4().hex[:8]}@test.com'
        create_customer(data_origin='synthetic_test', 
            name='Third Co', domain=_unique_domain(), vertical='datacenter_v1',
            admin_email=email, admin_name='A',
        )
        with pytest.raises(ToolError):
            create_customer(data_origin='synthetic_test', 
                name='Fourth Co', domain=_unique_domain(), vertical='datacenter_v1',
                admin_email=email, admin_name='B',
            )


class TestSaasKpiTier:
    def test_default_tier_applied_for_saas_premium(self):
        from mcp_server.cs_pulse_onboarding import create_customer
        domain = _unique_domain()
        result = create_customer(data_origin='synthetic_test', 
            name='SaaS Default Co', domain=domain, vertical='saas_premium',
            admin_email=f'admin_{uuid.uuid4().hex[:8]}@{domain}', admin_name='Admin',
        )
        assert result['tier']['name'] == 'SaaS Starter 9'
        with app.app_context():
            config = CustomerConfig.query.filter_by(customer_id=result['customer_id']).first()
            assert set(config.enabled_kpis) == {
                'P1-KPI1', 'P1-KPI3', 'P2-KPI1', 'P3-KPI1', 'P3-KPI3',
                'P3-KPI4', 'P5-KPI1', 'P5-KPI2', 'P5-KPI3',
            }
            assert set(config.pillar_weights.keys()) == {'P1', 'P2', 'P3', 'P5'}
            assert pytest.approx(sum(config.pillar_weights.values()), abs=1e-9) == 1.0

    def test_explicit_full_tier_clears_restriction(self):
        from mcp_server.cs_pulse_onboarding import create_customer
        domain = _unique_domain()
        result = create_customer(data_origin='synthetic_test', 
            name='SaaS Full Co', domain=domain, vertical='saas_premium',
            admin_email=f'admin_{uuid.uuid4().hex[:8]}@{domain}', admin_name='Admin',
            tier='saas_full_43',
        )
        with app.app_context():
            config = CustomerConfig.query.filter_by(customer_id=result['customer_id']).first()
            assert config.enabled_kpis is None
            assert config.pillar_weights is None


class TestCheckKpiDependencies:
    def test_no_warnings_when_using_defaults(self):
        from mcp_server.cs_pulse_onboarding import _check_kpi_dependencies
        assert _check_kpi_dependencies() == []

    def test_warns_on_disabled_dependent_pillar(self):
        from mcp_server.cs_pulse_onboarding import _check_kpi_dependencies
        warnings = _check_kpi_dependencies(enabled_pillars=['P1', 'P2', 'P3', 'P5'])  # P4 disabled
        assert len(warnings) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
