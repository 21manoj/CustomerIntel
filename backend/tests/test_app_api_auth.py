"""
app_api/auth.py + app_api/users.py — direct calls, real Postgres, no HTTP layer
(see tests/test_app_api_http.py for the route/RBAC/cookie layer):

  * login: success, wrong password, unknown email, inactive user, rate limit
  * sessions: sign/verify round-trip, tampered token, expired token, a
    deactivated user is locked out immediately even with a still-valid token
  * setup tokens: issue → consume sets the password and burns the token;
    expired, wrong, and reused tokens are all refused
  * create_customer issues a real usable setup token (the bug this closes:
    the old code generated a password and never returned or stored it)
  * users.invite / list_users / patch_user / reset_password, role validation,
    an admin cannot deactivate themself
"""
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault('SESSION_SECRET', 'test-secret-' + uuid.uuid4().hex)

from flask import Flask                                   # noqa: E402
from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common                        # noqa: E402
_common._flask_app = app
from models import User                                    # noqa: E402
from app_api import auth, users as user_admin              # noqa: E402


def _mk_user(role='csm', active=True, customer_id=1, password=None, **kw):
    from werkzeug.security import generate_password_hash
    tag = uuid.uuid4().hex[:10]
    u = User(customer_id=customer_id, user_name=kw.pop('name', f'Test User {tag}'), email=f'{tag}@t.test',
             role=role, active=active, password_hash=generate_password_hash(password) if password else None, **kw)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture(scope='module')
def db_ctx():
    with app.app_context():
        db.create_all()
        from models import Customer
        c = Customer(customer_name='Auth Test', domain=f'auth-{uuid.uuid4().hex[:8]}.test')
        db.session.add(c); db.session.commit()
        yield c.customer_id
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    auth._fail_tracker.clear()
    yield
    auth._fail_tracker.clear()


# ── login ────────────────────────────────────────────────────────────

def test_login_succeeds_and_records_last_login(db_ctx):
    with app.app_context():
        u = _mk_user(password='correct horse battery', customer_id=db_ctx)
        token, logged_in = auth.login(u.email, 'correct horse battery', ip='1.2.3.4')
        assert token and logged_in.user_id == u.user_id
        assert db.session.get(User, u.user_id).last_login is not None


def test_login_rejects_wrong_password_unknown_email_and_inactive_user(db_ctx):
    with app.app_context():
        u = _mk_user(password='right-password', customer_id=db_ctx)
        with pytest.raises(auth.AuthError, match='Incorrect'):
            auth.login(u.email, 'wrong-password', ip='9.9.9.9')
        with pytest.raises(auth.AuthError, match='Incorrect'):
            auth.login('nobody@nowhere.test', 'anything', ip='9.9.9.9')
        inactive = _mk_user(password='pw', active=False, customer_id=db_ctx)
        with pytest.raises(auth.AuthError, match='Incorrect'):
            auth.login(inactive.email, 'pw', ip='9.9.9.9')
        no_pw = _mk_user(password=None, customer_id=db_ctx)
        with pytest.raises(auth.AuthError, match='Incorrect'):
            auth.login(no_pw.email, '', ip='9.9.9.9')


def test_login_rate_limits_after_repeated_failures(db_ctx):
    with app.app_context():
        u = _mk_user(password='the-real-one', customer_id=db_ctx)
        ip = '5.5.5.5'
        for _ in range(5):
            with pytest.raises(auth.AuthError, match='Incorrect'):
                auth.login(u.email, 'nope', ip=ip)
        with pytest.raises(auth.AuthError, match='Too many failed attempts'):
            auth.login(u.email, 'the-real-one', ip=ip)          # even the RIGHT password is blocked once rate-limited
        with pytest.raises(auth.AuthError, match='Incorrect'):
            auth.login(u.email, 'nope', ip='6.6.6.6')            # a different IP is not blocked


# ── sessions ─────────────────────────────────────────────────────────

