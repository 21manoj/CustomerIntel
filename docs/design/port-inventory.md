# Port inventory — old repo (CustomerSuccessAI-DataCenter) → CustomerIntel

*2026-09-05. Every subsystem of the old backend, with its fate in the new build. Rule applied throughout (memory: "port must enhance value, not reproduce"): a subsystem is rebuilt on the evidence spine or retired; nothing is copied. Sizes: S days · M 1–3 weeks · L a quarter or gated on real data.*

*Refreshed 2026-11-05 against `main` @ `6063f70` (2026-09-14). Evidence below is file, commit and test references checked in the code; the original plan text is kept unchanged except where a row says otherwise. Local verification the same day (Python 3.11, Postgres 16, pinned requirements): `main` 887 passed / 21 skipped / 0 failed; the unmerged TPRM branch (§F) 903 / 21 / 0; Alembic runs from an empty database to `0007_worker_heartbeats` and `scripts/schema_check.py` passes; the Caddy snippet covers all 69 registered routes; the server boots and `/health` reports ok. **Not verified:** the Docker image build, the frontend build, an EC2 deploy (see `ec2-deploy-verify-runbook.md`) and real LLM extraction. Status key: **Shipped** · **Partial** (a first version exists; the gap is named) · **Open** (no code) · **Done**.*

## A. Done — rebuilt on the new spine

| Old subsystem | New build | Notes |
|---|---|---|
| Onboarding tools (create_customer, validate/upload CSV, templates, process_data) | `mcp_server/cs_pulse_onboarding.py`, `utils/csv_upload.py`, `utils/csv_ingest.py` | rewritten; upload lineage + run records added |
| Health scoring (score_calculator, vertical_health, generic_scorer, catalogs) | `utils/generic_scorer.py`, `utils/vertical_health.py`, `mcp_server/process_data_pipeline.py` | provenance on every row; noop scorer removed; lifecycle stages ported |
| Verticals as Python dirs (1,592 files, per-customer copies) | `config/*_kpi_catalog.json` + `taxonomy_*.json`, auto-discovered | retired as code; verticals are data |
| Taxonomy JSON | extended: role definitions, examples, per-vertical vocabularies | |
| Context graph (nodes/edges, invariants, provenance, supersession, edge factory) | `utils/context_graph_invariants.py`, `provenance.py`, `supersession.py`, `edge_factory.py` | ported with fixes |
| Wizard A (journey) | `journeys/` v3 — evidence-cited arcs, two layers, live months, narrative, versioned | pending items shipped 2026-09-05 (see B2) |
| Wizard B (pattern) | `wizards/wizard_b_hindsight.py` + `evals/lead_time_backtest.py` | Hindsight only, by decision |
| Signal engine (ingest, enrichment, urgency, collision, fusion, worker) | `signal_engine/` v2 | fusion/collision retired; extraction v2; review write path |
| Ask AI + RAG/Qdrant (ask_ai_endpoint, tools, enhanced_rag) | `ask_ai/` over the journey contract | Qdrant/embeddings retired; the old tool registry was not ported |
| MCP server | `server.py` (FastMCP over HTTP) + tools | standalone, keyed; `stateless_http` since PR #18 (`6063f70`) |
| API keys, feature toggles, LLM budget controller | ported (`api_key_service.py`, `feature_toggles.py`, `utils/llm_budget_controller.py`) | budget metered; enforced at call time only on the external-research tool (see D4) |
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
| 10 | **Schema migrations + backup/restore** | alembic/, migrations/, backup_restore_api | **DONE 2026-09-05**: Alembic (`backend/migrations`, baseline `0001_baseline`, `utils/schema.migrate` at boot stamps pre-Alembic DBs), `tests/test_migrations.py` guard, `scripts/schema_check.py` in the deploy, `deploy/backup_db.sh` (pg_dump before every deploy + daily cron, keeps 14) + `restore_db.sh` | done |

### B — status at `main` @ `6063f70` (refreshed 2026-11-05)

The table above is the plan as agreed on 2026-09-05 and is kept as written. This table is what the code shows now.

