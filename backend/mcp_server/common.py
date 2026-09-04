#!/usr/bin/env python3
"""
CS Pulse MCP — Shared helpers used across all tiered MCP servers.

Ported from the old repo's mcp_server/common.py (2026-09-01), Tier 2A.
Two fixes made during the port, not present in the old repo:

1. get_trailing_kpi_values_generic() queried the old repo's KPIScore table,
   which had zero live rows there (confirmed during the Tier 1 port) — the
   real per-account KPI values always lived in DC2SKPI (renamed
   KPIMeasurement here). So in the old repo this function was already
   dead-in-practice: it always returned {}, even though the table existed
   and the query "worked". Rewritten to query KPIMeasurement, the single
   real source, instead of carrying the dead path forward under a new name.

2. get_pillar_labels(vertical: str = 'dc2_s') had a silent dc2_s default
   that no real caller ever relied on (every live call site already passes
   an explicit vertical) — same pattern as the dc2_s defaults removed
   elsewhere in Tier 1 (vertical_health.py). Made
   required, no default.

get_playbook_config() is NOT ported yet: PLAYBOOK_CONFIG data lives only in
per-vertical Python modules (verticals/{vertical}/vertical_config.py) in
the old repo, none of which exist in this build (Tier 1 deleted the
Python-module vertical-loading path entirely — every vertical here is a
JSON catalog, and no playbook-config JSON equivalent has been created yet).
Porting the old function verbatim would silently work for saas_premium
(it has a try/except) but hard-crash for dc2_s (its import has no
try/except in the old repo) the first time any playbook-close path ran
for a dc2_s customer. Stubbed here to fail safely for every vertical
until playbook-config data itself is ported — that's real Tier 2/3 work,
not a foundation-layer concern.
"""

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from fastmcp.exceptions import ToolError

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
_PROMPT_FILE = os.path.join(_backend_dir, 'config', 'mcp_system_prompt.md')


def load_system_prompt() -> str:
    """Load MCP system prompt from config/mcp_system_prompt.md."""
    try:
        with open(_PROMPT_FILE, 'r') as f:
            return f.read()
    except FileNotFoundError:
        return (
            "AI-native Customer Success platform — health scoring, "
            "signal detection, context graph intelligence, revenue analytics."
        )


def load_system_prompt_content() -> str:
    """Load CS Pulse MCP system prompt for the resource endpoint.

    Search order:
      1) CSPULSE_MCP_SYSTEM_PROMPT_PATH env var
      2) backend/config/mcp_system_prompt.md
      3) Repo root CS_PULSE_MCP_SYSTEM_PROMPT.md
      4) mcp_server/cs_pulse_mcp_system_prompt.md
    """
    env_path = os.environ.get("CSPULSE_MCP_SYSTEM_PROMPT_PATH")
    if env_path and os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            return f.read()
    _dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        os.path.join(_dir, "..", "config", "mcp_system_prompt.md"),
        os.path.join(_dir, "..", "..", "..", "CS_PULSE_MCP_SYSTEM_PROMPT.md"),
        os.path.join(_dir, "cs_pulse_mcp_system_prompt.md"),
    ]:
        abs_path = os.path.abspath(candidate)
        if os.path.isfile(abs_path):
            with open(abs_path, "r", encoding="utf-8") as f:
                return f.read()
    return "# CS Pulse MCP — System prompt file not found."


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------
def check_mcp_enabled():
    """Raise ToolError if MCP_SERVER toggle is OFF."""
    from feature_toggles import feature_toggles, FeatureToggle
    if not feature_toggles.is_enabled(FeatureToggle.MCP_SERVER):
        raise ToolError("MCP Server is disabled. Enable via FEATURE_MCP_SERVER=true")


# ---------------------------------------------------------------------------
# Flask app singleton (lightweight DB context)
# ---------------------------------------------------------------------------
_flask_app = None


