# Governance pass, platform-wide — G1–G4 over every subsystem in this build

*2026-09-04. Decision document; no code changed. Code-verified against `backend/` at commit `25d486e` (main). Supersedes the priority list in `google-gap-priorities.md` (which was scoped to the evidence spine + governance); method and the four areas are in `evidence-spine-assessment.md` §0. Sizes: S = days, M = 1–3 weeks, L = a quarter or gated on real data.*

**The four areas, one line each:** G1 verifiable grounding (citations, confidence, methodology, snapshots, provenance) · G2 governed control plane (audit, isolation, permissions, spend, data handling) · G3 domain skills as data + connectors · G4 human verification before consequence.

**The frame is unchanged:** does the gap make "what happened, who, how early, how sure — with the evidence attached" more defensible to a CRO / CFO / VP CS, or cheaper to prove to a SOC2-minded IT reviewer? Two facts from the earlier doc still hold — no tenant has real data, and the surface is small (25 MCP tools, 24 HTTP routes) so control-plane work is still cheap.

---

## 1. What shipped since `google-gap-priorities.md` (verified at HEAD)

| Earlier item | Status | Proof |
|---|---|---|
| Read surface (G1) | **Done** | `journeys/read.py:65-86` (`get_journey` + evidence index), `journeys/http.py:22-59`, MCP `list_journeys`/`get_journey`/`get_evidence` (`cs_pulse_onboarding.py:763-833`) |
| Review write path + override audit; `requires_review` honoured (G4, G2) | **Done** | `signal_engine/review.py:116-119` writes `SignalReview`; `journey_builder.py:304-305` weights unreviewed low-confidence at `unreviewed_low_confidence_weight` (0.5, `health_thresholds.json`); rejected evidence excluded at `:86` |
| Narrative with citation rule | **Done** | `journeys/narrative.py:22-23` (`template_v1`, uncited sentences dropped to `omitted`) |
| Generator version + stale rebuild | **Done** | `wizard_a.py:32` (`GENERATOR_VERSION='3.2'`), `stale_journey_query` `:35`, `scripts/rebuild_stale_journeys.py`, run by `deploy_ec2.sh:49`; `/health` reports `stale_journeys` (`server.py:125`) |
| Outcome logging | **Done** | `journeys/outcomes.py:56` — vocabulary-enforced (`:74`), idempotent, LED_TO edges (`:117`) |
| CSV lane through the engine | **Done** | `csv_ingest.py:368-417` → `pipeline.ingest`; node `source_event_id = source_ref or signal_id` (`pipeline.py:289`) |
| Ask AI over the contract (P10) | **Done** | `ask_ai/answer.py` — reads only `journeys.read`, one forced tool call, every sentence must cite (`:50-51`), `as_of` scrubber (`:170-199`), metered (`:385,:390`) |
| Gap 7 tool-call audit log | **Done** | `mcp_server/audit.py:16`; `ToolAuditLog` (`models.py:100`); `GET /api/audit` (`journeys/http.py:92-105`) |
| Gap 8 (half) query-string keys | **Done** | `server.py:36` default false |
| Gap 5 (half) persisted backtest | **Done** for Wizard B | `wizard_b_hindsight.py:301-308` writes a `WizardRun` with the backtest and `evidence_label` |
| Gap 6 extraction eval set | **Seeded** | `evals/labelled/demo_signals_only_saas.claude-sonnet-5.*`: 31 communications, exact 16 / partial 13 / miss 2 / unclassified 1; subtype P 0.571 / R 0.933 / F1 0.709 (21 FPs, most fixed by `402911b` dedup; not re-run) |

Still open from that list: gaps 1–4 (stamping bundle, snapshots), 5's `data_origin` in `create_customer` (only `demo/generate.py:307` and `scripts/seed_demo.py:69` stamp it), 8's central isolation, 9 (data-handling statement), 10 (first adapter).

---

## 2. Subsystem verdicts

