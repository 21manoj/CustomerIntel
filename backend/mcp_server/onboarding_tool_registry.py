"""
Frictionless MCP onboarding tool names — single source of truth.

Imported by cs_pulse_onboarding.py (tool implementations) and auth.py
(HTTP/stdio auth exemptions). Kept in a standalone module so contract
tests can run without fastmcp installed.
"""

ONBOARDING_TOOLS = frozenset({
    'list_verticals',
    'get_reference_customer',
    'get_vertical_config',
    'get_csv_templates',
    'get_onboarding_status',
    'validate_csv',
    'create_customer',
    'configure_customer_kpis',
    'enable_features',
    'upload_csv',
    'process_data',
    'trigger_wizard',
    'complete_onboarding',
    'clone_customer',
    'download_customer_csv',
})