def test_session_round_trip_tamper_and_expiry(db_ctx, monkeypatch):
    with app.app_context():
        u = _mk_user(password='pw', customer_id=db_ctx)
        token = auth.sign_session(u.user_id)
        assert auth.verify_session(token).user_id == u.user_id
        assert auth.verify_session(token[:-1] + ('a' if token[-1] != 'a' else 'b')) is None
        assert auth.verify_session(None) is None
        assert auth.verify_session('garbage') is None

        from app_api import settings
        monkeypatch.setitem(settings.load(), 'session', {**settings.get('session'), 'max_age_seconds': -1})
        assert auth.verify_session(token) is None    # already "expired" the instant it was minted


def test_deactivated_user_is_locked_out_immediately_even_with_a_valid_token(db_ctx):
    with app.app_context():
        u = _mk_user(password='pw', customer_id=db_ctx)
        token = auth.sign_session(u.user_id)
        assert auth.verify_session(token) is not None
        u.active = False
        db.session.commit()
        assert auth.verify_session(token) is None     # the signature is still valid; the row says no


# ── setup tokens ─────────────────────────────────────────────────────

def test_setup_token_sets_password_and_is_single_use(db_ctx):
    with app.app_context():
        u = _mk_user(password=None, customer_id=db_ctx)
        raw = auth.issue_setup_token(u)
        db.session.commit()
        assert u.magic_link_token and u.magic_link_token != raw    # stored hashed, not raw
        with pytest.raises(auth.AuthError, match='at least'):
            auth.consume_setup_token(raw, 'short')
        auth.consume_setup_token(raw, 'a-brand-new-password')
        refreshed = db.session.get(User, u.user_id)
        assert refreshed.active and refreshed.magic_link_token is None and refreshed.password_hash
        with pytest.raises(auth.AuthError, match='Invalid or expired'):
            auth.consume_setup_token(raw, 'another-new-password')     # reused
        with pytest.raises(auth.AuthError, match='Invalid or expired'):
            auth.consume_setup_token('not-a-real-token', 'another-new-password')
        token, logged_in = auth.login(refreshed.email, 'a-brand-new-password')
        assert logged_in.user_id == u.user_id


def test_setup_token_expiry(db_ctx):
    with app.app_context():
        u = _mk_user(password=None, customer_id=db_ctx)
        raw = auth.issue_setup_token(u)
        u.magic_link_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        with pytest.raises(auth.AuthError, match='Invalid or expired'):
            auth.consume_setup_token(raw, 'a-new-password-here')


# ── create_customer wires the real fix ──────────────────────────────

def test_create_customer_issues_a_usable_setup_token_not_a_discarded_password():
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        res = create_customer(name=f'Setup {tag}', domain=f'setup-{tag}.test', vertical='saas_premium',
                              admin_email=f'admin_{tag}@t.test', admin_name='Admin', data_origin='synthetic_test')
        assert res.get('admin_setup_token') and 'once' in res['admin_setup_token_note'].lower()
        auth.consume_setup_token(res['admin_setup_token'], 'a-real-password-now')
        token, logged_in = auth.login(res['admin_email'], 'a-real-password-now')
        assert logged_in.user_id == res['admin_user_id'] and logged_in.role == 'admin'


# ── RBAC ─────────────────────────────────────────────────────────────

def test_user_scope_and_allows_helpers(db_ctx):
    with app.app_context():
        admin = _mk_user(role='admin', customer_id=db_ctx)
        scoped = _mk_user(role='csm', customer_id=db_ctx, allowed_customer_ids=[db_ctx], allowed_account_ids=[1, 2])
        unrestricted = _mk_user(role='cfo', customer_id=db_ctx)
        assert auth.user_scope(admin) == (None, None)
        assert auth.allows_customer(admin, 999) and auth.allows_account(admin, 999)
        assert auth.user_scope(scoped) == ([db_ctx], [1, 2])
        assert auth.allows_account(scoped, 1) and not auth.allows_account(scoped, 3)
        assert not auth.allows_customer(scoped, db_ctx + 100)
        assert auth.user_scope(unrestricted) == (None, None)


