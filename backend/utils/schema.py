"""
Schema management at boot — Alembic, not create_all.

    migrate(engine)   → {'action': 'upgraded' | 'stamped_then_upgraded' | 'created' | 'recreated', 'from': rev, 'to': head}

Three starting states:
  * empty DB                       → upgrade head (the baseline revision creates everything)
  * pre-Alembic DB (tables exist,
    no alembic_version)            → stamp the baseline once, then upgrade head. This is every DB that
                                     was built by db.create_all() before 2026-09-05 (the EC2 box).
  * DB with alembic_version        → upgrade head

Nothing here ALTERs on its own any more: the old boot-time ALTER helpers
(utils/schema_additive.py, signal_engine/models.py) are folded into the baseline.
A model change without a revision is caught by tests/test_migrations.py
(compare_metadata against a DB at head must be empty).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_REVISION = '0001_baseline'
PRE_ALEMBIC_SENTINEL_TABLE = 'customers'     # present on every DB create_all() ever built


def _config(engine):
    from alembic.config import Config
    cfg = Config(os.path.join(BACKEND, 'alembic.ini'))
    cfg.set_main_option('script_location', os.path.join(BACKEND, 'migrations'))
    cfg.attributes['connection_engine'] = engine
    return cfg


def current_revision(engine):
    from sqlalchemy import inspect, text
    if 'alembic_version' not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        row = conn.execute(text('select version_num from alembic_version')).first()
    return row[0] if row else None


def head_revision() -> str:
    from alembic.script import ScriptDirectory
    from alembic.config import Config
    cfg = Config(os.path.join(BACKEND, 'alembic.ini'))
    cfg.set_main_option('script_location', os.path.join(BACKEND, 'migrations'))
    return ScriptDirectory.from_config(cfg).get_current_head()


def migrate(engine) -> dict:
    from alembic import command
    from sqlalchemy import inspect
    cfg = _config(engine)
    before = current_revision(engine)
    tables = inspect(engine).get_table_names()
    if before is not None and PRE_ALEMBIC_SENTINEL_TABLE not in tables:
        # a version row with no tables behind it (a test DB after drop_all): the version is a lie — start over
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE alembic_version'))
        before = None
        command.upgrade(cfg, 'head')
        action = 'recreated'
    elif before is None and PRE_ALEMBIC_SENTINEL_TABLE in tables:
        # a DB that create_all() built before migrations existed: baseline it, then apply what came after
        command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, 'head')
        action = 'stamped_then_upgraded'
    elif before is None:
        command.upgrade(cfg, 'head')
        action = 'created'
    else:
        command.upgrade(cfg, 'head')
        action = 'upgraded'
    after = current_revision(engine)
    logger.info('schema: %s (%s → %s)', action, before, after)
    return {'action': action, 'from': before, 'to': after}
