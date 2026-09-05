# Showcase tenants (2026-09-05)

Two minimal tenants created on CustomerIntelV1 **through the user-scoped path only** (`backend/scripts/onboard_tenant.py` over MCP/HTTPS with a key; no database access), both declared `data_origin=synthetic_demo` and disclosed as such on every surface.

| Tenant | customer_id | Manifest | Shape |
|---|---|---|---|
| Meridian Workflows (showcase, signals-only) | 10 | `backend/demo/manifests/showcase_signals_only_saas.json` | 3 accounts, 11 communications, 1 CRM flag, 2 outcomes, **no KPI layer** — journeys from evidence alone (phases_basis=evidence, arcs evidence_only) |
| Atlas Analytics (showcase, signals+KPI) | 11 | `backend/demo/manifests/showcase_signals_kpi_saas.json` | same three stories + weekly KPI file (2,322 rows) — both layers; Northstar: first leading warning 2026-05, first trailing 2026-06, **lead 31 days** |

Stories: Northstar Mutual (champion departure → sponsor rebuild → contraction, `exec_sponsor_change`), Greenfield Retail (adoption → advocacy → +150 seats, `expansion_champion`), Harbor Bank (routine only, `steady`). Every account carries `use_cases` and `attributes`.

Receipts (`*_receipt.json`) list every tool call and what it wrote. Re-create: regenerate files with `demo/generate.py --out-dir`, then run the client; remove with `delete_customer` (server key, domain confirmation, audited).
