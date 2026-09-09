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

from extensions import db


from mcp_server.common import get_flask_app
app = get_flask_app()

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
            # no password is generated and discarded any more (that left the account permanently
            # unusable) -- a one-time setup token is issued instead, consumed at POST
            # /app/api/auth/set-password (tests/test_app_api_auth.py exercises the token end to end)
            assert user.password_hash is None
            assert result['admin_setup_token'] and 'once' in result['admin_setup_token_note'].lower()

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
    """config/kpi_dependencies/<vertical>.json — one file per vertical (2026-09-07; was a
    single dc2_s-only flat file silently read for every vertical before this). vertical
    is now a required first argument, actually used to pick the file."""

    def test_no_warnings_when_using_defaults(self):
        from mcp_server.cs_pulse_onboarding import _check_kpi_dependencies
        assert _check_kpi_dependencies('dc2_s') == []

    def test_warns_on_disabled_dependent_pillar(self):
        from mcp_server.cs_pulse_onboarding import _check_kpi_dependencies
        warnings = _check_kpi_dependencies('dc2_s', enabled_pillars=['P1', 'P2', 'P3', 'P5'])  # P4 disabled
        assert len(warnings) == 1

    def test_warns_on_disabled_dependent_pillar_saas_premium_not_dc2s_text(self):
        """Same call shape as the dc2_s case above, different vertical — must return
        saas_premium's own warning, never dc2_s's P4 (Channel & Partner Health) text."""
        from mcp_server.cs_pulse_onboarding import _check_kpi_dependencies
        warnings = _check_kpi_dependencies('saas_premium', enabled_pillars=['P1', 'P2', 'P4', 'P5'])  # P3 disabled
        assert len(warnings) == 1
        assert 'Customer Sentiment & Support' in warnings[0]
        assert 'Channel & Partner Health' not in warnings[0] and 'GRR' not in warnings[0]

    def test_missing_vertical_file_fails_closed_not_silently_dc2s(self):
        """A vertical with no config/kpi_dependencies/<vertical>.json yet (e.g. a brand-new
        vertical added to the catalogs but not yet mapped here) must return no warnings —
        never silently fall back to dc2_s's or any other vertical's file.

        Uses a dedicated handler instead of caplog, and explicitly resets logger.disabled:
        full-suite-only failure, root-caused empirically (a debug run showed
        logger.disabled=True here even though level/propagate were fine, and it
        reproduces only in full-suite position, never in isolation or file-alone).
        Cause: tests/test_migrations.py runs Alembic in-process (same pattern as
        utils/schema.migrate()), which runs migrations/env.py — whose fileConfig() call
        (line 20) was never given disable_existing_loggers=False, so it uses Python's
        default of True, which sets .disabled=True on every logger that already existed
        and isn't named in alembic.ini's [loggers] section — including this one. That's
        a real, pre-existing bug (flagged separately, not fixed here — it also fires on
        every real server boot: server.py's build_asgi_app() imports every mcp_server/*
        module, creating their loggers, THEN calls utils.schema.migrate(), which hits
        the same fileConfig() call and would silently kill application-level logging
        for the life of that process). This test resets .disabled for itself rather
        than depend on that bug being fixed or on some other file's teardown being clean."""
        import logging
        from mcp_server.cs_pulse_onboarding import _check_kpi_dependencies
        messages = []
        collector = logging.Handler()
        collector.emit = lambda record: messages.append(record.getMessage())
        logger = logging.getLogger('mcp_server.cs_pulse_onboarding')
        logger.addHandler(collector)
        prev_level, prev_disabled = logger.level, logger.disabled
        logger.setLevel(logging.WARNING)               # pin THIS logger's own level so no ancestor's
        logger.disabled = False                        # level can suppress it, and undo dictConfig's
        prev_manager_disable = logging.root.manager.disable   # disable_existing_loggers=True poisoning.
        logging.disable(logging.NOTSET)                # Also neutralize the separate global kill-switch
        try:                                           # (Logger.manager.disable) in case anything set it.
            warnings = _check_kpi_dependencies('totally_unmapped_vertical_xyz', enabled_pillars=['P1'])
        finally:
            logger.removeHandler(collector)
            logger.setLevel(prev_level)
            logger.disabled = prev_disabled
            logging.disable(prev_manager_disable)
        assert warnings == []
        assert any('no dependency map yet' in m for m in messages)

    def test_vertical_key_mismatch_fails_closed(self, monkeypatch, tmp_path):
        """A defensive guard against exactly this task's bug class: a file that exists but
        whose own declared 'vertical' doesn't match the one asked for (e.g. a copy-paste
        mistake when adding a new vertical's file) must not be trusted."""
        import json
        import mcp_server.cs_pulse_onboarding as onboarding
        bogus = tmp_path / 'mismatched.json'
        bogus.write_text(json.dumps({
            'vertical': 'dc2_s',  # declares dc2_s...
            'dependencies': {}, 'pillar_dependencies': {'P1': {'warning': 'wrong-vertical warning'}},
        }))
        monkeypatch.setattr(onboarding, '_kpi_dependencies_path', lambda vertical: str(bogus))
        # ...but is being loaded for a different vertical than it declares
        warnings = onboarding._check_kpi_dependencies('saas_premium', enabled_pillars=['P2'])
        assert warnings == []


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
