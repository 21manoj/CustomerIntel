"""
MCP tool names — single source of truth for auth over HTTP.

ONBOARDING_TOOLS   frictionless: NO key needed (prospects create a tenant and
                   load CSVs before anyone issues a key). If a key IS present it
                   is still validated.
KEYED_TOOLS        everything else: over HTTP a key is REQUIRED — the server key,
                   or a customer key scoped to that customer (read scope for reads,
                   write scope for WRITE_TOOLS in auth.py). Found 2026-09-04: the
                   read surface, review, outcomes and Ask AI had been added to the
                   frictionless set and were reachable anonymously.

Imported by cs_pulse_onboarding.py and auth.py. Kept in a standalone module
so contract tests can run without fastmcp installed.
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

KEYED_TOOLS = frozenset({
    'submit_signal',
    'process_signals',
    'configure_signal_engine',
    'list_journeys',
    'get_journey',
    'get_evidence',
    'get_review_queue',
    'review_signal',
    'log_outcome',
    'ask',
    'declare_data_origin',
    'import_communications',
})

ALL_TOOLS = ONBOARDING_TOOLS | KEYED_TOOLS