class _FakeRequest:
    def __init__(self, cookie):
        self.cookies = {'ci_session': cookie} if cookie else {}


def test_require_session_401_vs_403(db_ctx):
    with app.app_context():
        csm = _mk_user(role='csm', customer_id=db_ctx)
        token = auth.sign_session(csm.user_id)
        assert auth.require_session(_FakeRequest(token)).user_id == csm.user_id
        assert auth.require_session(_FakeRequest(token), role='csm').user_id == csm.user_id
        with pytest.raises(PermissionError, match='not_authenticated'):
            auth.require_session(_FakeRequest(None))
        with pytest.raises(PermissionError, match='not_authenticated'):
            auth.require_session(_FakeRequest('garbage'))
        with pytest.raises(PermissionError, match='wrong_role'):
            auth.require_session(_FakeRequest(token), role='admin')
        admin = _mk_user(role='admin', customer_id=db_ctx)
        admin_token = auth.sign_session(admin.user_id)
        assert auth.require_session(_FakeRequest(admin_token), role='csm').user_id == admin.user_id  # admin passes every role gate


# ── user management ──────────────────────────────────────────────────

def test_invite_validates_and_issues_a_setup_token(db_ctx):
    with app.app_context():
        admin = _mk_user(role='admin', customer_id=db_ctx)
        with pytest.raises(ValueError, match='role must be one of'):
            user_admin.invite(admin, db_ctx, 'x@t.test', 'X', 'superuser')
        with pytest.raises(ValueError, match='valid email'):
            user_admin.invite(admin, db_ctx, 'not-an-email', 'X', 'csm')
        with pytest.raises(ValueError, match='customer .* not found'):
            user_admin.invite(admin, db_ctx + 100000, 'x2@t.test', 'X', 'csm')
        u, raw = user_admin.invite(admin, db_ctx, 'newcsm@t.test', 'New CSM', 'csm', allowed_account_ids=[7])
        assert isinstance(u, dict) and u['role'] == 'csm' and u['allowed_account_ids'] == [7] and raw
        with pytest.raises(ValueError, match='already registered'):
            user_admin.invite(admin, db_ctx, 'newcsm@t.test', 'Dup', 'csm')
        rows = user_admin.list_users(db_ctx)
        assert any(r['email'] == 'newcsm@t.test' and r['has_password'] is False for r in rows)


def test_patch_user_updates_and_refuses_self_deactivation(db_ctx):
    with app.app_context():
        admin = _mk_user(role='admin', customer_id=db_ctx)
        target = _mk_user(role='csm', customer_id=db_ctx)
        out = user_admin.patch_user(admin, target.user_id, role='cro', allowed_customer_ids=[db_ctx])
        assert out['role'] == 'cro' and out['allowed_customer_ids'] == [db_ctx]
        with pytest.raises(ValueError, match='cannot deactivate your own account'):
            user_admin.patch_user(admin, admin.user_id, active=False)
        out2 = user_admin.patch_user(admin, target.user_id, active=False)
        assert out2['active'] is False
        with pytest.raises(ValueError, match='not found'):
            user_admin.patch_user(admin, 99999999, role='csm')


def test_reset_password_issues_a_fresh_token_and_invalidates_the_old_one(db_ctx):
    with app.app_context():
        admin = _mk_user(role='admin', customer_id=db_ctx)
        u = _mk_user(role='csm', password='old-password-here', customer_id=db_ctx)
        raw1 = user_admin.reset_password(admin, u.user_id)
        raw2 = user_admin.reset_password(admin, u.user_id)
        with pytest.raises(auth.AuthError, match='Invalid or expired'):
            auth.consume_setup_token(raw1, 'whatever-new-password')     # superseded by raw2
        auth.consume_setup_token(raw2, 'brand-new-password-2')
        auth.login(u.email, 'brand-new-password-2')
