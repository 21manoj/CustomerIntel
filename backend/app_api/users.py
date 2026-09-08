"""
User management — admin only (docs/design/ui-rbac.md §4).

    invite(admin, customer_id, email, name, role, allowed_account_ids=None) -> (User, raw_setup_token)
    list_users(customer_ids) -> [dict]
    patch_user(admin, user_id, role=None, active=None, allowed_customer_ids=None, allowed_account_ids=None) -> User
    reset_password(admin, user_id) -> raw_setup_token

Every write here is tenant-scoped to the ACTING admin, in this module rather
than only at the route, because these are the paths that can re-open the
cross-tenant hole app_api/auth.py describes: reset_password hands back a
working password-setup token for the target user (an account takeover if the
target is another tenant's admin — reproduced live before the fix), and
patch_user writes allowed_customer_ids itself, so an unconstrained one lets an
admin grant themselves any tenant. PermissionError here → 403 at the route.
"""
from __future__ import annotations

from typing import Optional

from app_api import settings
from app_api.auth import allows_customer, issue_setup_token


def _require_tenant(admin, customer_id, what: str) -> int:
    """The acting admin must be scoped to the tenant they are acting on."""
    if customer_id is None:
        raise ValueError('customer_id is required')
    cid = int(customer_id)
    if not allows_customer(admin, cid):
        raise PermissionError(f'{what}: customer {cid} is outside your tenant scope')
    return cid


def _valid_role(role: str) -> str:
    roles = settings.get('roles')
    role = (role or '').strip().lower()
    if role not in roles:
        raise ValueError(f'role must be one of {roles}')
    return role


def invite(admin, customer_id: int, email: str, name: str, role: str, allowed_account_ids: Optional[list] = None) -> tuple:
    """(user_view_dict, raw_setup_token). Never returns the ORM row — see app_api.auth.SessionUser
    for why nothing in this package hands a live row across a function boundary that might cross
    an app-context/session boundary too."""
    from models import Customer, User
    from extensions import db
    role = _valid_role(role)
    email = (email or '').strip().lower()
    if not email or '@' not in email:
        raise ValueError('a valid email is required')
    if not name or not name.strip():
        raise ValueError('name is required')
    if User.query.filter_by(email=email).first():
        raise ValueError(f'{email!r} is already registered')
    # Scope before existence: "customer 500 not found" vs "not permitted" would
    # otherwise tell an admin which other tenants exist.
    _require_tenant(admin, customer_id, 'invite')
    customer = db.session.get(Customer, int(customer_id))
    if not customer:
        raise ValueError(f'customer {customer_id} not found')
    # Scoped to their own tenant at creation, every role, admin included — a user
    # invited without allowed_customer_ids reaches nothing (auth.allows_customer is
    # fail-closed), which is how this path leaked before 2026-09-08: it never set it.
    user = User(customer_id=int(customer_id), user_name=name.strip(), email=email, role=role,
               allowed_customer_ids=[int(customer_id)], allowed_account_ids=allowed_account_ids, active=True)
    db.session.add(user)
    db.session.flush()
    raw = issue_setup_token(user)
    view = _view(user)
    from mcp_server import audit
    audit.record('ui', 'users.invite', customer_id, key_kind='user', key_record=None, outcome='allowed',
                 detail=f'user {user.user_id} ({email}) role={role} invited by user:{admin.user_id}')
    return view, raw


def list_users(customer_ids) -> list:
    """Users of the given tenants. The tenant list is REQUIRED and the caller
    (app_api/http.py) resolves it from the acting admin's own scope — the old
    signature defaulted to None = every user of every tenant, which returned the
    whole platform's user directory to any admin who simply omitted customer_id."""
    from models import User
    ids = [int(c) for c in (customer_ids if isinstance(customer_ids, (list, tuple, set)) else [customer_ids])]
    if not ids:
        return []
    return [_view(u) for u in User.query.filter(User.customer_id.in_(ids)).order_by(User.user_id).all()]


def _view(u) -> dict:
    return {'user_id': u.user_id, 'customer_id': u.customer_id, 'name': u.user_name, 'email': u.email,
            'role': u.role, 'active': u.active, 'allowed_customer_ids': u.allowed_customer_ids,
            'allowed_account_ids': u.allowed_account_ids, 'last_login': u.last_login.isoformat() if u.last_login else None,
            'has_password': bool(u.password_hash)}


def patch_user(admin, user_id: int, role: Optional[str] = None, active: Optional[bool] = None,
               allowed_customer_ids: Optional[list] = None, allowed_account_ids: Optional[list] = None) -> dict:
    from models import User
    from extensions import db
    user = db.session.get(User, int(user_id))
    if not user:
        raise ValueError(f'user {user_id} not found')
    _require_tenant(admin, user.customer_id, 'patch_user')
    if user.user_id == admin.user_id and active is False:
        raise ValueError('cannot deactivate your own account')
    changed = []
    if role is not None:
        user.role = _valid_role(role); changed.append(f'role={user.role}')
    if active is not None:
        user.active = bool(active); changed.append(f'active={user.active}')
    if allowed_customer_ids is not None:
        # An admin may only grant tenants they themselves hold: without this, the one
        # write path that sets the scope column is also the way to escape it.
        for cid in allowed_customer_ids:
            _require_tenant(admin, cid, 'patch_user allowed_customer_ids')
        user.allowed_customer_ids = allowed_customer_ids or None; changed.append('allowed_customer_ids')
    if allowed_account_ids is not None:
        user.allowed_account_ids = allowed_account_ids or None; changed.append('allowed_account_ids')
    db.session.commit()
    from mcp_server import audit
    audit.record('ui', 'users.patch', user.customer_id, key_kind='user', key_record=None, outcome='allowed',
                 detail=f'user {user.user_id} by user:{admin.user_id}: {", ".join(changed) or "no-op"}')
    return _view(user)


def reset_password(admin, user_id: int) -> str:
    from models import User
    from extensions import db
    user = db.session.get(User, int(user_id))
    if not user:
        raise ValueError(f'user {user_id} not found')
    _require_tenant(admin, user.customer_id, 'reset_password')
    raw = issue_setup_token(user)
    from mcp_server import audit
    audit.record('ui', 'users.reset_password', user.customer_id, key_kind='user', key_record=None, outcome='allowed',
                 detail=f'user {user.user_id} reset by user:{admin.user_id}')
    return raw