### 2.1 Health scoring (`process_data_pipeline.py`, `utils/vertical_health.py`, `utils/generic_scorer.py`, `config/*_kpi_catalog.json`)

| Area | Verdict | Proof |
|---|---|---|
| G1 grounding | **Missing** | A `HealthScore` row stores score, status, `contributing_pillars`, `calculated_at` (`process_data_pipeline.py:154-162`) and nothing that produced it: no pillar/KPI weights (`:76-77` admits `pillar_weights` "not written"), no catalog version, no KPI row ids, no upload id. The scorer returns `(health, pillars)` only (`generic_scorer.py:199`) — the weights it used are discarded. |
| G1 confidence/methodology | **Missing** | No `confidence_semantics`, no coverage (KPIs present / expected). `generic_scorer.py:145-146` drops unknown KPI codes silently; `:190` defaults a missing `weight_l2` to 0.2 silently. `common.py:228-231` swaps in `_noop_calculate → (0.0, {})` on *any* scorer-construction error, so a broken catalog writes health 0.0 / `critical` as if measured (guard-never-fires class). |
| G1 snapshots | **Partial** | Immutable-while-inputs-unchanged rule is real (`:50-59`, reopen set `:117-120`); but a reopened month is upserted in place (`:170-174`) — no before-image. |
| G2 audit | **Missing** | `process_data` returns steps/errors/timings to the caller (`cs_pulse_onboarding.py:478`) and persists none of it; Wizard A via `process_data` writes no `WizardRun` (only `trigger_wizard` does, `:590`). |
| G3 skills | **Done** | Catalog JSON per vertical, auto-discovered; declared-vertical check (`test_catalog_no_silent_substitution.py`); `CustomerConfig.pillar_weights/kpi_weights` overlays (`models.py:202-205`). |
| G4 | **Missing** | No override path for a score; `CustomerConfig` weight edits have `config_version` (`models.py:212`) but no history or approver. |

### 2.2 CSV ingest + KPI lineage (`utils/csv_upload.py`, `utils/csv_ingest.py`, `CsvUploadStaging`)

| Area | Verdict | Proof |
|---|---|---|
| G1 provenance | **Missing** | `KPIMeasurement` has no upload/batch id (`models.py:525-534`); staging rows are **deleted** after a clean ingest (`csv_ingest.py:969-972`), so the CSV behind a score cannot be re-read or hashed. |
| G1 no fabrication | **Partial** | Blank `value` becomes `0.0` and is scored (`csv_ingest.py:351`). `ON CONFLICT DO NOTHING` (`:361`) silently ignores a corrected re-upload of the same (account, code, month); the count of ignored rows is not reported. Unknown KPI codes are accepted at ingest and dropped at scoring. |
| G2 audit | **Partial** | `upload_csv` validates against `csv_schemas.json` (`csv_upload.py:244`) but the validation result is returned, not stored; `ToolAuditLog` records the call, not what was in it. |
| G3 | **Partial** | Schemas are config; the source-field → KPI-code mapping is still the uploader's job (P3 adapters). |
| G4 | n/a | |

### 2.3 Taxonomy / catalog / config versioning

| Area | Verdict | Proof |
|---|---|---|
| G1 | **Partial** | Files carry `version` (`taxonomy_*.json` all `"0.1"`, catalogs 1.0/3.0/3.1); `taxonomy_loader.py:312` exposes it; **no node, score, journey or run records it**. `health_thresholds.json` (`leading_indicator` block) has no version and its values (window 60 d, λ 0.023, 0.5 unreviewed weight) are not stamped on the journey beyond `window_days` (`journey_builder.py:342`). `signal_engine.json`/`ask_ai.json` set the model ids; only `llm_model_version` reaches a node (`pipeline.py:275`), no `prompt_version`. |
| G2 | **Missing** | No per-tenant record of which config versions stood behind a month's outputs. |
| G3 | **Done** | Overlays with role definitions + few-shot examples enforced as a tool-schema enum (`enrichment.py`, P16). |

