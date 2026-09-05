"""
MCP Server Authentication — API key validation for HTTP transport.

For stdio transport (Claude Desktop, Claude Code), auth is implicit (local process).
For Streamable HTTP transport (Copilot Studio, ChatGPT), Bearer token auth is required.

TWO-TIER AUTH MODEL:
  1. Server-level key (MCP_SERVER_API_KEY env var) — super-admin access to all customers
  2. Customer-scoped keys (DB-backed, csp_* format) — per-customer access with scopes

FRICTIONLESS AUTH MODEL:
  Onboarding tools (ONBOARDING_TOOLS) require NO API key. They are open for
  prospects evaluating the platform via an AI assistant. All other intelligence
  tools require a valid API key over HTTP.

SCOPE HIERARCHY:
  'read'  — intelligence/read tools (list_accounts, get_account_health, etc.)
  'write' — read + data ingestion (upload_csv, process_data, configure_customer_kpis)
  'admin' — write + customer management (all tools)

Ported 2026-09-01 (Tier 2A). Known gap, not yet ported: validate_customer_key()
below calls api_key_service.validate_api_key(), which needs both
api_key_service.py and the CustomerApiKey model — neither exists in this
build yet. This only matters for HTTP transport; every _resolve_key() path
below returns early (trusted, no-op) when MCP_TRANSPORT != "http", which is
the only mode Tier 2A needs. Port api_key_service.py + CustomerApiKey before
standing up HTTP transport, not before.
"""

import os
import logging
import contextvars
from functools import wraps
from typing import Optional

logger = logging.getLogger(__name__)

# Request-scoped API key storage (set by ASGI middleware, read by tool functions)
_current_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar('_current_api_key', default='')

# Session-scoped API key cache — survives across async tasks within the same MCP session.
# FastMCP may spawn tool execution in a new asyncio.Task where contextvars don't propagate.
# Key: mcp-session-id (str), Value: Bearer token (str)
_session_api_keys: dict = {}
_current_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar('_current_session_id', default='')


# ---------------------------------------------------------------------------
# Server-level API key (env var — super-admin / backward-compat)
# ---------------------------------------------------------------------------
MCP_SERVER_API_KEY = os.environ.get("MCP_SERVER_API_KEY", "")

# ---------------------------------------------------------------------------
# Auth toggle — set MCP_AUTH_REQUIRED=false to disable API key enforcement.
# Default: true (production-safe). Onboarding tools are exempt (ONBOARDING_TOOLS set).
# ---------------------------------------------------------------------------
MCP_AUTH_REQUIRED = os.environ.get("MCP_AUTH_REQUIRED", "true").lower() in ("true", "1", "yes")

if not MCP_AUTH_REQUIRED:
    import logging as _auth_log
    _auth_log.getLogger(__name__).critical(
        "⚠️  MCP_AUTH_REQUIRED=false — API key enforcement DISABLED. "
        "All tools accessible without authentication. "
        "Set MCP_AUTH_REQUIRED=true for production."
    )


# ---------------------------------------------------------------------------
# Onboarding tools — frictionless auth (no API key required)
# ---------------------------------------------------------------------------
# Canonical set: mcp_server/onboarding_tool_registry.py (frictionless tools).
try:
    from mcp_server.onboarding_tool_registry import ONBOARDING_TOOLS
except ImportError:  # module run with mcp_server/ itself on sys.path
    from onboarding_tool_registry import ONBOARDING_TOOLS

# Write-scope tools — require 'write' scope on the API key
WRITE_TOOLS = {
    'upload_csv',
    'process_data',
    'configure_customer_kpis',
    'enable_features',
    'trigger_wizard',
    'complete_onboarding',
    'validate_csv',
}


def is_onboarding_tool(name: str) -> bool:
    """Return True if the tool name is in the frictionless onboarding set."""
    return name in ONBOARDING_TOOLS


def is_write_tool(name: str) -> bool:
    """Return True if the tool requires write scope."""
    return name in WRITE_TOOLS


