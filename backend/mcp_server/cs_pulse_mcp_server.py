#!/usr/bin/env python3
"""
CS Pulse MCP Server — Expose platform as tool provider for external LLMs.

Supports two transport modes:
  - stdio:  Claude Desktop, Claude Code (local subprocess)
  - http:   Copilot Studio, ChatGPT, remote agents (Streamable HTTP)

Feature gated: Requires FeatureToggle.MCP_SERVER to be ON.

Tier 2A port (2026-09-01): this file carries only the `mcp` FastMCP
instance and the shared helper functions cs_pulse_onboarding.py needs —
not the old repo's ~69 @mcp.tool-decorated tools across 9 modules. Tools
get ported module-by-module as their own Tier 2 slices (onboarding first).

Three fixes made relative to the old repo's version, not cosmetic:

1. _get_dc2s_pillar_labels() dropped entirely. It was marked DEPRECATED in
   its own docstring and, confirmed by grep, never actually called anywhere
   in the old repo — only referenced in a comment. Genuinely dead code, not
   ported.

2. _get_flask_app(), _get_health_functions(), _get_trailing_kpi_values_generic(),
   and _get_precalculated_scores() now delegate to mcp_server/common.py
   instead of carrying their own independent copies. The old repo had two
   parallel implementations of each (one here, one in common.py) that could
   silently diverge — common.py's versions were already the correct ones
   (get_health_functions properly delegates to utils/vertical_health.py;
   this file's own copy used _make_generic_calculator directly plus a
   trailing-KPI reader pointed at the dead KPIScore table). Collapsing to
   one implementation fixes the silent-duplication pattern and the dead
   table read in the same move — this file's _get_health_functions used to
   always return {} for trailing KPI values in the old repo too, since
   KPIScore had zero live rows there.

3. _get_pillar_labels(vertical: str = 'dc2_s') and _get_health_functions had
   silent dc2_s-flavored defaults/behavior; removed — every real call site
   already passes an explicit value.
"""

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_mcp_dir = os.path.dirname(os.path.abspath(__file__))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)
if _mcp_dir not in sys.path:
    sys.path.insert(0, _mcp_dir)

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mcp_server.common import load_system_prompt as _load_system_prompt

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "CustomerIntelV1",
    instructions=_load_system_prompt(),
)


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------
def _check_mcp_enabled():
    """Raise ToolError if MCP_SERVER toggle is OFF."""
    from mcp_server.common import check_mcp_enabled
    check_mcp_enabled()


# ---------------------------------------------------------------------------
# Flask app singleton (lightweight DB context) — single implementation
# lives in mcp_server/common.py; this is a thin re-export so existing
# `from mcp_server.cs_pulse_mcp_server import _get_flask_app` call sites
# (e.g. mcp_server/auth.py) keep working.
# ---------------------------------------------------------------------------
def _get_flask_app():
    from mcp_server.common import get_flask_app
    return get_flask_app()


def _get_account_arr(account) -> float:
    """Extract ARR from account (profile_metadata or revenue column)."""
    from mcp_server.common import get_account_arr
    return get_account_arr(account)


def _get_pillar_labels(vertical: str) -> dict:
    """Return canonical pillar labels for a vertical."""
    from mcp_server.common import get_pillar_labels
    return get_pillar_labels(vertical)


def _validate_account_ownership(customer_id: int, account_id: int):
    """Tenant isolation: verify account belongs to customer, return Account or raise."""
    from mcp_server.common import validate_account_ownership
    return validate_account_ownership(customer_id, account_id)