### 2.4 Journeys / Wizard A (`journeys/`)

| Area | Verdict | Proof |
|---|---|---|
| G1 citations | **Done** | Episodes cite `evidence_node_ids`; arc cites `supporting_episode_ids`, `alternatives`, `contradicting_evidence`, `reason` on `steady`/`unclassified` (`arc_classifier.py:185-221`); narrative cites or omits. |
| G1 confidence | **Partial** | Arc `rule_match_constant` labelled (`:189`); `trajectory_confidence` (`journey_builder.py:463`) and `stub_confidence` 0.5 in Ask AI are unlabelled numbers. |
| G1 methodology | **Partial** | Journey has `version`, `as_of`, `window_days`, `generator_version`; no single block with thresholds, decay, taxonomy/catalog versions, coverage. |
| G1 snapshots | **Missing** | Upsert in place (`wizard_a.py:93-98`); `as_of` scrubber in Ask AI reconstructs from the *current* JSON, not from what was said then. |
| G2/G4 | **Partial** | Human review flows in; `Account.arc_type` overwritten each run; no override on an arc. |
| G3 | **Done** | `config/story_arcs/*.json` as `expected_path` overlays only (`wizard_a.py:13-14`). |

### 2.5 Wizard B Hindsight + lead-time backtest (`wizards/`, `evals/`)

| Area | Verdict | Proof |
|---|---|---|
| G1 | **Done** (design) | Pre-registered thresholds (`lead_time_backtest.py:50`), `measured` only when `data_origin` NULL **and** `--real` (`:215-217`), interventions labelled "a comparison, not a causal estimate", derived rules with frequency + median lead. |
| G1 replayability | **Partial** | Run persisted (`wizard_b_hindsight.py:301`) but cannot cite journey snapshots (none exist); `trigger_wizard` runs it with `persist=False` then stores its own `WizardRun` (`cs_pulse_onboarding.py:580,590`) — two shapes for one result. |
| G2 | **Partial** | `create_customer` never sets `data_origin`, so a hand-uploaded synthetic tenant + `--real` yields `measured`. Gate exists; the stamp does not. |

### 2.6 Ask AI (`ask_ai/`)

| Area | Verdict | Proof |
|---|---|---|
| G1 | **Done** | Cites or drops (`answer.py:50-51`), numbers flagged if not in cited blocks, `evidence_gaps`, model + generator returned; context budget truncation is disclosed (`:302`). |
| G2 | **Partial** | LLM call metered but **not gated**: `can_call` is never called by `ask_ai` or `enrichment.py` (grep: only `llm_budget_controller.py` itself) — the spend ledger records, it does not cap. Question and answer are not persisted; only the tool call is audited. No `prompt_version` on the answer (`SYSTEM_PROMPT` unversioned, `:53`). |
| G4 | n/a | Read-only. |

### 2.7 Generator / demo data (`demo/`)

| Area | Verdict | Proof |
|---|---|---|
| G1 | **Done** | `data_origin='synthetic_demo'` (`generate.py:307`); scorecard labelled with `model_version`, oracle runs self-declare 100%-by-construction (`oracle.py`). |
| G1 eval | **Seeded** | One vertical, one model, 31 comms; no pre-registered per-role thresholds; not re-run after the dedup fix. |
| G2 | **Partial** | `seed_demo.py` runs on every deploy (`deploy_ec2.sh:53`) into the production DB — synthetic tenants beside real ones, distinguishable only by `data_origin`. |

### 2.8 Auth / keys / audit / approvals (`api_key_service.py`, `mcp_server/auth.py`, `mcp_server/audit.py`, `approval_queue.py`)