def require_auth_if_key_present(tool_name: str, customer_id: int = None):
    """Enforce scope validation on onboarding tools IF an API key is present.

    Frictionless model: onboarding tools require NO key for prospects.
    BUT if a key IS present (e.g. a partner calling upload_csv), we still
    validate it — ensuring scope and customer isolation.

    Args:
        tool_name: The MCP tool being called.
        customer_id: The customer_id in the request (may be None for
                     discovery tools like list_verticals).

    Returns:
        The key_record if a key was present and validated, None otherwise.

    Raises:
        ToolError if a key IS present but fails validation.
    """
    from fastmcp.exceptions import ToolError
    from mcp_server import audit

    # Non-HTTP transports (stdio, SSE, etc.) are trusted — local process.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport != "http":
        audit.record('mcp', tool_name, customer_id, key_kind='local', outcome='allowed')
        return None

    raw_key = extract_api_key()
    if not raw_key:
        audit.record('mcp', tool_name, customer_id, key_kind='none', outcome='allowed', detail='frictionless onboarding')
        return None  # No key → prospect flow, allow (frictionless behavior)

    key_record = validate_customer_key(raw_key)

    if not key_record and validate_server_key(raw_key):
        audit.record('mcp', tool_name, customer_id, key_kind='server', outcome='allowed')
        return None  # Server key = super-admin, allow everything

    if not key_record:
        audit.record('mcp', tool_name, customer_id, key_kind='none', outcome='denied', detail='invalid or expired key')
        raise ToolError(
            "Invalid or expired API key provided. "
            "Onboarding tools don't require a key — remove the Authorization "
            "header to use the frictionless flow, or provide a valid key."
        )

    if customer_id is not None and key_record.customer_id != int(customer_id):
        audit.record('mcp', tool_name, customer_id, key_kind='customer', key_record=key_record, outcome='denied',
                     detail=f'key scoped to customer {key_record.customer_id}')
        raise ToolError(
            f"API key does not have access to customer {customer_id}. "
            f"This key is scoped to a different customer."
        )

    if is_write_tool(tool_name):
        if not check_scope(key_record, 'write'):
            audit.record('mcp', tool_name, customer_id, key_kind='customer', key_record=key_record, outcome='denied', detail='lacks write scope')
            raise ToolError(
                f"API key lacks required 'write' scope for tool '{tool_name}'. "
                f"Current scopes: {key_record.scopes}."
            )

    audit.record('mcp', tool_name, customer_id, key_kind='customer', key_record=key_record, outcome='allowed')
    return key_record


# ---------------------------------------------------------------------------
# Key extraction
# ---------------------------------------------------------------------------
def extract_api_key() -> Optional[str]:
    """Extract API key from the current transport context.

    Priority:
      1. contextvars _current_api_key_var (set by ASGI middleware per-request)
      2. session-scoped cache, keyed by this request's session id
      3. _MCP_CURRENT_API_KEY / CS_PULSE_API_KEY env vars (legacy/testing fallback)
    """
    key = _current_api_key_var.get('')
    if key:
        return key
    # Session-scoped cache lookup must be identity-blind-safe: never fall
    # back to an arbitrary session's key for a session-less caller — that
    # would hand a session-less HTTP caller some other tenant's key. Fail
    # closed (return None -> auth error) instead.
    session_id = _current_session_id_var.get('')
    if session_id and session_id in _session_api_keys:
        logger.debug("extract_api_key: found key via session cache (session_id=%s)", session_id[:8])
        return _session_api_keys[session_id]
    logger.debug("extract_api_key: no key found (contextvar=%r, sessions=%d)", key, len(_session_api_keys))
    key = os.environ.get("_MCP_CURRENT_API_KEY", "")
    if not key:
        key = os.environ.get("CS_PULSE_API_KEY", "")
    return key or None


# ---------------------------------------------------------------------------
# Scoped customer_id extraction (for tenant isolation on unscoped tools)
# ---------------------------------------------------------------------------
def get_scoped_customer_id() -> Optional[int]:
    """Return the customer_id this key is scoped to, or None for server/stdio keys.

    If no key or server key → returns None (show all customers).
    If customer-scoped key → returns that customer_id.
    """
    raw_key = extract_api_key()
    if not raw_key:
        return None  # stdio or no key

    if MCP_SERVER_API_KEY and raw_key == MCP_SERVER_API_KEY:
        return None

    key_record = validate_customer_key(raw_key)
    if key_record and key_record.customer_id:
        return int(key_record.customer_id)

    # Key was present but invalid (revoked, expired, or not found) — raise
    # rather than returning None (which would mean "show all").
    logger.warning("API key present but invalid/revoked — blocking request")
    raise PermissionError("Invalid or revoked API key")