def _resolve_customer_vertical(customer_id: int) -> str:
    """Look up the vertical for a customer.

    Resolution order (most authoritative first):
      1. CustomerConfig.vertical — canonical long form ('saas_premium', 'dc2_s', ...)
      2. Customer.vertical        — short code ('saas', 'dc', 'msp') — normalized via
                                    vertical_registry.VERTICAL_ALIASES
      3. Fails closed — raises ToolError. No dc2_s fallback.

    Why two tables: Customer.vertical predates CustomerConfig.vertical and uses short
    codes ('saas', 'dc'). When a downstream caller checks `vertical == 'saas_premium'`
    the short form silently misses, defaulting the tenant to dc2_s. Normalizing both
    sources here is the single fix point.

    Deliberately NOT delegated to utils.vertical_registry.get_vertical_for_customer:
    that resolver only checks CustomerConfig.vertical and doesn't know about the
    legacy Customer.vertical short-code tier — collapsing the two would lose that
    second tier. Carried forward unchanged from the old repo, including this
    distinction — see the old repo's own warning against merging them.

    No fallback to dc2_s: a silent default here previously served the wrong
    vertical's catalog/playbooks under the customer's own name whenever both
    tiers were unset.
    """
    from models import Customer, CustomerConfig
    from utils.vertical_registry import normalize_vertical

    customer = Customer.query.get(int(customer_id))
    if not customer:
        raise ToolError(f"Customer {customer_id} not found")

    # 1. CustomerConfig.vertical — canonical long form
    cfg = CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()
    if cfg and cfg.vertical:
        return normalize_vertical(cfg.vertical)

    # 2. Customer.vertical — short code, normalize to long form
    short = getattr(customer, 'vertical', None)
    if short:
        return normalize_vertical(short)

    # 3. Fail closed — no legacy dc2_s fallback
    raise ToolError(
        f"Cannot resolve vertical for customer {customer_id}: no "
        f"CustomerConfig.vertical and no Customer.vertical set. No fallback to dc2_s."
    )


def _get_precalculated_scores(account_id: int):
    """Vertical-agnostic: read pre-calculated scores via the canonical service.

    Returns (health_score, health_status, pillar_dict) or (None, None, None).
    """
    from mcp_server.common import get_precalculated_scores
    return get_precalculated_scores(account_id)


def _get_trailing_kpi_values_generic(account_id: int, days: int = 30) -> dict:
    """Vertical-agnostic: read latest KPI values (fallback path).

    Returns dict of {kpi_code: value}.
    """
    from mcp_server.common import get_trailing_kpi_values_generic
    return get_trailing_kpi_values_generic(account_id)


def _get_health_functions(customer_id: int):
    """Return (calculate_kpi_health, get_trailing_kpi_values, get_precalculated_scores)
    for the given customer.
    """
    from mcp_server.common import get_health_functions
    return get_health_functions(customer_id)


def _get_kpi_definitions(vertical: str) -> dict:
    """Return the KPI definitions dict for a vertical."""
    from mcp_server.common import get_kpi_definitions
    return get_kpi_definitions(vertical)


def _get_playbook_config(vertical: str):
    """Return (PLAYBOOK_CONFIG, should_trigger_playbook) for a vertical.

    See mcp_server/common.py::get_playbook_config for why this is
    currently a safe no-op stub, not the old repo's per-vertical logic.
    """
    from mcp_server.common import get_playbook_config
    return get_playbook_config(vertical)


def _require_auth(customer_id: int, required_scope: str = 'read',
                  _api_key: str = None):
    """Enforce API key auth for portfolio/customer-level intelligence tools."""
    from mcp_server.auth import require_auth
    require_auth(customer_id, required_scope, _api_key)


def _require_account_auth(customer_id: int, account_id: int,
                          required_scope: str = 'read',
                          _api_key: str = None):
    """Enforce API key auth for account-level intelligence tools."""
    from mcp_server.auth import require_account_auth
    require_account_auth(customer_id, account_id, required_scope, _api_key)


def get_system_prompt() -> str:
    """Returns the full system prompt text for CS Pulse MCP. Use as Claude project/custom instructions.

    Old repo also exposed this as an @mcp.resource("cspulse://system-prompt")
    — not re-registered here yet since no other Tier 2A tool needs it as a
    resource; the function itself is enough for now.
    """
    from mcp_server.common import load_system_prompt_content
    return load_system_prompt_content()
