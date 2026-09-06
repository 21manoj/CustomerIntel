"""
Session auth + RBAC for the human UI (docs/design/ui-rbac.md §2-3).

Two auth systems, kept separate: CustomerApiKey Bearer tokens are for MCP
and integrations (mcp_server/auth.py, api_key_service.py); this module is
for a person logged into the browser app. Never mix the two — a route here
never accepts a Bearer key, and mcp_server/auth.py never reads this cookie.

    login(email, password, ip) -> (token, user)         raises ValueError
    verify_session(token) -> User | None                 fresh DB read every call
    require_session(request, role=None) -> User          raises PermissionError (401/403 at the route)
    issue_setup_token(user) -> raw_token                  ADMIN-ONLY caller relays it; never emailed by this module
    consume_setup_token(raw_token, new_password) -> User  the only unauthenticated write in this package
    user_scope(user) -> (customer_ids | None, account_ids | None)   None = unrestricted (admin, or NULL column)

No self-service "forgot password by email": see the design doc §2 for why
that is an account-takeover hole without a mail sender. Only an
authenticated admin can issue or reissue a setup token.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from app_api import settings


@dataclass(frozen=True)
class SessionUser:
    """An immutable snapshot of a User row — never a live ORM object. Every
    field the HTTP layer needs is copied out while the row is still attached
    to a session; nothing downstream can hit the DetachedInstanceError class
    of bug (an ORM object read outside the app-context / session scope it
    was loaded in — found in this build's own first draft: a route handler
    read `user.role` after the app context that loaded it had already
    closed, which worked by luck until something committed first and
    expired the object's attributes)."""
    user_id: int
    customer_id: int
    email: str
    name: str
    role: str
    allowed_customer_ids: Optional[list]
    allowed_account_ids: Optional[list]


def _snapshot(user) -> SessionUser:
    return SessionUser(user_id=user.user_id, customer_id=user.customer_id, email=user.email, name=user.user_name,
                       role=user.role, allowed_customer_ids=user.allowed_customer_ids, allowed_account_ids=user.allowed_account_ids)

_serializer_cache = None
_fail_tracker: dict = defaultdict(list)
_last_cleanup: float = 0.0


class AuthError(ValueError):
    """Bad credentials, inactive user, rate-limited, or an invalid/expired token. Message is safe to show."""


def _secret() -> str:
    import os
    s = os.environ.get(settings.get('session', 'secret_env'))
    if not s:
        raise RuntimeError(f"{settings.get('session', 'secret_env')} is not set — required to sign session cookies")
    return s


def _serializer():
    from itsdangerous import URLSafeTimedSerializer
    global _serializer_cache
    if _serializer_cache is None or _serializer_cache[0] != _secret():
        secret = _secret()
        _serializer_cache = (secret, URLSafeTimedSerializer(secret, salt='ci-session'))
    return _serializer_cache[1]


def sign_session(user_id: int) -> str:
    return _serializer().dumps({'user_id': int(user_id)})


def _unsign_session(token: str) -> Optional[int]:
    from itsdangerous import BadSignature, SignatureExpired
    try:
        data = _serializer().loads(token, max_age=int(settings.get('session', 'max_age_seconds')))
    except (BadSignature, SignatureExpired):
        return None
    return data.get('user_id')


# ── login rate limiting (process-local; same shape as api_key_service.py) ──

def _rl_key(email: str, ip: str) -> str:
    return f'{email.lower().strip()}|{ip}'


def _cleanup_stale(cfg: dict):
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < cfg['cleanup_interval_seconds']:
        return
    _last_cleanup = now
    cutoff = now - cfg['window_seconds']
    for k in [k for k, ts in _fail_tracker.items() if not ts or ts[-1] < cutoff]:
        del _fail_tracker[k]


def _is_rate_limited(email: str, ip: str) -> bool:
    cfg = settings.get('login_rate_limit')
    _cleanup_stale(cfg)
    now = time.time()
    key = _rl_key(email, ip)
    cutoff = now - cfg['window_seconds']
    _fail_tracker[key] = [t for t in _fail_tracker[key] if t > cutoff]
    if len(_fail_tracker[key]) >= cfg['max_failures']:
        return now - _fail_tracker[key][-1] < cfg['block_seconds']
    return False


def _record_failure(email: str, ip: str):
    _fail_tracker[_rl_key(email, ip)].append(time.time())


# ── login / logout ──

def login(email: str, password: str, ip: str = 'unknown') -> tuple:
    """(token, User) — the ORM row, valid only inside the caller's own app context. HTTP
    handlers should call login_session instead. Raises AuthError with a message safe to show."""
    from werkzeug.security import check_password_hash
    from models import User
    if _is_rate_limited(email or '', ip):
        raise AuthError('Too many failed attempts. Try again in a minute.')
    user = User.query.filter_by(email=(email or '').strip().lower()).first() if email else None
    ok = bool(user and user.active and user.password_hash and check_password_hash(user.password_hash, password or ''))
    if not ok:
        _record_failure(email or '', ip)
        raise AuthError('Incorrect email or password.')
    user.last_login = datetime.utcnow()
    from extensions import db
    db.session.commit()
    return sign_session(user.user_id), user


def login_session(email: str, password: str, ip: str = 'unknown') -> tuple:
    """(token, SessionUser) — what the HTTP layer calls; the ORM row never leaves this function."""
    token, user = login(email, password, ip)
    return token, _snapshot(user)


def verify_session(token: Optional[str]):
    """The User for a session cookie, or None. Reads the row fresh every call — a
    deactivated user is locked out immediately, not just when the cookie expires."""
    if not token:
        return None
    uid = _unsign_session(token)
    if uid is None:
        return None
    from models import User
    user = User.query.get(uid)
    return user if (user and user.active) else None


# ── one-time setup / reset tokens (admin-issued only — see module docstring) ──

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def issue_setup_token(user) -> str:
    """A fresh one-time token for `user`, returned to the ADMIN caller only. Invalidates any prior
    token. Flushes, does not commit — callers with their own transaction in progress (create_customer)
    must not have it committed out from under them; callers that are standalone (app_api/users.py)
    already commit right after via mcp_server.audit.record."""
    cfg = settings.get('password_setup')
    raw = secrets.token_urlsafe(cfg['token_bytes'])
    user.magic_link_token = _hash_token(raw)
    user.magic_link_expires_at = datetime.utcnow() + timedelta(minutes=cfg['expiry_minutes'])
    from extensions import db
    db.session.flush()
    return raw


def consume_setup_token(raw_token: str, new_password: str):
    """Sets the password and burns the token. The only route in this package a
    logged-out caller can reach — it can only ever act on a token an admin already issued."""
    from werkzeug.security import generate_password_hash
    from models import User
    from extensions import db
    min_len = settings.get('password_min_length')
    if not new_password or len(new_password) < min_len:
        raise AuthError(f'Password must be at least {min_len} characters.')
    if not raw_token:
        raise AuthError('Invalid or expired link.')
    hashed = _hash_token(raw_token)
    user = User.query.filter_by(magic_link_token=hashed).first()
    if not user or not user.magic_link_expires_at or user.magic_link_expires_at < datetime.utcnow():
        raise AuthError('Invalid or expired link.')
    user.password_hash = generate_password_hash(new_password)
    user.magic_link_token = None
    user.magic_link_expires_at = None
    user.active = True
    db.session.commit()
    return user


# ── RBAC ──

def require_session(request, role: Optional[str] = None) -> SessionUser:
    """The logged-in user for this request, as an immutable snapshot — never the ORM row (see
    SessionUser). Raises PermissionError('not_authenticated' | 'wrong_role') — http.py's _guard
    translates those to 401 / 403."""
    token = request.cookies.get(settings.get('session', 'cookie_name'))
    user = verify_session(token)
    if not user:
        raise PermissionError('not_authenticated')
    if role and user.role != 'admin' and user.role != role:
        raise PermissionError('wrong_role')
    return _snapshot(user)


def user_scope(user) -> tuple:
    """(customer_ids, account_ids) — None means unrestricted. admin is always unrestricted."""
    if user.role == 'admin':
        return None, None
    return user.allowed_customer_ids, user.allowed_account_ids


def allows_customer(user, customer_id: int) -> bool:
    cids, _ = user_scope(user)
    return cids is None or int(customer_id) in cids


def allows_account(user, account_id: int) -> bool:
    _, aids = user_scope(user)
    return aids is None or int(account_id) in aids
