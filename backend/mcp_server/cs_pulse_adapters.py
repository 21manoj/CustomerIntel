"""
CS Pulse MCP — adapter tools (keyed, write scope).

    import_from_source(customer_id, source, content, process_now=True, dry_run=False)

Registered on the shared `mcp` instance like cs_pulse_onboarding; imported by server.build_asgi_app.
Names are classified in onboarding_tool_registry.KEYED_TOOLS and auth.WRITE_TOOLS.
"""
from __future__ import annotations

from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


@mcp.tool
def import_from_source(customer_id: int, source: str, content: str, process_now: bool = True, dry_run: bool = False) -> dict:
    """Import an export from another system through the communications lane
    (the same path as import_communications), using a declared source adapter.

    Sources: gainsight_timeline — a Gainsight Timeline activity export (CSV):
    Activity ID → source_ref (a second import of the same file writes nothing),
    Activity Type → source_type, Subject + Notes → the text (HTML stripped),
    Activity Date → occurred_at, Company ID / Company Name → the account
    (external id first, then name), Author + attendees → participants; every
    other column is kept on the signal as attributes. Unknown accounts and
    rejected rows are reported with their row numbers, never dropped silently.

    Args:
        customer_id: The customer ID
        source: gainsight_timeline
        content: The export file's text (CSV)
        process_now: Classify + write evidence + rebuild journeys now (default true)
        dry_run: Parse and report only; writes nothing (default false)
    """
    _require_auth_if_key_present('import_from_source', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from adapters.sources import import_from_source as _imp
        try:
            return _imp(int(customer_id), source, content, process_now=process_now, dry_run=dry_run)
        except ValueError as e:
            raise ToolError(str(e))