def get_flask_app():
    """Return a minimal Flask app for DB context."""
    global _flask_app
    if _flask_app is not None:
        return _flask_app

    from flask import Flask
    from extensions import db
    from dotenv import load_dotenv

    load_dotenv()

    app = Flask(__name__)
    database_url = os.environ.get('SQLALCHEMY_DATABASE_URI') or os.environ.get('DATABASE_URL')
    if not database_url:
        raise ToolError("DATABASE_URL environment variable is required")

    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    _flask_app = app
    return app


# ---------------------------------------------------------------------------
# Account / customer helpers
# ---------------------------------------------------------------------------
def get_account_arr(account) -> float:
    """Extract ARR from account (profile_metadata or revenue column)."""
    arr = 0.0
    if account.profile_metadata and isinstance(account.profile_metadata, dict):
        arr = float(account.profile_metadata.get('arr', 0) or 0)
    if not arr and account.revenue:
        arr = float(account.revenue)
    return arr


def validate_account_ownership(customer_id: int, account_id: int):
    """Tenant isolation: verify account belongs to customer, return Account or raise."""
    from models import Account
    account = Account.query.filter_by(
        account_id=account_id,
        customer_id=int(customer_id),
    ).first()
    if not account:
        raise ToolError(f"Account {account_id} not found for customer {customer_id}")
    return account


# ---------------------------------------------------------------------------
# Pillar labels (vertical-aware)
# ---------------------------------------------------------------------------
def get_pillar_labels(vertical: str) -> dict:
    """Return canonical pillar labels for a vertical.

    Resolves through the vertical registry so any vertical's JSON catalog
    gets its own pillar display names. The literal dict below is only a
    last resort for the case where the registry itself can't resolve the
    vertical at all (e.g. import failure) — not vertical-specific data.
    """
    try:
        from utils.vertical_registry import get_pillars
        pillars = get_pillars(vertical)
        if pillars:
            return {code: (p.get('name') or code) for code, p in pillars.items()}
    except Exception:
        pass
    return {
        'P1': 'Deployment Velocity', 'P2': 'Operational Stability',
        'P3': 'AI Workload Performance', 'P4': 'Channel & Partner Health',
        'P5': 'Expansion Readiness',
    }


# ---------------------------------------------------------------------------
# Health / score helpers (vertical-agnostic)
# ---------------------------------------------------------------------------
def get_precalculated_scores(account_id: int):
    """Read pre-calculated scores from the canonical account-health service.

    Returns (health_score, health_status, pillar_dict) or (None, None, None).
    """
    try:
        from utils.account_health import get_precalculated_scores_tuple
        return get_precalculated_scores_tuple(account_id)
    except Exception:
        return None, None, None


def get_trailing_kpi_values_generic(account_id: int) -> dict:
    """Read latest KPI values from KPIMeasurement. Returns {kpi_code: value}."""
    try:
        from models import KPIMeasurement
        rows = KPIMeasurement.query.filter_by(account_id=account_id) \
            .order_by(KPIMeasurement.measured_at.desc()).all()
        seen = {}
        for r in rows:
            if r.kpi_code not in seen and r.value is not None:
                seen[r.kpi_code] = float(r.value)
        return seen
    except Exception:
        return {}


def get_health_functions(customer_id: int):
    """Return (calculate_kpi_health, get_trailing_kpi_values, get_precalculated_scores)
    for the given customer. All verticals use the generic JSON-catalog scorer.

    Takes customer_id, not vertical: utils.vertical_health.get_health_calculator
    resolves the vertical internally via the single fail-closed lookup
    (utils.vertical_registry.get_vertical_for_customer) rather than trusting
    a vertical string the caller resolved earlier and might be passing
    stale. The old repo's equivalent took a vertical string directly —
    changed here to match the Tier 1 vertical_health.py contract instead of
    carrying the older, less safe calling convention forward.
    """
    from utils.vertical_health import get_health_calculator, get_trailing_kpi_values_func
    from utils.vertical_health import get_precalculated_scores as vpc

    try:
        calc = get_health_calculator(customer_id)
        trailing = get_trailing_kpi_values_func(customer_id)
        return calc, trailing, vpc
    except Exception:
        def _noop_calculate(kpi_values, customer_id=None):
            return 0.0, {}
        return _noop_calculate, get_trailing_kpi_values_generic, get_precalculated_scores


