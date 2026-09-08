"""
Schema is managed by Alembic (utils/schema.migrate at boot), not create_all:
  * an empty DB is created at head by the revisions alone and matches the models exactly
  * a pre-Alembic DB (built by create_all, no alembic_version) is stamped at the baseline then upgraded
  * a DB at head is a no-op; there is exactly one head
  * the models and the revisions agree — a model change without a revision fails here
  * 0005's data back-fill actually runs on a pre-Alembic database and scopes every
    existing user to their own tenant (the cross-tenant leak's other half)
"""
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text, inspect

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')
MIG_NAME = TEST_DB.rsplit('/', 1)[-1].split('?', 1)[0] + '_migrations'     # per test DB, so parallel suites never share it
MIG_DB = TEST_DB.rsplit('/', 1)[0] + '/' + MIG_NAME


def _diff(engine):
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata
    from extensions import db
    import models  # noqa: F401
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={'compare_type': True}), db.metadata)


@pytest.fixture(scope='module')
def scratch():
    """A separate, empty database for the migration paths (the shared test DB is create_all'd by every other module)."""
    admin = create_engine(TEST_DB, isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS {MIG_NAME}'))
        conn.execute(text(f'CREATE DATABASE {MIG_NAME}'))
    eng = create_engine(MIG_DB)
    yield eng
    eng.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS {MIG_NAME} WITH (FORCE)'))


def _wipe(engine):
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA public CASCADE; CREATE SCHEMA public;'))


def test_single_head_and_baseline_named():
    from utils.schema import head_revision, BASELINE_REVISION
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    cfg = Config(str(BACKEND / 'alembic.ini')); cfg.set_main_option('script_location', str(BACKEND / 'migrations'))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1 and heads[0] == head_revision()
    assert any(r.revision == BASELINE_REVISION for r in ScriptDirectory.from_config(cfg).walk_revisions())


def test_empty_db_is_created_at_head_and_matches_models(scratch):
    from utils.schema import migrate, head_revision
    _wipe(scratch)
    res = migrate(scratch)
    assert res['action'] == 'created' and res['from'] is None and res['to'] == head_revision()
    tables = set(inspect(scratch).get_table_names())
    assert {'interventions', 'forecast_runs', 'account_forecasts', 'alembic_version'} <= tables
    assert _diff(scratch) == []
    assert migrate(scratch)['action'] == 'upgraded'      # at head: no-op


def test_pre_alembic_db_is_stamped_then_upgraded(scratch):
    """The EC2 box before 2026-09-05: every table from create_all, no alembic_version."""
    from flask import Flask
    from extensions import db
    from utils.schema import migrate, head_revision
    _wipe(scratch)
    app = Flask(__name__); app.config['SQLALCHEMY_DATABASE_URI'] = MIG_DB; db.init_app(app)
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
    assert 'alembic_version' not in inspect(scratch).get_table_names()
    res = migrate(scratch)
    assert res['action'] == 'stamped_then_upgraded' and res['to'] == head_revision()
    assert _diff(scratch) == []


def test_models_and_revisions_agree_on_the_shared_test_db():
    """The guard: any model change needs `alembic revision --autogenerate`."""
    from utils.schema import migrate
    eng = create_engine(TEST_DB)
    migrate(eng)
    assert _diff(eng) == []


def test_pre_alembic_drift_is_reconciled(scratch):
    """The exact drift scripts/schema_check.py found on the EC2 database before the first Alembic deploy:
    two JSONB columns where the model says JSON, two indexes the boot ALTER helper never created."""
    from flask import Flask
    from extensions import db
    from utils.schema import migrate, head_revision
    _wipe(scratch)
    app = Flask(__name__); app.config['SQLALCHEMY_DATABASE_URI'] = MIG_DB; db.init_app(app)
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
    with scratch.begin() as conn:
        conn.execute(text('ALTER TABLE qualitative_signals ALTER COLUMN extractions TYPE JSONB USING extractions::jsonb'))
        conn.execute(text('ALTER TABLE qualitative_signals ALTER COLUMN attributes TYPE JSONB USING attributes::jsonb'))
        conn.execute(text('DROP INDEX ix_qualitative_signals_content_hash'))
        conn.execute(text('DROP INDEX ix_kpi_measurements_upload_id'))
    assert len(_diff(scratch)) == 4
    res = migrate(scratch)
    assert res['action'] == 'stamped_then_upgraded' and res['to'] == head_revision()
    assert _diff(scratch) == []           # whatever head is: later revisions must be idempotent on a create_all DB


def test_0005_backfills_every_unscoped_user_to_their_own_tenant(scratch):
    """Revision 0005 is data, not DDL, so schema comparison cannot see whether it ran.
    The rows it fixes are on a running box: every User predating 2026-09-08 has
    allowed_customer_ids IS NULL, which app_api.auth now (correctly) reads as "reaches no
    tenant at all" — so without this back-fill the fix locks every existing user out of
    their own tenant instead of merely fencing them out of everyone else's."""
    from flask import Flask
    from extensions import db
    from utils.schema import migrate, head_revision
    _wipe(scratch)
    app = Flask(__name__); app.config['SQLALCHEMY_DATABASE_URI'] = MIG_DB; db.init_app(app)
    with app.app_context():
        import models  # noqa: F401
        from models import Customer, User
        db.create_all()                        # the pre-Alembic box, exactly
        c1 = Customer(customer_name='Tenant One', domain='one.test')
        c2 = Customer(customer_name='Tenant Two', domain='two.test')
        db.session.add_all([c1, c2]); db.session.commit()
        rows = [User(customer_id=c1.customer_id, user_name='one admin', email='a@t.test', role='admin'),
                User(customer_id=c1.customer_id, user_name='one csm', email='b@t.test', role='csm'),
                User(customer_id=c2.customer_id, user_name='two cfo', email='c@t.test', role='cfo'),
                User(customer_id=None, user_name='no tenant', email='d@t.test', role='csm'),
                User(customer_id=c1.customer_id, user_name='hand scoped', email='e@t.test', role='cro',
                     allowed_customer_ids=[c1.customer_id, c2.customer_id])]
        db.session.add_all(rows); db.session.commit()
        assert [u.allowed_customer_ids for u in User.query.order_by(User.user_id).all()][:4] == [None] * 4
        ids = (c1.customer_id, c2.customer_id)
        db.session.remove()

    assert migrate(scratch)['to'] == head_revision()

    with app.app_context():
        from models import User
        by_email = {u.email: u for u in User.query.all()}
        assert by_email['a@t.test'].allowed_customer_ids == [ids[0]]      # admin: its own tenant, not "all"
        assert by_email['b@t.test'].allowed_customer_ids == [ids[0]]
        assert by_email['c@t.test'].allowed_customer_ids == [ids[1]]
        assert by_email['d@t.test'].allowed_customer_ids is None          # no tenant to grant: stays fail-closed
        assert by_email['e@t.test'].allowed_customer_ids == list(ids)     # a hand-set scope is never narrowed
        from app_api import auth
        assert auth.allows_customer(by_email['a@t.test'], ids[0])
        assert not auth.allows_customer(by_email['a@t.test'], ids[1])
        assert not auth.allows_customer(by_email['d@t.test'], ids[0])
        db.session.remove()

    assert migrate(scratch)['to'] == head_revision()                      # re-runnable
    with app.app_context():
        from models import User
        assert {u.email: u.allowed_customer_ids for u in User.query.all()} == {
            'a@t.test': [ids[0]], 'b@t.test': [ids[0]], 'c@t.test': [ids[1]], 'd@t.test': None, 'e@t.test': list(ids)}
        db.session.remove()
