"""
Alembic environment — the models' metadata is the source of truth; the DB
URL is DATABASE_URL (never alembic.ini). Offline mode (--sql) is supported.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _metadata():
    from extensions import db
    import models  # noqa: F401 — registers every table on db.metadata (the signal engine's tables live there too)
    return db.metadata


def _url() -> str:
    url = os.environ.get('DATABASE_URL') or config.get_main_option('sqlalchemy.url')
    if not url or url.endswith('unused'):
        raise SystemExit('DATABASE_URL is required to run migrations')
    return url


target_metadata = _metadata()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={'paramstyle': 'named'}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = context.config.attributes.get('connection_engine') or create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
