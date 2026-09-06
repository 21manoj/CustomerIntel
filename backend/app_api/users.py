"""
User management — admin only (docs/design/ui-rbac.md §4).

    invite(admin, customer_id, email, name, role, allowed_account_ids=None) -> (User, raw_setup_token)
    list_users(customer_id=None) -> [dict]
    patch_user(admin, user_id, role=None, active=None, allowed_customer_ids=None, allowed_account_ids=None) -> User
    reset_password(admin, user_id) -> raw_setup_token
"""
from __future__ import annotations

from typing import Optional

from app_api import settings
from app_api.auth import issue_setup_token


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
    customer = db.session.get(Customer, int(customer_id))
    if not customer:
        raise ValueError(f'customer {customer_id} not found')
    user = User(customer_id=int(customer_id), user_name=name.strip(), email=email, role=role,
               allowed_account_ids=allowed_account_ids, active=True)
    db.session.add(user)
    db.session.flush()
    raw = issue_setup_token(user)
    view = _view(user)
    from mcp_server import audit
    audit.record('ui', 'users.invite', customer_id, key_kind='user', key_record=None, outcome='allowed',
                 detail=f'user {user.user_id} ({email}) role={role} invited by user:{admin.user_id}')
    return view, raw


def list_users(customer_id: Optional[int] = None) -> list:
    from models import User
    q = User.query
    if customer_id is not None:
        q = q.filter_by(customer_id=int(customer_id))
    return [_view(u) for u in q.order_by(User.user_id).all()]


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
    if user.user_id == admin.user_id and active is False:
        raise ValueError('cannot deactivate your own account')
    changed = []
    if role is not None:
        user.role = _valid_role(role); changed.append(f'role={user.role}')
    if active is not None:
        user.active = bool(active); changed.append(f'active={user.active}')
    if allowed_customer_ids is not None:
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
    raw = issue_setup_token(user)
    from mcp_server import audit
    audit.record('ui', 'users.reset_password', user.customer_id, key_kind='user', key_record=None, outcome='allowed',
                 detail=f'user {user.user_id} reset by user:{admin.user_id}')
    return raw
