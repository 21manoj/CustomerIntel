# Port inventory — old repo (CustomerSuccessAI-DataCenter) → CustomerIntel

*2026-09-05. Every subsystem of the old backend, with its fate in the new build. Rule applied throughout (memory: "port must enhance value, not reproduce"): a subsystem is rebuilt on the evidence spine or retired; nothing is copied. Sizes: S days · M 1–3 weeks · L a quarter or gated on real data.*

## A. Done — rebuilt on the new spine

| Old subsystem | New build | Notes |
|---|---|---|
| Onboarding tools (create_customer, validate/upload CSV, templates, process_data) | `mcp_server/cs_pulse_onboarding.py`, `utils/csv_upload.py`, `utils/csv_ingest.py` | rewritten; upload lineage + run records added |
| Health scoring (score_calculator, vertical_health, generic_scorer, catalogs) | `utils/generic_scorer.py`, `utils/vertical_health.py`, `mcp_server/process_data_pipeline.py` | provenance on every row; noop scorer removed; lifecycle stages ported |
| Verticals as Python dirs (1,592 files, per-customer copies) | `config/*_kpi_catalog.json` + `taxonomy_*.json`, auto-discovered | retired as code; verticals are data |
| Taxonomy JSON | extended: role definitions, examples, per-vertical vocabularies | |
| Context graph (nodes/edges, invariants, provenance, supersession, edge factory) | `utils/context_graph_invariants.py`, `provenance.py`, `supersession.py`, `edge_factory.py` | ported with fixes |
| Wizard A (journey) | `journeys/` v3 — evidence-cited arcs, two layers, live months, narrative, versioned | **pending items below** |
| Wizard B (pattern) | `wizards/wizard_b_hindsight.py` + `evals/lead_time_backtest.py` | Hindsight only, by decision |
| Signal engine (ingest, enrichment, urgency, collision, fusion, worker) | `signal_engine/` v2 | fusion/collision retired; extraction v2; review write path |
| Ask AI + RAG/Qdrant (ask_ai_endpoint, tools, enhanced_rag) | `ask_ai/` over the journey contract | Qdrant/embeddings retired; the old tool registry was not ported |
| MCP server | `server.py` (FastMCP over HTTP) + tools | standalone, keyed |
| API keys, feature toggles, LLM budget controller | ported (`api_key_service.py`, `feature_toggles.py`, `utils/llm_budget_controller.py`) | budget metered, not yet enforced |
| Activity log | `models.ToolAuditLog` + `mcp_server/audit.py` | new shape |
| Load driver | `demo/` generator v2 (communications + scorecard) | rewritten |

## B. To build — in the agreed order

| # | Subsystem | Old code | Decision | Size |
|---|---|---|---|---|
| 1 | **Playbook governance layer** | orchestrator, triggers, webhook engine, execution/reports/recommendations APIs, work packages, cost bridge, knowledge, lifecycle, vertical routing, approval_queue | **new, minimal** (`playbook-governance-layer.md`): definitions as data, one table, 3 tools, one signed webhook, INTERVENTION node. Execution engine retired; `approval_queue.py` deleted. | ~1 wk |
| 2 | **Wizard A pending** | — | coverage profile derived from data (no flags), phases from evidence when no KPI layer, evidence-only arc variants, backtest on live months | S |
| 3 | **Wizard D (foresight)** | `wizards/wizard_d_predictor_calibrator.py`, `predictor/` (panel, GLMM, features, inference) | rebuild on journey features + logged outcomes; forecast block with CIs and the prior/calibrated label; Wizard A embeds the latest run | M–L (gated on outcomes) |
| 4 | **Power-of-1 / ROI** | `power_of_1_model.py`, `outcome_roi_engine.py`, `outcome_roi_api.py`, `playbook_cost_bridge.py` | rebuild on journey + outcomes + interventions: exposure-weighted priority now, measured impact from Hindsight; kill the dc2_s-only KPI-code coupling | M |
| 5 | **Wizard C (weights)** | `wizards/wizard_c_weight_calibrator_db.py` | rebuild: learn from logged OUTCOMES (not health — circular), human-approved calibration proposals with before/after recorded | M (gated on outcomes) |
| 6 | **UI** | React app (CRO/CFO/CSM dashboards, canvas mock) | new, as consumers of the read surface: Journey Canvas (live), portfolio, review queue, interventions; users/login come with it | L |
| 7 | **Users, login, RBAC, SSO/MFA** | auth_decorators, auth_middleware, admin/user APIs, magic link | new, with the UI; keys stay for integrations | M |
| 8 | **Outbound notifications** | `notifications_api.py`, providers (slack/email/jira/salesforce), integration_api/models, n8n models | folded into the playbook layer's `notify` action class + webhook; providers become adapters (#9) | with #1 |
| 9 | **Source adapters** | `providers/` (inbound direction did not exist) | new, first one chosen by the first design partner (Zendesk/Pylon or Gainsight/ChurnZero export); communications lane already exists | M each |
| 10 | **Schema migrations + backup/restore** | alembic/, migrations/, backup_restore_api | new: Alembic before the first real tenant; backup job | S–M |

## C. Retired — not coming back

| Old subsystem | Why |
|---|---|
| Qdrant / embeddings / enhanced RAG (and its ~20 scripts) | evidence is structured and cited; retrieval is the journey, not vectors |
| Signal analyst agent (`agents/`: analyst, decision matrix, converters, dedup, onboarding agent) | superseded by engine v2 (extraction, dedup, roster) and the urgency/divergence layers; the decision-matrix idea lives on as playbook trigger rules |
| LLM tier-1 (`llm/`: tier1 inference, anomaly explainer, causal reasoning, action-plan generator) | explanation is the cited narrative + Ask AI; action text is rendered by the external workflow |
| Playbook execution engine, templates, work packages | governance only, by decision |
| Composite fusion, CG collision | absolute-separation rule; content-hash dedup |
| 78 `*_api.py` REST modules, `api_v1_routes` | MCP + the read surface are the API; the UI consumes those |
| Celery/async jobs, cache API, analytics API, account snapshot APIs, admin cleanup | no need yet; revisit only with the UI |
| uuid_migration, one-off scripts (53), fix/verify scripts | one-off |
| Per-customer vertical Python dirs, `_evaluate_dc2s_playbooks`, dc2_s-only Po1 codes | vertical coupling |

## D. Cross-cutting, after the ports (governance High list)

Stamping bundle (confidence column, prompt/taxonomy version, methodology block) · journey snapshots · `data_origin` required at creation · LLM budget enforcement · review decisions bound to key_id · data-handling statement · extraction eval second pass · central tenant scoping · risk dashboard for IT.