# ---------------------------------------------------------------------------
# Server-level key validation (backward-compat, super-admin)
# ---------------------------------------------------------------------------
def validate_server_key(api_key: str) -> bool:
    """Validate an API key against the configured server-level key."""
    if not MCP_SERVER_API_KEY:
        return False
    return api_key == MCP_SERVER_API_KEY


# Legacy alias
validate_api_key = validate_server_key


# ---------------------------------------------------------------------------
# Customer-scoped key validation (DB-backed)
# ---------------------------------------------------------------------------
def validate_customer_key(raw_key: str):
    """Validate a customer API key against the customer_api_keys DB table.

    Returns the CustomerApiKey record if valid, None otherwise. Requires
    api_key_service.py — not yet ported (see module docstring); only
    reached over HTTP transport, so this stays a documented gap rather
    than a blocker for stdio.
    """
    if not raw_key:
        return None
    try:
        from api_key_service import validate_api_key as db_validate

        try:
            return db_validate(raw_key)
        except RuntimeError:
            pass  # "Working outside application context" — create one

        try:
            from mcp_server.common import get_flask_app
            app = get_flask_app()
            with app.app_context():
                return db_validate(raw_key)
        except Exception as ctx_err:
            logger.warning("Customer key validation (app context fallback): %s", ctx_err)
            return None

    except Exception as e:
        logger.warning("Customer key validation failed: %s", e)
        return None


def check_scope(key_record, required_scope: str) -> bool:
    """Check if a key record has the required scope.

    Scope hierarchy: admin ⊃ write ⊃ read
    """
    scopes = key_record.scopes if key_record.scopes else ['read']

    if 'admin' in scopes:
        return True
    if required_scope == 'read' and 'write' in scopes:
        return True
    return required_scope in scopes


# ---------------------------------------------------------------------------
# Unified auth enforcement (called by MCP tools)
# ---------------------------------------------------------------------------
def _resolve_key(customer_id: int, required_scope: str, _api_key=None):
    """Internal: validate key + customer + scope. Returns key_record or None (server key).

    Raises ToolError on any auth failure.
    """
    from fastmcp.exceptions import ToolError

    if not MCP_AUTH_REQUIRED:
        return None

    if _api_key is not None:
        raw_key = _api_key
    else:
        # Non-HTTP transports (stdio, SSE, etc.) are trusted — local process.
        transport = os.environ.get("MCP_TRANSPORT", "stdio")
        if transport != "http":
            return None  # Trusted — no key_record to return

        raw_key = extract_api_key()

    if not raw_key:
        raise ToolError(
            "API key required. Pass via Authorization: Bearer <key> header. "
            "You received a key when your customer was created via create_customer(). "
            "If you lost it, generate a new one from the Admin UI."
        )

    key_record = validate_customer_key(raw_key)
    if key_record:
        if key_record.customer_id != int(customer_id):
            raise ToolError(
                f"API key does not have access to customer {customer_id}. "
                f"This key is scoped to a different customer."
            )

        if not check_scope(key_record, required_scope):
            raise ToolError(
                f"API key lacks required '{required_scope}' scope. "
                f"Current scopes: {key_record.scopes}. "
                f"Contact your admin to get a key with '{required_scope}' access."
            )

        logger.debug(
            "Auth OK: key_id=%s customer=%s scope=%s",
            key_record.id, key_record.customer_id, required_scope,
        )
        return key_record

    if validate_server_key(raw_key):
        logger.debug("Auth OK: server key (super-admin) for customer=%s", customer_id)
        return None  # Server key — no account restrictions

    raise ToolError(
        "Invalid or expired API key. Check your key and try again. "
        "If you lost your key, generate a new one from the Admin UI."
    )


def require_auth(customer_id: int, required_scope: str = 'read',
                 _api_key: str = None):
    """Enforce customer API key auth for a tool call (customer-level).

    Checks: valid key → customer_id match → scope.
    For stdio transport: no-op (local process trusted).
    """
    _resolve_key(customer_id, required_scope, _api_key)


