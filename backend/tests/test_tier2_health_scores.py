"""
Tier 2A-4 checkpoint: health scoring on the ORM, end to end through
process_data against a real Postgres DB. Covers: one HealthScore per
(account, month) with kpi_only_score == health_score; correct
month-over-month deltas (the old raw-SQL version wrote 0.00 everywhere);
immutability in 'auto' vs rewrite in 'full_recalc'; item-28 account_status
sync; lifecycle-stage pillar weights actually reaching the scorer (the old
pipeline's TypeError fallback silently dropped them); adoption back-fill
using the vertical's adoption pillar, not a hardcoded P1.
"""
import os
import sys
import uuid
from datetime import date

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

from models import Account, CustomerConfig, HealthScore
import utils.health_thresholds as ht


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_health_scores.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


ACCOUNTS_CSV = (
    "source_account_id,account_name,industry,region,arr,products,contract_start\n"
    'ACC-1,Titan,Telco,NA,8200000,"[{""name"": ""Managed Kubernetes"", ""arr"": 100}]",2025-06-15\n'
    'ACC-2,Meridian,AI/ML,NA,4100000,,2025-05-20\n'
)

# datacenter_v1: P1 = Revenue & Unit Economics, P6 = Provisioning Velocity
# (its adoption pillar). Two months for ACC-1, one for ACC-2.
KPI_CSV = (
    "source_account_id,kpi_code,measured_at,value\n"
    "ACC-1,P1-KPI1,2025-11-01,1.2\n"
    "ACC-1,P6-KPI1,2025-11-01,40\n"
    "ACC-1,P1-KPI1,2025-12-01,2.4\n"
    "ACC-1,P6-KPI1,2025-12-01,80\n"
    "ACC-2,P1-KPI1,2025-11-01,2.0\n"
    "ACC-2,P6-KPI1,2025-11-01,60\n"
)


def _new_customer(prefix: str, vertical: str = 'datacenter_v1') -> int:
    from mcp_server.cs_pulse_onboarding import create_customer
    tag = uuid.uuid4().hex[:8]
    return create_customer(data_origin='synthetic_test', 
        name=f'{prefix} {tag}', domain=f'{prefix}-{tag}.test', vertical=vertical,
        admin_email=f'{prefix}_{tag}@t.test', admin_name='A',
    )['customer_id']


def _upload(cid, files: dict):
    from mcp_server.cs_pulse_onboarding import upload_csv
    for ft, content in files.items():
        upload_csv(cid, ft, content)


def _scores(cid):
    return {
        (hs.account.account_name if hasattr(hs, 'account') else hs.account_id, hs.measurement_month): hs
        for hs in HealthScore.query.join(Account, Account.account_id == HealthScore.account_id)
        .filter(Account.customer_id == cid).all()
    }


def _scores_by_name(cid):
    accts = {a.account_id: a.account_name for a in Account.query.filter_by(customer_id=cid).all()}
    return {
        (accts[hs.account_id], hs.measurement_month): hs
        for hs in HealthScore.query.filter(HealthScore.account_id.in_(list(accts))).all()
    }


@pytest.fixture(scope='module')
def customer_id():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        cid = _new_customer('health')
        _upload(cid, {'account_details.csv': ACCOUNTS_CSV, 'kpi_measurements.csv': KPI_CSV})
        from mcp_server.cs_pulse_onboarding import process_data
        res = process_data(cid)
        assert res['status'] == 'success', res
        yield cid
        db.session.remove()
        db.drop_all()


