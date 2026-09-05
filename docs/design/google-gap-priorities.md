# Google gap priorities — what to build from G1–G4, for CS Pulse

*2026-09-04. Decision document; no code changed. Code-verified against `backend/` at commit `c431487`. Companion to `evidence-spine-assessment.md` (§0: Google's four areas, our verdicts) and `backlog-provisions.md` (P1–P16). Sizes: S = days, M = 1–3 weeks, L = a quarter or gated on real data.*

---

> **Status 2026-09-04 (later):** the two "already underway" items shipped, and so did: outcome logging, Ask AI over the contract, Generator v2 + first real extraction scorecard (`backend/evals/labelled/`), the tool-call audit log (`GET /api/audit`), and query-string keys off by default (part of gap 7/8 here). The superseding, platform-wide list is `governance-pass-platform-2026-09.md`.

## 0. The frame

Google wrote its four areas for a law firm filing a brief. Our buyer is a CRO / CFO / VP CS who has to believe an early warning, and an IT reviewer who has to let customer emails reach us. Each gap is judged on one question: **does it make "what happened, who, how early, how sure — with the evidence attached" more defensible in a sale, or cheaper to prove later?** Anything that only makes us resemble Gemini Enterprise is dropped.

Two facts shape the ranking. **No tenant has real data** — `Customer.data_origin` is stamped only by `demo/generate.py:253`; `create_customer` never sets it — so every "measured" claim is gated on a first adapter, not on more machinery. And **the surface is small right now**: seven MCP tools (`mcp_server/cs_pulse_onboarding.py`) and eight signal routes (`signal_engine/http.py`). Governance plumbing that cost 255 files in the old repo costs an afternoon here, and gets expensive again once the read surface lands.

## 1. Already underway — excluded from evaluation

| Item | Area |
|---|---|
| Read surface over journeys / evidence nodes (`get_journey`, `get_evidence`, review queue as read tools) | G1 |
| Review write path (accept / reject / reclassify) with an override audit row; `requires_review` honoured in the leading composite (`journeys/journey_builder.py:264` today reads neither `requires_review` nor `confidence`) | G4, G2 |

The override audit covers *human decisions on nodes*, not *who called which tool* — that is gap 7.

## 2. Gap inventory — every remaining item, evaluated for CS Pulse

| # | Gap | Area | Code today | Buyer · claim | Real data? | Size | Value |
|---|---|---|---|---|---|---|---|
| 1 | **Confidence semantics on extraction** + fix the column | G1 | `pipeline.py:258` writes `properties.confidence` (LLM self-report, `enrichment.py:56`); `ContextNode.confidence` keeps its default `1.0` (`models.py:264`); no `confidence_semantics` label (the arc has one, `arc_classifier.py:189`) | every demo · "how sure" is in the one-liner, so the number must say what it is | no | S | **High** — unearned-confidence bug class; the in-progress composite weighting needs a real value |
| 2 | **Prompt + taxonomy version on nodes** | G1 | only `llm_model_version` is stamped (`enrichment.py:338`); `taxonomy_base.json` is `"version": "0.1"` and `taxonomy_loader.py:76` exposes it, but no node carries it; no `prompt_version` anywhere | IT / VP CS · "every citation names the extractor, prompt and vocabulary that produced it" | no | S | **High** — after the next prompt change old and new extractions are indistinguishable; gap 6 cannot be scored |
| 3 | **Methodology block on the journey** | G1 | partial: `window_days` on the leading series (`journey_builder.py:332`), `confidence_semantics` on the arc, `generator_version` on `JourneyData`; no single block with thresholds used, decay lambda, versions, evidence counts, coverage | CFO · Google's "explicit methodology"; canvas header and Ask AI read one block | no | S | **High** — must exist before the read surface freezes its JSON |
| 4 | **Point-in-time journey snapshots** | G1 | `wizard_a.py:57-69` upserts `JourneyData` in place. The backtest is point-in-time by construction (`lead_time_backtest.py` docstring), so lead-time claims do not need this — but "what did you say on 12 March" is unanswerable | VP CS / CSM · disputed alerts, QBR record, the scrubber; CFO · "data snapshots for auditing" | no | S | **High** — append-only, keyed by wizard run; the override audit needs a before-image |
| 5 | **Auditable backtest: persist runs + stamp `data_origin`** | G1 | the backtest is a CLI with no writes (`:31`); `measured` needs `data_origin` NULL and `--real` (`:211-212`); a load-driver tenant looks real to that gate | CFO / board · "median D days in K of N, F false alarms, on your history", replayable from a stored run | claim yes (L); code no (S) | S | **High** — the honesty gate is the claim |
| 6 | **Extraction-accuracy eval set** (precision / recall per role, per vertical) | G1 | none; signal-engine tests mock the LLM (`tests/test_signal_engine_v2.py:194`) | VP CS / IT · "the extractor is measured, not trusted"; the only defence against "your AI misread the email" | partly — start on demo manifests, credible with a design partner's comms | M | **High** — the domain eval Google calls methodology; prompt tuning is blocked on it (`evidence-spine-assessment.md` §3) |
| 7 | **Tool-call / route audit log** | G2 | no `ActivityLog` in this build (`id_generator.py:64` has only the prefix); `LLMUsageLog` and `WizardRun` exist; nothing records which key called which tool | IT · SOC2 "who did what"; support · cross-tenant investigation | no | S | **High** — one middleware over 7 tools + 8 routes; base for gaps 8 and 13 |
| 8 | **Centralised tenant isolation** + no query-string keys | G2 | per-tool gates (`mcp_server/auth.py:371,382,414`), 70 `filter_by(customer_id` sites; `server.py:52-60` accepts `?api_key=` — secrets in URLs reach Caddy logs | IT · "isolation is enforced in one place, tested once" | no | S–M | **High** — cheapest now, ruinous after real data lands |
| 9 | **Data-handling statement** ("never trained on") | G2 | no such doc; the truth is statable today: only `raw_text[:40000]` leaves the box, to the Anthropic API (`enrichment.py:309-319`), metered in `LLMUsageLog`; transcripts require `consent_verified` (`pipeline.py:88`) | IT · first question in every security review | no | S (a day) | **High** — most claim per hour of anything here |
| 10 | **First source adapter** (buyer-chosen) | G3 | `SOURCE_TYPES` are JSON shapes (`pipeline.py:40`) plus SendGrid / Slack webhooks; no Zendesk, Gong, Salesforce, HubSpot, Gainsight, ChurnZero | everyone · the only path to real data, which gates 5 and 6 | it *is* the data | M | **High for one**; Low for any built speculatively |
| 11 | RBAC / SSO / MFA | G2 | `auth_middleware.py` is a `NotImplementedError` placeholder; `User.role` (`models.py:67`) never read; no browser login exists — access is API keys with read / write / admin scopes and `allowed_account_ids` | IT · questionnaire; "CSMs see their accounts" is already answerable with account-scoped keys (`require_account_auth`) | no | M / M–L | Medium — triggered |
| 12 | Permission inheritance from source systems | G2 | nothing; the CS equivalent is CRM ownership / territory → `allowed_account_ids` | VP CS · only where CSMs must not see the whole book | needs the CRM adapter | M | Medium — triggered |
| 13 | Risk dashboard for IT | G2 | `/health` shows counts; the ingredients (LLM ledger, key list, review backlog, coverage P16-8) exist or are cheap | IT · per-tenant trust view | no | S–M after 7 | Medium — triggered |
| 14 | Approval tiers before agentic actions | G4 | `approval_queue.py` tiers on LLM-self-reported confidence (≥85 % auto-executes, `:6`); blueprint never mounted; no actuator exists, so nothing consequential happens today | VP CS · "no customer-facing action without a human" | no | M | Medium — triggered by the first actuator |
| 15 | Per-customer BYOK | G2 | one global `ANTHROPIC_API_KEY` (`enrichment.py:311`) | large-enterprise IT | no | S | Low — triggered |
| 16 | CMEK / dedicated tenancy | G2 | shared Postgres, per-row scoping | enterprise IT | no | L | Low |
| 17 | Partner-agent ecosystem | G3 | none | — | — | L | Not us |
| 18 | Ethical walls | G2 | none | — | — | — | Not applicable |

## 3. High-value items, in the recommended order

| Order | Item(s) | Why here |
|---|---|---|
| 1 | **Stamping bundle** — gaps 1, 2, 3 | Three or four days, one PR: `confidence` in the column, a `confidence_semantics` label (`llm_self_report_explicitness` / `rule_map_constant` / `stub_keyword`), `prompt_version`, `taxonomy_version` on every node; one `methodology` block per journey. Must precede the read surface freezing its JSON; the eval (6) needs the versions. |
| 2 | **Journey snapshots** — gap 4 | `journey_snapshots(run_id, account_id, as_of, journey_json, generator_version)`, append-only, written by `run_wizard_a`; `JourneyData` stays the head. Gives the override audit a before-image and the scrubber a real source. |
| 3 | **Auditable backtest** — gap 5 | Persist each run as a `WizardRun`-shaped row citing snapshot ids (so 2 first); stamp `data_origin` in `create_customer` (`synthetic_*` for demo / load-driver, the source system for adapters, NULL only on an explicit real flag). The claim waits for a tenant; the gate should not. |
| 4 | **Data-handling statement** — gap 9 | One page, true today. Before the next prospect conversation. |
| 5 | **Audit log + central isolation + no query-string keys** — gaps 7, 8 | Before the first real tenant's data lands, while the surface is seven tools: a middleware logging `(key_id, customer_id, tool, arg hash, outcome)`; one scoped-query helper every tool passes through; a test that enumerates registered tools and fails on any bypass. |
| 6 | **Extraction eval set** — gap 6 | After 1. Label 200–300 communications across two verticals (demo manifests first, a design partner's comms as they arrive); a harness shaped like `lead_time_backtest.py` with pre-registered per-role thresholds, reported per `prompt_version`. |
| 7 | **First adapter** — gap 10 | Whichever the first design partner has. Ticket / support (Zendesk, Pylon) serves signals-first; a Gainsight / ChurnZero export serves the backtest wedge (P3–P5). Build the seam (declared transforms in config) with the first source, not before. |

Items 1–4 are all S and all G1: they turn "the spine is honest" into something a buyer can inspect. Item 5 is the one G2 investment whose cost only rises. Items 6 and 7 convert claims into measurements, and 7 is where real data first enters — nothing else on this list substitutes for it.

## 4. Medium — do when a buyer triggers it

| Item | Trigger | Shape |
|---|---|---|
| RBAC + role readings (11, P11) | first UI, or a questionnaire asking for roles | read `User.role`; CFO / CRO / VP-CS / CSM default framings on the same journey |
| SSO / MFA (11) | a buyer's IT requires it — not before a browser login exists | OIDC only; never home-grown MFA |
| Permission inheritance (12) | a CRM adapter plus a buyer whose CSMs must not see all accounts | CRM owner / territory → `allowed_account_ids`, synced on ingest, labelled inherited |
| Risk dashboard (13) | first security review after gap 7 | a `get_tenant_trust` read tool: LLM calls and spend, text sent to the LLM (count, not content), keys and last use, review backlog, unclassified / unresolved-people rates, months without evidence |
| Approval tiers (14) | the first actuator — and not before the review write path, real auth and the audit log | mount `approval_queue` behind real auth; tier by **action class** (customer-facing message = always human; internal task = auto with audit), never by LLM confidence; approvals keyed to episode ids |
| Further adapters (10) | each named source, one buyer at a time | same seam as the first |
| BYOK (15) | a buyer asks | per-customer key in `CustomerConfig`, encrypted; `enrichment.py:311` reads it before the env |

## 5. Low / not for CS Pulse

| Item | Reason |
|---|---|
| Partner-agent ecosystem (17) | We are the agent over the buyer's evidence; the ecosystem play is Google's. Our openness is the MCP server itself. |
| CMEK / dedicated tenancy (16) | Nobody buying an early-warning layer for a CS team asks; the first enterprise that does will ask for SSO and a SOC2 report first. |
| Ethical walls (18) | A CS team wants every CSM to see the shared account context; the legal-matter model does not transfer. |
| Speculative adapters (10, beyond the first) | Each is an M with zero value until a buyer supplies credentials and data. |
| A second extraction model or prompt tuning | Blocked on gap 6. |

## 6. What this buys the pitch

After items 1–4, every number on the canvas can say what it is, when it was computed, by which extractor, and what it said last month — G1 in full, at S cost. After item 5 the IT reviewer gets a true data-handling page, an audit trail and one enforced isolation point. Items 6 and 7 are where the product stops being defended by design and starts being defended by measurement.