def get_kpi_definitions(vertical: str) -> dict:
    """Return the KPI definitions dict for a vertical, via the vertical registry."""
    from utils.vertical_registry import get_kpis
    return get_kpis(vertical)


def get_playbook_config(vertical: str):
    """Return (PLAYBOOK_CONFIG, should_trigger_playbook) for a vertical.

    Stub: playbook-config data hasn't been ported into this build yet (see
    module docstring). Always returns a safe no-op rather than crashing —
    replace once playbook definitions are ported as their own catalog.
    """
    return {}, lambda *a, **kw: False


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def require_auth(customer_id: int, required_scope: str = 'read', _api_key: str = None):
    """Enforce API key auth for customer-level tools."""
    from mcp_server.auth import require_auth as _auth
    _auth(customer_id, required_scope, _api_key)


def require_account_auth(customer_id: int, account_id: int,
                         required_scope: str = 'read', _api_key: str = None):
    """Enforce API key auth + account-level restriction."""
    from mcp_server.auth import require_account_auth as _auth
    _auth(customer_id, account_id, required_scope, _api_key)


# ---------------------------------------------------------------------------
# Context graph gate
# ---------------------------------------------------------------------------
def check_context_graph(customer_id: int):
    """Raise ToolError if context graph is not enabled for this customer.

    Checks:
      1. Global platform toggle (FeatureToggleManager)
      2. Per-customer DB toggle (models.FeatureToggle) — if the row exists
         and is explicitly disabled, blocks access.
    """
    from feature_toggles import feature_toggles, FeatureToggle as FTEnum

    if not feature_toggles.is_enabled(FTEnum.CONTEXT_GRAPH):
        raise ToolError(
            f"Context graph is not enabled for customer {customer_id}. "
            "Enable via feature toggles or onboarding."
        )

    try:
        from models import FeatureToggle as FTModel
        toggle = FTModel.query.filter_by(
            customer_id=int(customer_id),
            feature_name='context_graph',
        ).first()
        if toggle is not None and not toggle.enabled:
            raise ToolError(
                f"Context graph is not enabled for customer {customer_id}. "
                "Enable via enable_features() or onboarding."
            )
    except ToolError:
        raise
    except Exception:
        pass  # DB toggle check unavailable — rely on global toggle


# ---------------------------------------------------------------------------
# Visualization helpers
# ---------------------------------------------------------------------------
def mermaid_safe(text: str) -> str:
    """Escape text for safe use inside Mermaid node labels."""
    if not text:
        return ''
    return (text.replace('"', "'")
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('&', '&amp;')
                .replace('\n', ' ')
                .replace('[', '(')
                .replace(']', ')'))


def format_revenue_short(value: float) -> str:
    """Format a revenue value as a short string like $1.2M or $450K."""
    if not value:
        return '$0'
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    elif abs(value) >= 1_000:
        return f"${value / 1_000:.0f}K"
    else:
        return f"${value:.0f}"


# ---------------------------------------------------------------------------
# CSV helper
# ---------------------------------------------------------------------------
def csv_string(headers: list, rows: list) -> str:
    """Build a CSV string from headers and list-of-dict rows."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# MCP server runner helper
# ---------------------------------------------------------------------------
def run_server(mcp_instance, default_port: int = 8001):
    """Run an MCP server in stdio or HTTP mode based on sys.argv."""
    transport = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_TRANSPORT", "stdio")

    if transport == "http":
        os.environ["MCP_TRANSPORT"] = "http"
        mcp_instance.run(transport="streamable-http", host="0.0.0.0", port=default_port)
    else:
        mcp_instance.run(transport="stdio")
