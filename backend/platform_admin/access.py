"""
Who may use the platform console.

    require_platform(request, write=False) -> SessionUser      raises PermissionError

One rule: the logged-in user's users.platform_role must be one of
config/app_api.json platform.roles — and, for anything that changes state, one
of platform.write_roles. The tenant `role` column is never consulted: a tenant's
own 'admin' is not an operator, and treating it as one is exactly the leak
app_api/auth.py documents. platform_role is written only by
scripts/manage_platform_operators.py, on the box.

PermissionError messages: 'not_authenticated' (401), 'not_operator' (403),
'read_only' (403) — platform_admin/http.py maps them.
"""
from __future__ import annotations

from app_api import settings
from app_api.auth import require_session


def roles() -> tuple:
    return tuple(settings.get('platform', 'roles'))


def write_roles() -> tuple:
    return tuple(settings.get('platform', 'write_roles'))


def require_platform(request, write: bool = False):
    user = require_session(request)             # 'not_authenticated' when there is no valid session
    if user.platform_role not in roles():
        raise PermissionError('not_operator')
    if write and user.platform_role not in write_roles():
        raise PermissionError('read_only')
    return user