| Area | Verdict | Proof |
|---|---|---|
| G2 audit | **Done** | Both chokepoints write `ToolAuditLog` (`auth.py:123-161`, `signal_engine/http.py:39-54`); `/api/audit` tenant-scoped unless server key. Tested (`test_signal_engine_v2.py:540-545`). |
| G2 isolation — HTTP routes | **Done** | `_authorize` fails closed with no key when `MCP_AUTH_REQUIRED` (`http.py:37-40`). |
| G2 isolation — MCP tools | **MISSING (new, severe)** | **Every one of the 25 tools is in `ONBOARDING_TOOLS`** (`onboarding_tool_registry.py:9-35`), including `get_journey`, `get_evidence`, `review_signal`, `log_outcome`, `ask`. `require_auth_if_key_present` returns *allowed* when no key is presented (`auth.py:126-129`). Over HTTP, an anonymous caller can read any tenant's evidence and write reviews/outcomes for any `customer_id`. Isolation holds only if the caller volunteers a key. The audit row records it as `key_kind='none', outcome='allowed', detail='frictionless onboarding'`. No test exercises a keyless read tool (`test_server_http.py:95` covers `create_customer` only). |
| G2 central scoping | **Missing** | 86 ad-hoc `customer_id` filters; `read.py:74` fetches evidence nodes by id with no tenant filter (ids come from the tenant's own journey, so safe today, not by construction). |
| G2 keys | **Done** | SHA-256 hashed, prefix lookup, scopes, expiry, IP rate-limit (`api_key_service.py:112-137`). Gap: `expires_at` never set by `create_customer`; no rotation; failure tracker is per-process. |
| G2 RBAC/SSO | **Missing** | `auth_middleware.py` is a `NotImplementedError` stub; `User.role` unread. |
| G4 reviewer identity | **Partial** | `reviewer` is caller-supplied free text (`review.py:67,119`); not bound to `key_id`. A tenant key can attribute a decision to anyone. |
| G4 approvals | **Missing** | `approval_queue.py` is **not mounted** anywhere (only referenced from its own docstring and `auth_middleware.py`); tiers key on model confidence with auto-execute ≥ 0.85 (`:69,:105`). Nothing consequential exists yet to gate. |

### 2.9 Server / deploy (`server.py`, `deploy/`)

| Area | Verdict | Proof |
|---|---|---|
| G2 secrets | **Partial** | `deploy/.env` git-ignored, example committed; secrets are plain env vars in the container (compose `:53-62`); `TEST_DATABASE_URL` is set in the production container. Container runs as root (no `USER` in `Dockerfile`). |
| G2 exposure | **Partial** | `/health` is unauthenticated through Caddy and returns tenant/journey/run counts, `git_sha`, `auth_required` (`server.py:131-137`). Ports bound to 127.0.0.1; Caddy owns 443. |
| G2 ops | **Missing** | No backup/restore (no `pg_dump` step); schema is `db.create_all()` additive-only with no migration tool (`server.py:18-19`); `MCP_AUTH_REQUIRED=false` is a one-env-var full bypass, logged CRITICAL (`auth.py:59-65`). |
| G2 LLM data path | **Partial** | Only `raw_text[:prompt_text_chars]` + roster leaves the box (`enrichment.py:320-321`); no data-handling statement yet. |

### 2.10 Not in this build — evaluate when ported

**Wizard C** (weight calibration), **Wizard D** (NRR foresight), **Power-of-1 / ROI dollars**, **playbooks / actuator**, **UI**. `trigger_wizard` names only `a`/`b` (`cs_pulse_onboarding.py:532-535`); `get_playbook_config` is a no-op stub (`common.py:240-247`). The old-repo findings in `backlog_google_principles_platform_wide_pass.md` (Po1 hardcoded benchmarks, Wizard D point estimates without CIs, Wizard C learning from HealthScore) remain the checklist for that pass; nothing here should be read as clearing them.

---

## 3. Ranked list — High (merged with the still-open earlier items)

| # | Item | Area | Subsystem | Buyer / claim | Size | Depends on |
|---|---|---|---|---|---|---|
| 1 | **Close the anonymous-tool hole.** Split `ONBOARDING_TOOLS` into a true frictionless set (`list_verticals`, `get_csv_templates`, `create_customer`, `validate_csv`…) and everything else, which must go through `require_scoped_read`/`require_auth`; add a test that enumerates registered tools and fails on any read/write tool reachable without a key. | G2 | auth | IT · "no key, no data" — today untrue over HTTP | S (a day) | none — **do first** |
| 2 | **Health-score provenance.** Persist on `HealthScore`: `pillar_weights`, `kpi_weights` used, `catalog_version`, `taxonomy_version`, `kpi_codes_used` / `kpi_codes_dropped`, `weight_source` (catalog / config / lifecycle), `input_batch_id`. Make the scorer return what it used; remove `_noop_calculate` (raise, don't write 0.0); report dropped codes and blank-value rows at ingest instead of coercing to 0.0. | G1 | health scoring, CSV | CFO · "a score is a receipt: these KPIs, these weights, this catalog" | S–M | none |
| 3 | **Ingest lineage.** Keep the upload: `csv_uploads(id, customer_id, file_type, sha256, row_count, validation_json, key_id, uploaded_at)`; stamp `KPIMeasurement.upload_id`; persist the `process_data` run (steps, errors, counts, ignored-conflict rows) as a run row; stop deleting staging content until the upload row exists. | G1/G2 | CSV | IT/CFO · "which file produced this month" | S | 2 (shares batch id) |
| 4 | **Stamping bundle** (earlier gaps 1–3): `ContextNode.confidence` written (`pipeline.py:283-290` never sets it — column stays 1.0), `confidence_semantics` (`llm_self_report_explicitness` / `rule_map_constant` / `stub_keyword`), `prompt_version` + `taxonomy_version` on every node; one `methodology` block per journey (thresholds, λ, window, versions, evidence counts, coverage). | G1 | signal engine, journeys | every demo · "how sure" must say what it is | S | none; before 5 |
| 5 | **Point-in-time snapshots** (earlier gap 4): `journey_snapshots(run_id, account_id, as_of, journey_json, generator_version)` append-only from `run_wizard_a`; `HealthScore` before-image on reopen; Wizard B runs cite snapshot ids; Ask AI `as_of` reads the snapshot when one exists. | G1 | journeys, health, Wizard B | VP CS/CFO · "what did you say on 12 March" | S–M | 4 |
| 6 | **`data_origin` at creation** (earlier gap 5 remainder): `create_customer(..., data_origin=)` required; `synthetic_*` for demo/seed, source system for adapters, NULL only on an explicit real flag; unify `trigger_wizard`'s Wizard B run with `run_wizard_b(persist=True)`. | G1/G2 | onboarding, Wizard B | CFO/board · the honesty gate on "measured" | S | none |
| 7 | **Enforce the LLM budget + version the prompts.** Call `can_call` before every model call (enrichment, Ask AI); per-tenant cap in config; `prompt_version` constants in `enrichment.py`/`answer.py` stamped on nodes and answers. | G2 | signal engine, Ask AI | IT · spend is capped, not just counted | S | none |
| 8 | **Bind human decisions to identity.** `SignalReview.key_id` (+ `log_outcome.decided_by`) from the validated key; `reviewer` becomes a display label, never the record of who. | G4 | review, outcomes | VP CS · an override names its author | S | 1 |
| 9 | **Data-handling statement** (earlier gap 9) — one page, true today: what leaves the box (`raw_text[:N]` + roster to Anthropic), metered where, consent on transcripts, synthetic seeding, retention, no training. | G2 | doc | IT · first security-review question | S (a day) | none |
| 10 | **Extraction eval, second pass** (earlier gap 6): re-run after `402911b`; pre-register per-role precision/recall thresholds; add the datacenter manifest; report per `prompt_version` × model; include unclassified and polarity-conflict rates. | G1 | generator/evals | VP CS/IT · "the extractor is measured" | M | 4 (versions) |
| 11 | **Central tenant scoping** (earlier gap 8 remainder): one scoped-query helper; `get_evidence`/`get_journey` node fetches filtered by `customer_id`; test that every tool passes through it. | G2 | auth | IT · isolation enforced once | S–M | 1 |
| 12 | **First adapter** (earlier gap 10) — buyer-chosen, the only route to real data; builds the seam once. | G3 | ingest | everyone | M | 3, 6 |

Order: 1 the same afternoon; 2–3 together (health scoring is the one subsystem a CFO acts on that currently cites nothing); 4–5–6 as the G1 block before any surface freezes the JSON; 7–9 are each a day; 10–12 turn design into measurement.

## 4. Medium — buyer-triggered

| Item | Trigger | Shape |
|---|---|---|
| Non-root container, `pg_dump` → S3 + documented restore, a migration tool (`create_all` is additive-only) | first paying tenant, or a SOC2 questionnaire | `USER app` in Dockerfile; nightly dump job in compose; Alembic |
| `/health` split | first external security scan | public `/health` = status only; counts + sha behind the server key |
| Key rotation + expiry defaults | a buyer's key-management policy | `expires_at` default 365 d; `rotate_api_key`; last-used report |
| Score / arc override with approver | first CSM disputing a score in front of a buyer | `HealthScoreOverride` audit row; arc override stamped on the journey as `human_override` with reason |
| Weight-change history | Wizard C port, or a buyer asking "who changed the weights" | `CustomerConfig` history table keyed by `config_version` + approver; stamped on scores (item 2) |
| RBAC / role readings (P11); SSO/MFA | first UI, or IT requiring it — OIDC only, never home-grown | read `User.role`; default framings per role |
| Permission inheritance | a CRM adapter plus CSMs who must not see the whole book | CRM owner/territory → `allowed_account_ids`, labelled inherited |
| Tenant trust / risk view | first security review after the audit log | `get_tenant_trust`: LLM calls + spend, text sent (count), keys + last use, review backlog, unclassified rate, months without evidence, coverage (P16-8) |
| Approval tiers | the first actuator — not before items 1, 8 | mount `approval_queue` behind real auth; tier by **action class**, never by LLM confidence; keyed to episode ids |
| Ask AI Q/A log | a buyer asks to review what the model told their team | append-only `ask_log(customer_id, key_id, question, answer_json, model, prompt_version)` |
| Per-customer BYOK | a buyer asks | encrypted key in `CustomerConfig`, read before env |
| Demo seeding on deploy | first real tenant on the box | `--no-seed` default; seed only to a tagged demo customer set |

## 5. Low / not for CS Pulse

| Item | Reason |
|---|---|
| Partner-agent ecosystem | We are the agent over the buyer's evidence; the MCP server is our openness. |
| CMEK / dedicated tenancy | Nobody buying an early-warning layer asks before SSO and a SOC2 report. |
| Ethical walls | A CS team wants shared account context; the legal-matter model does not transfer. |
| Speculative adapters beyond the first | Zero value until a buyer supplies credentials and data. |
| Second extraction model / prompt tuning | Blocked on item 10 — nothing to measure against until the eval is pre-registered. |
| Wizard C/D, Po1, playbooks, UI governance | Not in this build; evaluate on port with the checklist in §2.10. |

## 6. What this buys

Item 1 removes a claim we cannot make today ("no key, no data"). Items 2–3 make the health score — the number a CFO actually acts on — a receipt rather than an assertion, which is the G1 standard the signal engine already meets. Items 4–6 finish G1 on the evidence side and put the honesty gate where a tenant is born. After 7–9 the IT reviewer gets a capped model spend, an attributed override trail and a true data-handling page. Items 10–12 are where the product stops being defended by design and starts being defended by measurement.
