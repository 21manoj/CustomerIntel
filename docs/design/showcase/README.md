# Showcase tenants (2026-09-05)

Two minimal tenants created on CustomerIntelV1 **through the user-scoped path only** (`backend/scripts/onboard_tenant.py` over MCP/HTTPS with a key; no database access), both declared `data_origin=synthetic_demo` and disclosed as such on every surface.

| Tenant | customer_id | Manifest | Shape |
|---|---|---|---|
| Meridian Workflows (showcase, signals-only) | 10 | `backend/demo/manifests/showcase_signals_only_saas.json` | 3 accounts, 11 communications, 1 CRM flag, 2 outcomes, **no KPI layer** — journeys from evidence alone (phases_basis=evidence, arcs evidence_only) |
| Atlas Analytics (showcase, signals+KPI) | 11 | `backend/demo/manifests/showcase_signals_kpi_saas.json` | same three stories + weekly KPI file (2,322 rows) — both layers; Northstar: first leading warning 2026-05, first trailing 2026-06, **lead 31 days** |

Stories: Northstar Mutual (champion departure → sponsor rebuild → contraction, `exec_sponsor_change`), Greenfield Retail (adoption → advocacy → +150 seats, `expansion_champion`), Harbor Bank (routine only, `steady`). Every account carries `use_cases` and `attributes`.

Receipts (`*_receipt.json`) list every tool call and what it wrote. Re-create: regenerate files with `demo/generate.py --out-dir`, then run the client; remove with `delete_customer` (server key, domain confirmation, audited).

## One governed intervention per tenant (2026-09-05, through the tools only)

Run with `backend/scripts/showcase_intervention.py` over MCP/HTTPS with the server key — no database access. The deploy's stale-journey rebuild had already run the first playbook evaluation on every tenant (the hook in `run_wizard_a`), so both tenants carried `expansion_intent_handoff` proposals on Northstar and Greenfield before the script started. Neither tenant has a workflow endpoint configured, so every approval records `delivery.status = not_configured`; the row, the INTERVENTION node and the narrative say so instead of hiding it. The `report_intervention` calls below stand in for the external workflow's callback.

| Tenant | Intervention | What happened | Receipt |
|---|---|---|---|
| Meridian Workflows (10) | #13 `expansion_intent_handoff` (notify, human approval at automation level 0) on **Greenfield Retail**, urgency high, cites 4 `expansion_intent` episodes ("please quote 150 additional seats…") | approved → sent (not_configured) → started → **done** with outcome `expansion_opportunity` $48,000 (pipeline), in window, expected. Journey: episode `int:689`, hook lists `out:690`; narrative cites both. Per-playbook: realized $48,000 vs exposure $2,100,000 (two numbers). | `meridian_intervention_receipt.json` |
| Atlas Analytics (11) | a fresh `executive_escalation` email on **Northstar Mutual** (COO escalated to our CEO) submitted through `submit_signal` → the rebuild hook proposed #26 `escalation_exec_response` (escalate, critical) by itself | approved → sent (not_configured) → started → **done** with outcome `escalation_resolved` (protected, no revenue figure), in window, expected. Journey: episode `int:692`, phase `resolution`, lead_days still 31; narrative cites `int:692` and `out:693`. | `atlas_intervention_receipt.json` |

Two things the run caught and fixed the same day: a client sending `revenue: null` was rejected by the tool's `float` schema (now `Optional[float]`, and the client omits nulls); an outcome without a revenue figure read as "$0 protected" in the narrative (now said without an amount), and playbook labels with an em dash were truncated by the title stripper (labels now use a colon).
