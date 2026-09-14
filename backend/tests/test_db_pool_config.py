"""
backend scaling plan step 1: get_flask_app() used pure SQLAlchemy/Flask-SQLAlchemy
defaults (pool_size=5, max_overflow=10, no pre_ping, no recycle) with no way to
tune per deployment. Now configurable via DB_POOL_SIZE / DB_MAX_OVERFLOW /
DB_POOL_PRE_PING / DB_POOL_RECYCLE, defaulting to the plan's own values so an
unset .env reproduces today's intended behavior (not the old SQLAlchemy default
of max_overflow=10 -- that default silently changed to 5, documented here).

get_flask_app() is a process-wide singleton (mcp_server.common._flask_app), so
this must run the assertions in a SEPARATE subprocess per case -- importing the
module a second time in-process would just return the already-built app engine.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

_PROBE = """
import sys
sys.path.insert(0, {backend!r})
import mcp_server.common as common
app = common.get_flask_app()
from extensions import db
with app.app_context():
    pool = db.engine.pool
    print('|'.join(str(x) for x in (pool.size(), pool._pre_ping, pool._recycle, pool._max_overflow)))
"""


def _probe(env_overrides):
    env = dict(os.environ)
    env['DATABASE_URL'] = TEST_DB
    for k in ('DB_POOL_SIZE', 'DB_MAX_OVERFLOW', 'DB_POOL_PRE_PING', 'DB_POOL_RECYCLE'):
        env.pop(k, None)
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, '-c', _PROBE.format(backend=str(BACKEND))],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, f"probe failed:\nSTDOUT: {out.stdout}\nSTDERR: {out.stderr}"
    size, pre_ping, recycle, max_overflow = out.stdout.strip().split('|')
    return {
        'pool_size': int(size), 'pre_ping': pre_ping == 'True',
        'recycle': int(recycle), 'max_overflow': int(max_overflow),
    }


def test_defaults_match_the_scaling_plan():
    cfg = _probe({})
    assert cfg == {'pool_size': 5, 'pre_ping': True, 'recycle': 1800, 'max_overflow': 5}


def test_every_knob_is_env_overridable():
    cfg = _probe({
        'DB_POOL_SIZE': '11', 'DB_MAX_OVERFLOW': '3',
        'DB_POOL_PRE_PING': 'false', 'DB_POOL_RECYCLE': '900',
    })
    assert cfg == {'pool_size': 11, 'pre_ping': False, 'recycle': 900, 'max_overflow': 3}


@pytest.mark.parametrize('falsy', ['false', 'False', '0', 'no', 'NO'])
def test_pre_ping_recognizes_common_falsy_spellings(falsy):
    cfg = _probe({'DB_POOL_PRE_PING': falsy})
    assert cfg['pre_ping'] is False
