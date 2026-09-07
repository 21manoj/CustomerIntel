"""
CS Pulse MCP — Wizard B (Hindsight) read tool.

    get_hindsight(customer_id)

The run itself is `trigger_wizard(customer_id, 'b')` (cs_pulse_onboarding) and
the Wizard B step inside process_data (auto-run at >=5 journeys). Registers
on the shared `mcp` instance; keyed over HTTP (onboarding_tool_registry.KEYED_TOOLS).

Until 2026-09-07 Wizard B's only exposure was embedded inside get_roi's
'hindsight' block (roi/measured.py) — Wizard C and D each got a dedicated
read tool of their own (get_calibration, get_forecast); this one hadn't.
The lookup itself lives in wizards/wizard_b_hindsight.py's own get_hindsight()
so this tool and get_roi's block read the exact same function, never two
copies that can drift.
"""
from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


@mcp.tool
def get_hindsight(customer_id: int) -> dict:
    """Wizard B (Hindsight) — the latest backward-looking read for a tenant.

    Arc pattern profiles, the phase transition matrix with trigger-role
    histograms, realized NRR (lost/expansion buckets only, new_logo excluded
    from the denominator), intervention lift rows (before/after windows on
    the journeys' counterfactual hooks — a labelled comparison, not a causal
    estimate), and the lead-time backtest's evidence label. Needs >=5
    journeys; process_data auto-runs it, or force a fresh pass with
    trigger_wizard(customer_id, 'b'). Status is 'no_run' with a hint, never
    a guess, when nothing has completed yet.

    This is the same data get_roi's 'hindsight' block already shows —
    exposed here as its own read so you don't have to pull the whole ROI
    response just to see it.

    Args:
        customer_id: The customer ID
    """
    _require_auth_if_key_present('get_hindsight', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import Customer
        from extensions import db
        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f'Customer {customer_id} not found.')
        from wizards.wizard_b_hindsight import get_hindsight as _gh
        from journeys.read import origin_block
        res = _gh(customer_id)
        return {'customer_id': int(customer_id), **origin_block(customer_id), **res}