| # | Subsystem | Status | Evidence | What is left |
|---|---|---|---|---|
| 1 | Playbook governance layer | **Shipped** (minimal, as decided) | `playbooks/{definitions,governance,http,webhook}.py`, `config/playbook_governance.json`, per-vertical definitions in `config/playbooks/`; `3869af1` (2026-09-05): one interventions table (proposed→approved→sent→closed) and one signed webhook. The plan said 3 tools; it shipped as six (`configure_playbooks`, `get_playbooks`, `evaluate_playbooks`, `list_interventions`, `approve_intervention`, `report_intervention`). `approval_queue.py` is gone. | Definitions exist for 4 of the 5 catalog verticals (none for `manufacturing_iot`). The seven TPRM playbooks are on the unmerged branch (§F). |
| 2 | Wizard A pending items | **Mostly shipped** | `13c806a`, `88427fa` (2026-09-05): coverage derived from data (`journeys/coverage.py`), phases from evidence when there is no KPI layer, evidence equivalents for the arc rules. | "Backtest on live months": `evals/lead_time_backtest.py:88` skips live and signals-only months (no trailing layer), so they are not backtested. Arc-rule quirk: §G2. |
| 3 | Wizard D (foresight) | **Shipped** (first version) | `wizards/wizard_d_{foresight,http,settings}.py`, `config/wizard_d.json`, migration `0003`; `e36b9e2` (template prior + Beta-binomial calibration, portfolio propagation, journey embed); tool `get_forecast`. | Calibration needs logged outcomes; until then the forecast carries the prior label (`wizard-d-foresight.md`). Not exercised on real outcomes. |
| 4 | Power-of-1 / ROI | **Shipped**, extended since | `roi/{basis,benchmarks,csm,investment,measured,power_of_1,priorities,settings,http}.py`; no `dc2_s` string left in `roi/`; tools `get_power_of_1`, `get_roi`, `get_investment_priorities`, `get_investment_cost`. Extended: investment-cost model `c3d6232`, peer-headroom ranking `f119ba7`, real sourced benchmarks with the fabricated citations removed `ea122c2`. | None recorded. `tprm_v1` is excluded from ROI by design (§F). |
| 5 | Wizard C (weights) | **Shipped** (first version) | `wizards/wizard_c_{calibration,http}.py`, `config/wizard_c.json`, migration `0004` (`WeightCalibration`, `CustomerConfig.weights_origin`); tools `get_calibration`, `approve_calibration`, `reject_calibration`, `configure_customer_kpis` (`aa8c60f`); UI page Calibrations. | Needs logged outcomes (same gate as Wizard D). |
| 6 | UI | **Shipped** (first build), served publicly | `frontend/src/pages/`: Login, Portfolio, AccountDetail, ReviewQueue, Interventions, Roi, Settings, Calibrations, JourneyCanvas ("Phase 1", `469e4ef`), AskAI; served at the existing hostname (`eb2d3c5`, 2026-09-08). | Later Journey Canvas phases (none found); an IT risk page (D9). The frontend build was not run for this refresh. |
| 7 | Users, login, RBAC, SSO/MFA | **Partial** | Email + password login with a session cookie separate from Bearer keys; roles reuse `User.role` / `allowed_customer_ids` / `allowed_account_ids` (`466e1ac`; `app_api/{auth,users,http}.py`); `vpcsm` role (`bca30ee`); tenant-scope fix `2f7ef51` with migration `0005`. | SSO/MFA: no code (deferred to the governance list by `ui-rbac.md`). |
| 8 | Outbound notifications | **Shipped** (Slack + signed webhook) | Notify-class interventions post to a tenant Slack incoming webhook (`playbooks/governance.py:301-330`, `adapters/slack_notify.py`); signed webhook with an n8n workflow export (`adapters/n8n/`) and a reference receiver (`adapters/receiver/`); `0c50648`. | No email, Jira or Salesforce senders (the only grep matches are inbound SendGrid parsing and comments). By design these go through the external workflow. |
| 9 | Source adapters | **Partial** | One source adapter, Gainsight Timeline (`adapters/sources/gainsight_timeline.py` + samples), the `import_from_source` tool and `config/adapters.json`. Inbound lanes ported from the old repo: SendGrid inbound email (`signal_engine/email_receiver.py`), Slack events (`signal_engine/slack_events.py`), CSV and `import_communications`. | Zendesk, Pylon, ChurnZero and others: backlog P3, P5, P14, P15 (`backlog-provisions.md`). |
| 10 | Schema migrations + backup/restore | **Done** | Alembic at revision `0007_worker_heartbeats` (7 revisions); `deploy/backup_db.sh`, `deploy/restore_db.sh`; verified from an empty database to head, and `scripts/schema_check.py` passes (2026-11-05). | — |

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
| `decisions.csv` / `signal_edges.csv` schema entries (`config/csv_schemas.json`, `context_graph_model.auto_generated`, `db_table: context_nodes`/`context_edges`, `source: "Inferred from story arc patterns..."`) | **found stale 2026-09-11**, not removed when row 43 above landed: this is the same retired signal-analyst/decision-matrix idea, described as if still planned. The loader and consumer are real and tested (`utils/csv_ingest.py`'s `load_decisions`/`load_signal_edges`; `process_data_pipeline.py`'s STAKEHOLDER↔DECISION `INVOLVES` linker) but no inference step exists or was ever planned under the new design — the new design's answer to "notice something decision-worthy" is the playbook trigger rules, not a `DECISION` node. Confirmed sitewide: 0 `DECISION` nodes, 0 non-`LED_TO` edges, across every tenant this deployment has ever created. Building the missing inference step would re-implement what row 43 already retired, not finish a pending feature — the schema entries should be removed or reclassified as `customer_provided`, not built out. |
| `accounts.csv` / `products.csv` schema entries (`config/csv_schemas.json`, `regular_model.files`) | **found stale 2026-09-11**: `regular_model._description` already says account_details.csv consolidates accounts + products + stakeholder metadata + business profiles into one file — these two are pre-consolidation remnants, not a second and third onboarding file. `accounts.csv` carries `_deprecated: true` and survives only as a fallback roster source (`utils/csv_ingest.py`'s `ACCOUNT_FILES = ('account_details.csv', 'accounts.csv')`, preference order); `products.csv` carries no such flag but has zero consumer — absent from `ingest_staged_csvs`'s entire file-processing chain, so an uploaded one is staged and never read. The live onboarding contract is 4 files: `account_details.csv`, `kpi_measurements.csv`, `enhanced_qualitative_signals.csv`, `outcomes.csv` (matches `demo/generate.py`'s own "four canonical CSVs" docstring) — not the 5-7 file counts a flat read of `regular_model.files`/the upload alias table would suggest. |

**Verified on `main` (2026-11-05):** none of the retired packages exist. The backend top level is `adapters, app_api, ask_ai, config, demo, evals, journeys, mcp_server, migrations, playbooks, roi, scripts, signal_engine, tests, utils, wizards` (no `agents/`, `llm/`, `predictor/`, `providers/` or `verticals/`); there is no `approval_queue.py`; `signal_engine/` has no fusion or collision module; no Qdrant import. **Stale comments to clean up:** `mcp_server/cs_pulse_onboarding.py:545` and `mcp_server/process_data_pipeline.py:25` still describe approval-queue seeding, Qdrant indexing and the onboarding agent as "later phases". They are retired, not deferred.

## D. Cross-cutting, after the ports (governance High list)

Stamping bundle (confidence column, prompt/taxonomy version, methodology block) · journey snapshots · `data_origin` required at creation · LLM budget enforcement · review decisions bound to key_id · data-handling statement · extraction eval second pass · central tenant scoping · risk dashboard for IT.

### D — status at `main` @ `6063f70` (refreshed 2026-11-05)

Sized in `governance-high-list-scoping.md` (2026-09-06). This table is what the code shows now.

| # | Item | Status | Evidence / what is left |
|---|---|---|---|
| 1 | Stamping bundle (confidence, prompt/taxonomy version, methodology) | **Open** | No `prompt_version` anywhere in non-test code. `HealthScore` provenance was already complete at scoping (not re-checked). |
| 2 | Journey snapshots | **Open** | No snapshot or history model in code; per the scoping, every Wizard A rebuild overwrites `journey_json` in place. |
| 3 | `data_origin` required at creation | **Mostly done** | At scoping (2026-09-06) `create_customer` required it with no default. Loose ends noted then (DB column nullable; `scripts/seed_demo.py` overwrite) were not re-checked. |
| 4 | LLM budget enforcement | **Partial** | `can_call_and_run` now has one production caller, `signal_engine/external_research.py:67-75` (added 2026-09-07). Signal extraction (`signal_engine/enrichment.py`) and Ask AI (`ask_ai/answer.py`) only record spend after the call (`record_usage`); extraction has a process-local daily call counter (`signal_engine/enrichment.py:162`). |
| 5 | Review decisions bound to `key_id` | **Open** | No `key_id` in `signal_engine/review.py` or `journeys/outcomes.py`. |
| 6 | Data-handling statement | **Open** | No such document under `docs/`. |
| 7 | Extraction eval second pass | **Open** | `evals/labelled/` is unchanged since `62ada9a` (2026-09-04). |
| 8 | Central tenant scoping | **Partial** | Fixed: the admin-role cross-tenant leak (`2f7ef51`, migration `0005`, `tests/test_app_api_tenant_isolation.py`); unauthenticated cross-tenant writes through onboarding tools (`e684813`, PR #1); constant-time key and secret comparison and the login timing side channel (`3d0eed3`, PR #2). Left: the ~93 ad hoc `customer_id=` filters (count from the 2026-09-08 scoping, not re-counted) and the unguarded evidence query in `journeys/read.py` (file unchanged since 2026-09-05). |
| 9 | Risk dashboard for IT | **Open** | `list_api_keys` exists in `api_key_service.py` with no route, tool or page; there is no risk page in `frontend/src/pages/`. |
| — | SSO/MFA | **Open** | See B7. |

## E. Built since the inventory was written (no old-repo counterpart)

| What | Evidence |
|---|---|
| External-world research tool; Wizard B `get_hindsight` | `cb06fd1` (2026-09-07); `signal_engine/external_research.py` |
| Po1/ROI extensions: investment-cost model, peer-headroom ranking, sourced benchmarks | B4 |
| Ask AI multi-turn continuity and citable external research; role-based Ask AI UI | `b692ee0` (2026-09-08), `469e4ef` (2026-09-06) |
| CSM / manager dashboard tools and `clone_customer` | `bca30ee` (2026-09-09); `mcp_server/cs_pulse_csm.py` (`get_csm_ranking`, `get_csm_scorecard`, `get_csm_daily_actions`, `get_team_capacity`) |
| "Why panel": surfacing why-questions that were computed but never shown | `ae86195`, `571d1d3` (2026-09-12) |
| Security hardening | D8 |
| Demo tenants: a 20-account datacenter tenant ("Aurelia Compute") and three 20-account portfolio tenants | `2892a60` (2026-09-07), `eb2547e` (2026-09-09) |
| Deploy: standalone path, public frontend, Alembic at boot with backups | `fac7f54`, `eb2d3c5`, `b1e79e0` |
| Backend scaling plan steps 1–4a: pool config and off-loop REST handlers (PR #16), the signal worker in its own container with a heartbeat (PR #17, migration `0007`), `stateless_http` on `/mcp` (PR #18) | `9dd44fa`, `fa0c827`, `6063f70`. The plan itself is a published artifact (Evidence Graph Platform Guide, v5), not a file in git. |
| Reliability fixes | signal drain race (PR #13), `ProcessRun` finalized on any failure (PR #15) |
| Operations docs | `fde-guide.md` (PR #14); `ec2-deploy-verify-runbook.md` |

## F. Not on `main` yet — TPRM (open PR #19)

| | |
|---|---|
| Branch | `feat/tprm-v1-signals-only-vertical`: 4 commits ahead of `main`, 0 behind (tip `1a47ede`, 2026-10-15); PR #19 open since 2026-09-14. 16 files, +1,048 / −10; no schema change. |
| What it adds | A `tprm_v1` signals-only vertical (taxonomy overlay `config/taxonomy_tprm_v1.json` and a deliberately empty KPI catalog); seven vendor-risk playbooks (`config/playbooks/tprm_v1.json`); a read-only, tenant-checked MCP tool `get_evidence_graph`; phase-transition `LED_TO` edges gated on `vertical == 'tprm_v1'`; `SIGNALS_ONLY_VERTICALS` in `utils/vertical_registry.py`. |
| Who consumes it | A separate TPRM pilot application (its own private repo, not part of this repo) that calls CustomerIntel only over `/mcp` with Bearer tenant keys. CustomerIntel holds no TPRM UI. The sequencing decision (TPRM first, self-serve taxonomy authoring deferred) is in the Evidence Graph Platform plan (v5), a published artifact. |
| Verified 2026-11-05 | Branch suite 903 passed / 21 skipped / 0 failed. The pilot's integration tests pass against it. Against `main` one fails (`vendor_security_breach` is stored as the generic `incident`) and the end-to-end run stops at the missing `get_evidence_graph` tool (`main` registers 42 tools, the branch 43). |

## G. Findings from the 2026-11-05 verification (open)

1. **`main` accepts an unregistered vertical silently.** `create_customer(vertical='tprm_v1')` on `main` succeeds with `customer_uuid` null; TPRM subtypes degrade to generic roles (`incident`) or `unclassified_signal` and nothing fails. ROI already fails closed on an unknown vertical (`b208e8e`); `create_customer` does not.
2. **`crisis_recovery` fires without a recovery.** The rule at `journeys/arc_classifier.py:68-72` needs only `infra_incident` or `escalation` plus a dip condition, while the comment at lines 30-33 says the arc needs the crisis and the recovery. In the local TPRM run an unresolved vendor (roles `commercial_pressure`, `escalation`) got `crisis_recovery` at 0.8 confidence, the same label and confidence as a vendor that did recover.
3. **A key in the URL is ignored unless `MCP_ALLOW_QUERY_KEY=true`** (default off, by design). `tools/list` answers without a key, so a single-URL connector looks connected while every keyed tool is refused. Reproduced locally; handling is in the runbook (§6).

## Related documents

`governance-high-list-scoping.md` (sizing for D) · `backlog-provisions.md` (P1–P16, unscheduled) · `fde-guide.md` · `ec2-deploy-verify-runbook.md`
