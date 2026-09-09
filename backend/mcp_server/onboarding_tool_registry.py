"""
MCP tool names — single source of truth for auth over HTTP.

ONBOARDING_TOOLS   frictionless: NO key needed. Reserved for tools that either
                   read nothing tenant-specific (list_verticals, get_csv_templates),
                   CREATE a brand-new tenant (create_customer — returns a real
                   customer-scoped key in its own response, so every tool acting on
                   an EXISTING customer_id has a key available to it by construction
                   and belongs in KEYED_TOOLS, not here), or carry their own
                   independent compensating control in the tool itself the way
                   clone_customer's unconditional is_synthetic gate does. If a key
                   IS present on a frictionless call it is still validated.
KEYED_TOOLS        everything else: over HTTP a key is REQUIRED — the server key,
                   or a customer key scoped to that customer (read scope for reads,
                   write scope for WRITE_TOOLS in auth.py). Found 2026-09-04: the
                   read surface, review, outcomes and Ask AI had been added to the
                   frictionless set and were reachable anonymously. Found again
                   2026-09-09: upload_csv/process_data/trigger_wizard/
                   configure_customer_kpis were in the frictionless set too — since
                   frictionless means "no key checked at all," not "no key required
                   for this tenant," any caller could pass ANY existing customer_id
                   to these and mutate that tenant's data with zero credentials, and
                   none of the four carry a compensating control the way
                   clone_customer does. create_customer itself was never the problem
                   (it can only ever create a NEW row); the four tools that act on a
                   customer_id someone else already owns are.

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
    'enable_features',
    'complete_onboarding',
    'clone_customer',
    'download_customer_csv',
})

KEYED_TOOLS = frozenset({
    'configure_customer_kpis',
    'upload_csv',
    'process_data',
    'trigger_wizard',
    'submit_signal',
    'research_account_external_signals',
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
    'configure_column_map',
    'get_column_map',
    'delete_customer',
    # playbook governance layer (playbooks/)
    'evaluate_playbooks',
    'approve_intervention',
    'report_intervention',
    'list_interventions',
    'configure_playbooks',
    'get_playbooks',
    # Power-of-1 / ROI (roi/, mcp_server/cs_pulse_roi.py) — reads
    'get_investment_priorities',
    'get_power_of_1',
    'get_roi',
    'get_investment_cost',
    # Wizard D — Foresight (mcp_server/cs_pulse_wizard_d.py)
    'get_forecast',
    # Wizard B — Hindsight (mcp_server/cs_pulse_wizard_b.py)
    'get_hindsight',
    # adapters (adapters/sources, mcp_server/cs_pulse_adapters.py)
    'import_from_source',
    # Wizard C (mcp_server/cs_pulse_wizard_c.py)
    'get_calibration',
    'approve_calibration',
    'reject_calibration',
    # CSM scorecard / capacity / daily actions / ranking (roi/csm.py, mcp_server/cs_pulse_csm.py)
    'get_csm_scorecard',
    'get_team_capacity',
    'get_csm_daily_actions',
    'get_csm_ranking',
})

ALL_TOOLS = ONBOARDING_TOOLS | KEYED_TOOLS
