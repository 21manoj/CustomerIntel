"""
CS Pulse MCP — API key management.

    list_api_keys(customer_id, include_revoked=False)
    revoke_api_key(customer_id, key_id)
    rotate_api_key(customer_id, key_id)

For a leaked or retired tenant key. All three are admin tools (auth.ADMIN_TOOLS):
over HTTP they need the server key, or a customer key for that tenant with 'admin'
scope. The read+write key a tenant gets at onboarding cannot call them, so a leaked
tenant key cannot be used to mint its own replacement or lock the tenant out.

A revoke is effective on the next request (api_key_service.validate_api_key reads
the database every time). Every call, allowed or denied, is in the tool audit log;
the revoke/rotate rows carry the affected key id and prefix in `detail`.

The operator can do the same from the box without any key:
    python scripts/manage_api_keys.py --help
"""
from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


def _customer_or_error(customer_id: int):
    from models import Customer
    from extensions import db
    c = db.session.get(Customer, int(customer_id))
    if not c:
        raise ToolError(f'Customer {customer_id} not found.')
    return c


@mcp.tool
def list_api_keys(customer_id: int, include_revoked: bool = False) -> dict:
    """List a tenant's API keys — id, 12-character prefix, name, scopes, active flag,
    expiry, last use. The full key is never stored and cannot be shown again.

    Use this to find the id of a key to revoke or rotate. Requires the server key
    or a customer key with 'admin' scope.

    Args:
        customer_id: The customer ID
        include_revoked: Also list keys that were revoked (default False)
    """
    _require_auth_if_key_present('list_api_keys', customer_id)
    _check_mcp_enabled()
    with _get_flask_app().app_context():
        _customer_or_error(customer_id)
        from api_key_service import list_api_keys as _list
        keys = _list(int(customer_id), include_revoked=bool(include_revoked))
        return {'customer_id': int(customer_id), 'count': len(keys), 'keys': keys}


@mcp.tool
def revoke_api_key(customer_id: int, key_id: int) -> dict:
    """Revoke one of a tenant's API keys. The key is refused from the next request
    on; this cannot be undone (issue a new key with rotate_api_key before revoking
    if the tenant still needs access). Requires the server key or a customer key
    with 'admin' scope.

    Args:
        customer_id: The customer ID the key belongs to
        key_id: The key's id, from list_api_keys
    """
    _require_auth_if_key_present('revoke_api_key', customer_id)
    _check_mcp_enabled()
    with _get_flask_app().app_context():
        _customer_or_error(customer_id)
        from extensions import db
        from models import CustomerApiKey
        from api_key_service import revoke_api_key as _revoke
        from mcp_server import audit
        rec = db.session.get(CustomerApiKey, int(key_id))
        if not rec or rec.customer_id != int(customer_id):
            raise ToolError(f'API key {key_id} not found for customer {customer_id}.')
        was_active = bool(rec.is_active)
        prefix = rec.key_prefix
        _revoke(int(key_id), customer_id=int(customer_id))
        audit.record('mcp', 'revoke_api_key', customer_id, key_kind='system', outcome='allowed',
                     detail=f'revoked key id={key_id} prefix={prefix}' if was_active else f'key id={key_id} was already revoked')
        return {'customer_id': int(customer_id), 'key_id': int(key_id), 'key_prefix': prefix,
                'status': 'revoked' if was_active else 'already_revoked'}


@mcp.tool
def rotate_api_key(customer_id: int, key_id: int) -> dict:
    """Replace one of a tenant's API keys: a new key with the same name, scopes and
    restrictions is issued and the old key is revoked at the same moment. The new
    key is returned ONCE in this response — store it now; it cannot be read back.
    Requires the server key or a customer key with 'admin' scope.

    Args:
        customer_id: The customer ID the key belongs to
        key_id: The id of the key to replace, from list_api_keys
    """
    _require_auth_if_key_present('rotate_api_key', customer_id)
    _check_mcp_enabled()
    with _get_flask_app().app_context():
        _customer_or_error(customer_id)
        from api_key_service import rotate_api_key as _rotate
        from mcp_server import audit
        try:
            full_key, new = _rotate(int(key_id), customer_id=int(customer_id))
        except ValueError as e:
            raise ToolError(f'{e} (customer {customer_id}).')
        audit.record('mcp', 'rotate_api_key', customer_id, key_kind='system', outcome='allowed',
                     detail=f'rotated key id={key_id} -> id={new.id} prefix={new.key_prefix}')
        return {'customer_id': int(customer_id), 'revoked_key_id': int(key_id), 'api_key': full_key,
                'key': new.to_dict(),
                'note': 'Store this key now; it is shown once. The old key no longer works.'}