def require_read_key(tool_name: str, _api_key: str = None):
    """Enforce that *some* valid API key (customer-scoped or server-level) is
    present for a non-onboarding tool that has no customer_id parameter
    (discovery/list tools). stdio transport: no-op.
    """
    from fastmcp.exceptions import ToolError

    if not MCP_AUTH_REQUIRED:
        return
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport != "http":
        return

    raw_key = _api_key if _api_key is not None else extract_api_key()
    if not raw_key:
        raise ToolError(
            f"API key required for '{tool_name}'. Pass via "
            "Authorization: Bearer <key> header."
        )
    if validate_customer_key(raw_key) or validate_server_key(raw_key):
        return
    raise ToolError(
        "Invalid or expired API key. Check your key and try again."
    )


def require_scoped_read(tool_name: str, customer_id: int, _api_key: str = None):
    """Tenant-isolation gate for a read tool that DOES take a customer_id.

    Delegates to _resolve_key (the same path customer/account tools use),
    which rejects a key scoped to a different customer and lets the
    server-level key through. Distinct from require_read_key, which only
    checks that *some* valid key exists, for parameterless discovery tools.
    """
    return _resolve_key(customer_id, 'read', _api_key=_api_key)


def require_cross_customer_auth(tool_name: str, _api_key: str = None):
    """Enforce SERVER-LEVEL key auth for tools that read across customers.

    Customer-scoped keys are explicitly rejected — a tenant key must never
    enumerate other tenants, regardless of scope. stdio transport: no-op.
    """
    from fastmcp.exceptions import ToolError

    if not MCP_AUTH_REQUIRED:
        return
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport != "http":
        return

    raw_key = _api_key if _api_key is not None else extract_api_key()
    if not raw_key:
        raise ToolError(
            f"'{tool_name}' is a cross-customer tool and requires a "
            "server-level API key over HTTP."
        )
    if validate_server_key(raw_key):
        return
    if validate_customer_key(raw_key):
        raise ToolError(
            f"'{tool_name}' reads across customers and cannot be called "
            "with a customer-scoped key. A server-level key is required."
        )
    raise ToolError(
        "Invalid or expired API key. Check your key and try again."
    )


def require_account_auth(customer_id: int, account_id: int,
                         required_scope: str = 'read',
                         _api_key: str = None):
    """Enforce customer + account-level API key auth for a tool call.

    Same as require_auth, plus checks allowed_account_ids restriction.
    If a key has allowed_account_ids set (e.g. [354001, 354003]), the tool
    is ONLY allowed to access those accounts. NULL = all accounts. This is
    the partner isolation layer: a partner managing one pillar for 3
    accounts can only read/write those 3 accounts, not the full portfolio.
    """
    from fastmcp.exceptions import ToolError

    key_record = _resolve_key(customer_id, required_scope, _api_key)

    if key_record is None:
        return  # stdio or server key — no account restrictions

    if not key_record.has_account_access(int(account_id)):
        raise ToolError(
            f"API key does not have access to account {account_id}. "
            f"This key is restricted to accounts: {key_record.allowed_account_ids}."
        )


# ---------------------------------------------------------------------------
# Legacy decorator (kept for backward-compat, but prefer require_auth())
# ---------------------------------------------------------------------------
def require_api_key(func):
    """Decorator to require API key for HTTP-transported tool calls.

    Only enforced when MCP_SERVER_API_KEY is set. If not set, all HTTP
    requests are rejected (stdio is always allowed).
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not MCP_AUTH_REQUIRED:
            return func(*args, **kwargs)

        transport = os.environ.get("MCP_TRANSPORT", "stdio")
        if transport != "http":
            return func(*args, **kwargs)

        if is_onboarding_tool(func.__name__):
            cid = kwargs.get('customer_id')
            require_auth_if_key_present(func.__name__, cid)
            return func(*args, **kwargs)

        api_key = os.environ.get("_MCP_CURRENT_API_KEY", "")
        if not validate_api_key(api_key):
            raise PermissionError("Invalid or missing MCP API key")
        return func(*args, **kwargs)

    return wrapper
