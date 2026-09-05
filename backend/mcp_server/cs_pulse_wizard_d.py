"""
CS Pulse MCP — Wizard D (Foresight) read tool.

    get_forecast(customer_id, account_id=None)

The run itself is `trigger_wizard(customer_id, 'd')` (cs_pulse_onboarding) and
the Wizard D step inside process_data. Registers on the shared `mcp` instance;
keyed over HTTP (onboarding_tool_registry.KEYED_TOOLS).
"""
from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


@mcp.tool
def get_forecast(customer_id: int, account_id: int = None) -> dict:
    """Wizard D (Foresight) — the latest forward view for a tenant.

    Without account_id: the portfolio roll-up (revenue-weighted expected ARR
    at the horizon end with its propagated range under two correlation
    assumptions, portfolio NRR with bounds, basis counts, the label counts
    the calibration gate saw) plus one compact row per account.

    With account_id: that account's full block — basis ('prior' template
    until the tenant has enough labelled outcomes, 'calibrated' on them
    after), label counts, retention and expansion probabilities with their
    interval and its semantics, the decision point inside the horizon,
    expected ARR with bounds, the drivers (every factor applied), the
    story-arc template position, and the episode ids the forecast rests on.
    Every number here carries its basis and a range; quote both.

    Args:
        customer_id: The customer ID
        account_id: Optional account for the full per-account block
    """
    _require_auth_if_key_present('get_forecast', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from wizards.wizard_d_foresight import get_forecast as _gf
        from journeys.read import origin_block
        res = _gf(customer_id, account_id)
        if res is None:
            raise ToolError('no Foresight run for this ' + ('account' if account_id else 'customer')
                            + " — trigger_wizard(customer_id, 'd') or process_data")
        return {'customer_id': int(customer_id), **origin_block(customer_id), **res}