class TestScoresWritten:
    def test_one_row_per_account_month(self, customer_id):
        with app.app_context():
            s = _scores_by_name(customer_id)
            assert set(s) == {
                ('Titan', date(2025, 11, 1)), ('Titan', date(2025, 12, 1)), ('Meridian', date(2025, 11, 1)),
            }
            for hs in s.values():
                assert hs.health_score is not None
                assert hs.kpi_only_score == hs.health_score
                assert hs.health_status == ht.classify(float(hs.health_score))
                assert set(hs.contributing_pillars) == {'P1', 'P6'}
                assert hs.calculated_at is not None

    def test_step_reports_rows_actually_inserted(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        res = process_data(customer_id)   # nothing new → 0 written, not 3
        assert 'health_scores_auto_0_written' in res['steps_completed']

    def test_month_over_month_delta_is_real(self, customer_id):
        with app.app_context():
            s = _scores_by_name(customer_id)
            nov, dec = s[('Titan', date(2025, 11, 1))], s[('Titan', date(2025, 12, 1))]
            assert nov.change_from_last_month is None
            assert float(dec.change_from_last_month) == round(float(dec.health_score) - float(nov.health_score), 2)
            assert float(dec.change_from_last_month) != 0.0
            assert s[('Meridian', date(2025, 11, 1))].change_from_last_month is None


class TestImmutability:
    def test_auto_mode_keeps_old_months_full_recalc_rewrites(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        with app.app_context():
            before = {k: float(v.health_score) for k, v in _scores_by_name(customer_id).items()}
            cc = CustomerConfig.query.filter_by(customer_id=customer_id).first()
            cc.pillar_weights = {'P1': 0.99, 'P6': 0.01}   # a change that must move the score
            db.session.commit()
        _upload(customer_id, {'kpi_measurements.csv': (
            "source_account_id,kpi_code,measured_at,value\n"
            "ACC-2,P1-KPI1,2025-12-01,2.2\nACC-2,P6-KPI1,2025-12-01,30\n"
        )})
        res = process_data(customer_id)
        assert 'health_scores_auto_1_written' in res['steps_completed'], res['steps_completed']
        with app.app_context():
            after = {k: float(v.health_score) for k, v in _scores_by_name(customer_id).items()}
            for k, v in before.items():
                assert after[k] == v, f'{k} rewritten in auto mode'
            assert ('Meridian', date(2025, 12, 1)) in after

        res = process_data(customer_id, mode='full_recalc')
        assert 'health_scores_full_recalc_4_written' in res['steps_completed'], res['steps_completed']
        with app.app_context():
            recalc = {k: float(v.health_score) for k, v in _scores_by_name(customer_id).items()}
            assert any(recalc[k] != before[k] for k in before), 'full_recalc changed nothing'
            for hs in _scores_by_name(customer_id).values():
                assert hs.kpi_only_score == hs.health_score
            # deltas recomputed against the rewritten history
            s = _scores_by_name(customer_id)
            assert float(s[('Titan', date(2025, 12, 1))].change_from_last_month) == round(
                recalc[('Titan', date(2025, 12, 1))] - recalc[('Titan', date(2025, 11, 1))], 2)
            cc = CustomerConfig.query.filter_by(customer_id=customer_id).first()
            cc.pillar_weights = None
            db.session.commit()


class TestOpenMonthRescoring:
    def test_late_rows_for_a_scored_month_reopen_it_in_auto_mode(self):
        """Immutability protects closed months, not half-scored ones: a
        second upload with more rows for an already-scored month rescores
        that month (and only that month) in 'auto' mode."""
        from mcp_server.cs_pulse_onboarding import process_data
        with app.app_context():
            cid = _new_customer('reopen')
        _upload(cid, {'account_details.csv': ACCOUNTS_CSV, 'kpi_measurements.csv': (
            "source_account_id,kpi_code,measured_at,value\n"
            "ACC-1,P1-KPI1,2025-11-03,1.0\nACC-1,P6-KPI1,2025-11-03,40\n"
            "ACC-1,P1-KPI1,2025-12-03,1.0\nACC-1,P6-KPI1,2025-12-03,40\n"
        )})
        res = process_data(cid)
        assert 'health_scores_auto_2_written' in res['steps_completed'], res['steps_completed']
        with app.app_context():
            before = {k: float(v.health_score) for k, v in _scores_by_name(cid).items()}
        # the rest of December arrives: much better numbers
        _upload(cid, {'kpi_measurements.csv': (
            "source_account_id,kpi_code,measured_at,value\n"
            "ACC-1,P1-KPI1,2025-12-17,3.0\nACC-1,P6-KPI1,2025-12-17,95\n"
            "ACC-1,P1-KPI1,2025-12-24,3.0\nACC-1,P6-KPI1,2025-12-24,95\n"
        )})
        res = process_data(cid)
        assert 'health_scores_auto_1_written_1_reopened' in res['steps_completed'], res['steps_completed']
        with app.app_context():
            after = {k: float(v.health_score) for k, v in _scores_by_name(cid).items()}
            assert after[('Titan', date(2025, 11, 1))] == before[('Titan', date(2025, 11, 1))]   # closed month untouched
            assert after[('Titan', date(2025, 12, 1))] != before[('Titan', date(2025, 12, 1))]   # open month rescored
            assert after[('Titan', date(2025, 12, 1))] > before[('Titan', date(2025, 12, 1))]
        # nothing new → nothing reopened
        res = process_data(cid)
        assert 'health_scores_auto_0_written' in res['steps_completed'], res['steps_completed']
        assert not any('reopened' in s for s in res['steps_completed'])


class TestAccountStatusSync:
    def test_item28_mapping_and_churned_terminal(self):
        from mcp_server.process_data_pipeline import _sync_account_status
        with app.app_context():
            cid = _new_customer('sync')
            names = {'H': 'healthy', 'A': 'at_risk', 'C': 'critical', 'X': 'healthy'}
            ids = {}
            for n in names:
                a = Account(customer_id=cid, account_name=n, account_status='churned' if n == 'X' else 'onboarding')
                db.session.add(a)
                db.session.flush()
                ids[n] = a.account_id
                db.session.add(HealthScore(account_id=a.account_id, measurement_month=date(2026, 1, 1),
                                           health_score=50, health_status='critical'))
                db.session.add(HealthScore(account_id=a.account_id, measurement_month=date(2026, 2, 1),
                                           health_score=50, health_status=names[n]))
            db.session.commit()
            synced = _sync_account_status(cid, set(ids.values()))
            assert synced == 3
            got = {a.account_name: a.account_status for a in Account.query.filter_by(customer_id=cid).all()}
            assert got == {'H': 'active', 'A': 'at_risk', 'C': 'at_risk', 'X': 'churned'}


class TestLifecycleWeightsApplied:
    def test_stage_pillar_weights_reach_the_scorer(self):
        """A stage weighting P6 alone must make health == the P6 pillar
        score. In the old pipeline this override never reached the scorer."""
        from mcp_server.cs_pulse_onboarding import process_data
        with app.app_context():
            cid = _new_customer('lifecycle')
            cc = CustomerConfig.query.filter_by(customer_id=cid).first()
            cc.lifecycle_stage_weights = {
                'enabled': True, 'date_field': 'contract_start',
                'stages': [{'name': 'all', 'min_days': 0, 'max_days': None, 'pillar_weights': {'P6': 1.0}}],
            }
            db.session.commit()
        _upload(cid, {'account_details.csv': ACCOUNTS_CSV, 'kpi_measurements.csv': KPI_CSV})
        res = process_data(cid)
        assert res['status'] == 'success', res
        with app.app_context():
            for hs in _scores_by_name(cid).values():
                assert set(hs.contributing_pillars) == {'P6'}
                assert float(hs.health_score) == round(hs.contributing_pillars['P6'], 2)

    def test_disabled_config_uses_default_weights(self, customer_id):
        with app.app_context():
            for hs in _scores_by_name(customer_id).values():
                assert set(hs.contributing_pillars) == {'P1', 'P6'}


class TestAdoptionBackfill:
    def test_uses_vertical_adoption_pillar_not_p1(self, customer_id):
        with app.app_context():
            titan = Account.query.filter_by(customer_id=customer_id, account_name='Titan').first()
            latest = HealthScore.query.filter_by(account_id=titan.account_id).order_by(
                HealthScore.measurement_month.desc()).first()
            expected = round(float(latest.contributing_pillars['P6']), 1)
            assert titan.profile_metadata['product_adoption'] == expected
            assert titan.profile_metadata['products'][0]['adoption'] == expected
            assert expected != round(float(latest.contributing_pillars['P1']), 1)
            # Meridian has no products → untouched
            meridian = Account.query.filter_by(customer_id=customer_id, account_name='Meridian').first()
            assert 'product_adoption' not in (meridian.profile_metadata or {})

    def test_vertical_without_adoption_pillar_is_noop(self):
        from mcp_server.process_data_pipeline import backfill_product_adoption
        with app.app_context():
            assert backfill_product_adoption(0, [], 'healthcare_provider') is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
