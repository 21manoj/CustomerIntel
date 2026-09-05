"""
Schema is managed by Alembic (utils/schema.migrate at boot), not create_all:
  * an empty DB is created at head by the revisions alone and matches the models exactly
  * a pre-Alembic DB (built by create_all, no alembic_version) is stamped at the baseline then upgraded
  * a DB at head is a no-op; there is exactly one head
  * the models and the revisions agree — a model change without a revision fails here
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
MIG_DB = TEST_DB.rsplit('/', 1)[0] + '/customerintel_migrations_test'


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
        conn.execute(text('DROP DATABASE IF EXISTS customerintel_migrations_test'))
        conn.execute(text('CREATE DATABASE customerintel_migrations_test'))
    eng = create_engine(MIG_DB)
    yield eng
    eng.dispose()
    with admin.connect() as conn:
        conn.execute(text('DROP DATABASE IF EXISTS customerintel_migrations_test WITH (FORCE)'))


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
    assert 'interventions' in inspect(scratch).get_table_names() and 'alembic_version' in inspect(scratch).get_table_names()
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
