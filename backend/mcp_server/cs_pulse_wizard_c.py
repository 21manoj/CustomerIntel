"""
CS Pulse MCP — Wizard C tools (weight calibration from logged outcomes).

    get_calibration(customer_id, proposal_id=None)      read scope
    approve_calibration(customer_id, proposal_id, note)  write scope — writes CustomerConfig, recomputes health
    reject_calibration(customer_id, proposal_id, note)   write scope

The proposal itself is made by trigger_wizard(customer_id, 'c') in
cs_pulse_onboarding (the wizards' one entry point). Registered on the shared
`mcp` instance; imported by server.build_asgi_app. Every tool is keyed
(onboarding_tool_registry.KEYED_TOOLS) — none is frictionless.
"""
from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


@mcp.tool
def get_calibration(customer_id: int, proposal_id: int = None) -> dict:
    """Wizard C read: the weights in force (and who set them), one calibration
    proposal in full — the outcome counts it was built from, per-KPI and
    per-pillar effects (samples on each side, mean score before positive vs
    negative outcomes, effect in points, standardised d, direction,
    confidence tier), proposed vs current weights, and the before/after on
    every account's latest month — plus the list of every proposal and its
    state. Answers "why is this pillar weighted 30%?" with rows, not a model.

    Args:
        customer_id: The customer ID
        proposal_id: A specific proposal (default: the latest)
    """
    _require_auth_if_key_present('get_calibration', customer_id)
    _check_mcp_enabled()
    with _get_flask_app().app_context():
        from wizards.wizard_c_calibration import get_calibration as _get
        try:
            return _get(int(customer_id), int(proposal_id) if proposal_id is not None else None)
        except ValueError as e:
            raise ToolError(str(e))


@mcp.tool
def approve_calibration(customer_id: int, proposal_id: int, note: str = None) -> dict:
    """Approve a Wizard C proposal: its pillar and KPI weights go into the
    tenant's CustomerConfig (customized_by='wizard_c:<id>', config_version
    bumped, weights_origin='wizard_c'), then health is recomputed through the
    normal pipeline so every row carries weight_source='wizard_c'. The
    approval, the key that made it and the recompute result are audited.

    Args:
        customer_id: The customer ID
        proposal_id: The proposal to approve (get_calibration lists them)
        note: Why (optional, kept on the row)
    """
    _require_auth_if_key_present('approve_calibration', customer_id)
    _check_mcp_enabled()
    with _get_flask_app().app_context():
        from wizards.wizard_c_calibration import approve
        try:
            return approve(int(customer_id), int(proposal_id), note=note)
        except ValueError as e:
            raise ToolError(str(e))


@mcp.tool
def reject_calibration(customer_id: int, proposal_id: int, note: str = None) -> dict:
    """Reject a Wizard C proposal. Nothing changes; the decision and the key
    that made it are audited, the note stays on the row.

    Args:
        customer_id: The customer ID
        proposal_id: The proposal to reject
        note: Why (optional)
    """
    _require_auth_if_key_present('reject_calibration', customer_id)
    _check_mcp_enabled()
    with _get_flask_app().app_context():
        from wizards.wizard_c_calibration import reject
        try:
            return reject(int(customer_id), int(proposal_id), note=note)
        except ValueError as e:
            raise ToolError(str(e))
