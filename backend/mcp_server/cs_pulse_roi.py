"""
CS Pulse MCP — Power-of-1 / ROI read tools (docs/design/power-of-1-roi.md).

    get_investment_priorities(customer_id, account_id=None)
    get_power_of_1(customer_id, account_id=None)
    get_roi(customer_id)

Reads only. Keyed over HTTP (onboarding_tool_registry.KEYED_TOOLS); the
computation lives in roi/ and is shared with the /api/roi/* routes and the
portfolio row in list_journeys.
"""
from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


def _read(customer_id, fn):
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import Customer
        from extensions import db
        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f'Customer {customer_id} not found.')
        try:
            return fn()
        except ValueError as e:                     # unknown vertical / missing economics: fail closed, say why
            raise ToolError(str(e))


@mcp.tool
def get_investment_priorities(customer_id: int, account_id: int = None) -> dict:
    """Where the next CS hour or dollar goes, now: accounts ranked by
    exposure-weighted revenue (revenue × the larger of a journey-derived
    risk factor — phase, leading layer, cited urgency, renewal proximity —
    and an opportunity factor from positive roles), each row with its lens
    (protect | grow — protect first whenever the risk factor clears the
    configured override, the other lens kept as secondary_lens), the
    factors, the open interventions (a proposed one is
    a decision waiting) and the episode / node ids it rests on. Portfolio
    totals for the tenant's vertical. Every $ is labelled derived; nothing
    here is a forecast.

    Args:
        customer_id: The customer ID
        account_id: One account only (optional)
    """
    _require_auth_if_key_present('get_investment_priorities', customer_id)
    from roi.priorities import investment_priorities
    return _read(customer_id, lambda: investment_priorities(int(customer_id), int(account_id) if account_id is not None else None))


@mcp.tool
def get_power_of_1(customer_id: int, account_id: int = None) -> dict:
    """What a 1-point (and a 1 %) move in each pillar and KPI is worth on
    THIS tenant's revenue base: revenue × the weights actually applied
    (latest health row, else CustomerConfig, else the catalog) × the
    vertical's assumed $ per health point (config/economics/<vertical>.json,
    with its basis sentence). A 1 % move in a KPI's value is scored through
    the catalog curve at the account's latest measurement. Band view
    (revenue at risk by band, points to the next band) and investment
    scenarios (share of revenue → break-even health lift). Every $ carries
    basis + basis_chain; derived × assumed = assumed.

    Args:
        customer_id: The customer ID
        account_id: One account only (optional)
    """
    _require_auth_if_key_present('get_power_of_1', customer_id)
    from roi.power_of_1 import power_of_1
    return _read(customer_id, lambda: power_of_1(int(customer_id), int(account_id) if account_id is not None else None))


@mcp.tool
def get_roi(customer_id: int) -> dict:
    """What the last interventions returned: realized $ (linked outcomes,
    measured, cited by node id) vs exposure $ (account revenue on the rows,
    derived) per playbook and per pillar — two numbers, never summed; the
    outcome ledger by revenue bucket with the subset linked to
    interventions; Wizard B's latest hindsight run (lift rows, realized NRR,
    evidence label); and a measured $ per health point only when enough
    closed interventions carry both a health lift and a revenue outcome —
    otherwise insufficient_data with the count.

    Args:
        customer_id: The customer ID
    """
    _require_auth_if_key_present('get_roi', customer_id)
    from roi.measured import roi
    return _read(customer_id, lambda: roi(int(customer_id)))
