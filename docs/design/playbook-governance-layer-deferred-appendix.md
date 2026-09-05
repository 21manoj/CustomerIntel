> **Status (2026-09-05): superseded as the build plan by `playbook-governance-layer.md` (the minimal version).** This document is kept as the appendix of deferred controls — tiers 2–3 and dual approval, dollar budgets, new key scopes, HMAC + retry schedule + dead-letter + polled queue, the events table, escalation chaining, three attribution strengths. Each returns when a tenant asks for it.

# Playbook governance layer — data + governance + integration, no engine

*2026-09-04. Design only; no code changed. Code-verified against `backend/` at `9366d01`. Companion to `governance-pass-platform-2026-09.md` (§2.8, §3), `evidence-spine-assessment.md` (§2 row 9, §3 "not the actuator before the review path, real auth, override audit"), `backlog-provisions.md`.*

**Decision this document implements:** the platform owns the *governance* of playbooks — what may fire, on what evidence, who approves, what left the box, what came back, what it changed. The *workflow* (Slack post, calendar hold, credit memo, CPQ amendment) runs in n8n / Salesforce Flow / Zapier / a partner system, reached by signed webhook or a polled queue. We never execute an action ourselves. That keeps the actuator risk outside the box and makes every action a *receipt* in the evidence graph rather than a side effect.

**The five rules it must satisfy** (same as the rest of the build): vertical content is data, not code · every claim cites evidence (episode ids + quotes) · leading/trailing separation is absolute (playbook lift is measured on both series, never a blend) · human verification before consequence · audit at chokepoints, numbers in config JSON.

---

## 1. Playbook definitions as data

`config/playbooks/*.json` are the shipped library, auto-discovered like catalogs and story arcs (`utils/vertical_registry.py`, `utils/story_arc_loader.py`). Each file declares `vertical` (`base` for vertical-agnostic) and is written against **taxonomy roles**, never subtypes — the same rule as arc rules and `urgency.structural_by_role`. Per-tenant overrides live in the `playbooks` table (§8): a tenant may disable a library playbook, tighten a threshold, raise an approval tier, or add a private playbook; a tenant override may **never lower** the approval tier or the budget below the policy floor in `config/playbook_governance.json`.

### 1.1 Trigger vocabulary

| Predicate | Reads | Notes |
|---|---|---|
| `roles_any` / `roles_all` | episode `role` on `kind='signal'` in the window | reviewed-rejected episodes never count (they are already excluded from the journey) |
| `min_effective_urgency` | evidence node `properties.effective_urgency` | role floor ⊕ perceived, from `signal_engine/urgency.py`; an unreviewed low-confidence node satisfies it only at `unreviewed_low_confidence_weight`, i.e. the rule may require `reviewed: true` |
| `early_warning_in` | `leading_vs_trailing.series[-1].early_warning` | `early_warning` / `recovery_watch` / `aligned` / `leading_only` |
| `arc_in`, `phase_in` | `journey.arc.arc_type`, `journey.current_phase` | `state='classified'` only; `steady`/`unclassified` never match an arc predicate |
| `min_exposure` | `Account.revenue` | exposure at trigger, the only dollar the rule sees |
| `renewal_within_days` | `features.days_to_renewal` | as-of relative, like everything in `journeys/` |
| `no_evidence_days` | `journey.last_evidence_at` vs `as_of` | the silence trigger — the one rule with no citation; it cites the *last* episode and states the gap |
| `window_days` | | episodes older than this are not trigger evidence |

### 1.2 Action classes and their policy (`config/playbook_governance.json`)

```json
{
  "version": "1.0",
  "action_classes": {
    "notify":   {"approval_tier": 0, "unit_cost": 0,     "rate_limit_per_account_per_30d": 4, "leaves_box": "internal_only"},
    "schedule": {"approval_tier": 1, "unit_cost": 150,   "rate_limit_per_account_per_30d": 2, "leaves_box": "customer_facing"},
    "escalate": {"approval_tier": 1, "unit_cost": 500,   "rate_limit_per_account_per_30d": 1, "leaves_box": "internal_only"},
    "offer":    {"approval_tier": 2, "unit_cost": null,  "rate_limit_per_account_per_30d": 1, "leaves_box": "customer_facing"},
    "credit":   {"approval_tier": 2, "unit_cost": null,  "rate_limit_per_account_per_30d": 1, "leaves_box": "financial"},
    "contract": {"approval_tier": 3, "unit_cost": null,  "rate_limit_per_account_per_30d": 1, "leaves_box": "financial"}
  },
  "approval_tiers": {
    "0": {"label": "auto",           "approvers_required": 0, "scope": null,      "allowed_when_automation_level_gte": 1},
    "1": {"label": "single",         "approvers_required": 1, "scope": "approve"},
    "2": {"label": "single_named",   "approvers_required": 1, "scope": "approve", "budget_enforced": true},
    "3": {"label": "dual",           "approvers_required": 2, "scope": "approve", "budget_enforced": true, "distinct_keys": true}
  },
  "candidate_ttl_days": 14,
  "default_suppression_days": 30,
  "measurement": {"before_after_days": 90, "outcome_window_days_default": 120},
  "dispatch": {"retry_schedule_seconds": [60, 300, 1800, 7200, 21600], "timestamp_tolerance_seconds": 300, "payload_version": "1"}
}
```

`unit_cost: null` means the cost must arrive in the receipt (`cost_actual`) — a credit's cost is the credit. Tier is a property of the **action class**, never of a model score (§3).

### 1.3 Four example playbooks

**dc2_s — thermal escalation** (`config/playbooks/dc2s_thermal_escalation.json`)

```json
{
  "playbook_id": "dc2s_thermal_escalation", "version": "1.0", "vertical": "dc2_s",
  "title": "Thermal incident → field-engineering escalation",
  "trigger": {
    "window_days": 21, "roles_any": ["infra_incident"], "subtypes_any": ["thermal_throttling_event", "power_event"],
    "min_effective_urgency": "high", "min_count": 2, "min_exposure": 500000
  },
  "action": {"class": "escalate", "target": "field_engineering_oncall",
             "template_hint": "thermal_rca_48h"},
  "suppression_days": 21,
  "expected_outcome": {"types": ["escalation_resolved", "uptime_restored"], "window_days": 45},
  "budget": {"per_account_per_quarter": 2}
}
```

**saas_premium — seat true-down save**

```json
{
  "playbook_id": "saas_seat_truedown_save", "version": "1.0", "vertical": "saas_premium",
  "title": "Seat under-utilisation + procurement pressure before renewal → value review with utilisation plan",
  "trigger": {
    "window_days": 60, "roles_all": ["usage_decline", "commercial_pressure"],
    "subtypes_any": ["seat_underutilization", "seat_reduction_request", "procurement_review"],
    "renewal_within_days": 120, "early_warning_in": ["early_warning", "leading_only"], "min_exposure": 100000
  },
  "action": {"class": "schedule", "target": "csm_calendar", "template_hint": "utilisation_review_exec"},
  "suppression_days": 45,
  "expected_outcome": {"types": ["renewal_secured", "revenue_protected", "contraction"], "window_days": 150},
  "escalation": {"if_no_receipt_days": 10, "then_action_class": "escalate"}
}
```

**base, signals-only — champion departure → sponsor rebuild** (no KPI predicates; works on the P1 tier)

```json
{
  "playbook_id": "champion_departure_sponsor_rebuild", "version": "1.0", "vertical": "base",
  "title": "Champion change with no replacement named → executive sponsor introduction",
  "trigger": {
    "window_days": 30, "roles_any": ["champion_change"], "reviewed": true,
    "absent_roles": ["advocacy", "expansion_intent"], "absent_stakeholder_subtypes": ["exec_sponsor"],
    "min_exposure": 250000
  },
  "action": {"class": "escalate", "target": "exec_sponsor_program", "template_hint": "cro_intro_request"},
  "suppression_days": 90,
  "expected_outcome": {"types": ["executive_engagement", "renewal_secured"], "window_days": 120},
  "follow_up": {"playbook_id": "sponsor_rebuild_qbr", "after_state": "completed"}
}
```

**base — expansion intent handoff**

```json
{
  "playbook_id": "expansion_intent_handoff", "version": "1.0", "vertical": "base",
  "title": "Expansion interest with healthy or recovering leading layer → hand to account executive",
  "trigger": {"window_days": 30, "roles_any": ["expansion_intent"], "early_warning_in": ["aligned", "recovery_watch", "leading_only"],
              "absent_roles": ["escalation", "announcement"]},
  "action": {"class": "notify", "target": "sales_handoff_channel"},
  "suppression_days": 30,
  "expected_outcome": {"types": ["expansion_approved", "expansion_closed"], "window_days": 180}
}
```

`expected_outcome.types` are validated at load against the tenant's `revenue_buckets` (`utils/taxonomy_loader.py`), exactly as `log_outcome` validates — a playbook naming an outcome the vocabulary lacks fails to load, loudly.

---

## 2. Trigger evaluation

**When.** `playbooks.evaluate(customer_id, account_ids)` runs at the end of the three places that already rebuild journeys, after `run_wizard_a` returns: `signal_engine/pipeline.process_pending` (`:410`), `journeys/outcomes.log_outcome` (`:125`), `signal_engine/review.review_signal` (`:139`), and `mcp_server/process_data_pipeline.run_wizard_a_step`. It is also callable on demand (`evaluate_playbooks`, dry-run by default). It never runs on a stale journey (`wizard_a.stale_journey_query`).

**What it reads.** Only `JourneyData.journey_json` (v3: episodes with roles/urgency/review, `leading_vs_trailing`, `arc`, `current_phase`, `features`, `last_evidence_at`) plus `Account.revenue` and the evidence nodes named by `evidence_node_ids` for quotes. It reads nothing the read surface (`journeys/read.py`) does not already expose — so a candidate is checkable by the same `get_journey` call a human uses.

**How a candidate cites.** A candidate carries `trigger_episode_ids` (sorted), `citations: [{episode_id, node_id, role, subtype, quote, occurred_at, effective_urgency, review}]`, and `rule_trace` — which predicates matched on what values (`{"roles_all": {"usage_decline": ["sig:4412"], "commercial_pressure": ["sig:4419"]}, "renewal_within_days": 87}`). A candidate with an empty citation list is not written — same rule as the narrative (`narrative.py` `CITATION_RULE`); the silence playbook cites the last episode and records `evidence_gap_days`.

**Idempotency.** `trigger_key = sha256(account_id | playbook_id | playbook_version | ",".join(trigger_episode_ids))`, unique on `playbook_executions`. New evidence changes the set and creates a new candidate; a rebuild that finds the same set does nothing.

**Suppression.** No new candidate for `(account, playbook)` while one is in a non-terminal state, or within `suppression_days` of the last `completed`/`dispatched` one (`suppressed_by_execution_id` recorded, count reported). Per-account rate limits per action class from policy (§7) apply across playbooks.

**Expiry.** A candidate not decided within `candidate_ttl_days` moves to `expired` with an event row; nothing silently lingers.

---

## 3. Approval

Tiers are keyed to the **action class** (§1.2) — the thing that determines blast radius — never to model confidence. Model confidence is already stamped on the evidence and shown in the citations; a reviewer sees it, the gate does not use it.

**What to keep from `approval_queue.py`:** the lifecycle columns (`decided_by`, `decided_at`, `decision_notes`, `expires_at`), the `pending / history / stats` read shapes, `action_payload` as JSON. **What to discard:** `AUTO_EXECUTE_THRESHOLD` / `REVIEW_THRESHOLD` and `auto_rejected` (confidence-keyed tiers — the exact anti-pattern), `_execute_action` (an event bus that does not exist in this build), `_apply_weight_calibration` (Wizard C, not in build), the Flask blueprint and `auth_middleware.get_current_customer_id` (a `NotImplementedError` stub; the server is Starlette/FastMCP), `agent_id`, `predicted_outcome`, `confidence`, `dollar_impact` as decision inputs, string `account_id`. Verdict: **delete the file**; the replacement is `playbooks/approvals.py` with the `approvals` table in §8.

**Who can approve.** A new key scope `approve` (add to `VALID_SCOPES` in `api_key_service.py`; `admin` implies it). Rules:

- Tier 1–3 approvals require `approve`; the decision row stores `key_id` (governance High #8 — identity from the validated key, `approver` label display-only).
- Tier 3 requires two approvals from **distinct** `key_id`s.
- A key with the `integrate` scope (§4/§5, the external engine's key) can never approve — separation of duties between the system that acts and the people who authorise.
- A key with `allowed_account_ids` may only approve executions on those accounts (`CustomerApiKey.has_account_access`).
- Tier 2–3 check the budget (§7) at approval time and again at dispatch; `budget_exhausted` is a rejection reason, recorded.

**Per-tenant `automation_level`** (`CustomerConfig.playbook_automation_level`, default `0`): `0` suggest-only — candidates are created and listed, nothing dispatches, not even notify; `1` notify auto-approves (tier 0), everything else waits; `2` reserved (schedule auto) — not built in MVP. **Kill switch:** `FeatureToggle(feature_name='playbook_dispatch')` per tenant plus env `PLAYBOOK_DISPATCH_ENABLED` for the box; when off, approved executions stay `approved` and are listed as `held`, never lost.

**Auto-approval audit.** A tier-0 auto-approval writes an `approvals` row with `key_id = NULL`, `decided_by = 'policy'`, `policy_version`, `automation_level` at the time, and the `playbook_executions` event `requested → approved (auto)`. It appears in `GET /api/playbooks/approvals?decided_by=policy` and is counted separately in per-playbook reporting.

---

## 4. Dispatch

**Primary: signed outbound webhook.** Per tenant, `webhook_endpoints` rows (url, `action_classes` it serves, secret). Delivery = POST with headers `X-CI-Event: playbook.execution.approved`, `X-CI-Delivery-Id` (uuid, idempotency key — the receiver must dedupe on it), `X-CI-Timestamp` (unix seconds), `X-CI-Signature: v1=hex(hmac_sha256(secret, timestamp + "." + body))`, `X-CI-Payload-Version: 1`. The receiver rejects a timestamp outside `timestamp_tolerance_seconds`. Retries on non-2xx / timeout follow `retry_schedule_seconds`, each attempt a `webhook_deliveries` row; after the last, state `dead_lettered`, surfaced in `get_interventions(state='dead_lettered')` and `/health` (`dead_letter_count`), with a `redeliver` action. Only `https://` targets; no redirects followed; response bodies truncated to 1 KB in the row.

**Payload contract v1** (minimum necessary — §7):

```json
{
  "payload_version": "1", "execution_id": "pbx_01J9...", "customer_id": 415,
  "account": {"account_id": 3684, "account_name": "Zenith Compute", "external_account_id": "SFDC-0019x", "exposure": 1200000},
  "playbook": {"playbook_id": "saas_seat_truedown_save", "version": "1.0", "title": "...",
               "action": {"class": "schedule", "target": "csm_calendar", "template_hint": "utilisation_review_exec"}},
  "citations": [{"episode_id": "sig:4412", "role": "usage_decline", "subtype": "seat_underutilization",
                 "quote": "Half the seats haven't logged in since March", "occurred_at": "2026-08-14T10:02:00", "effective_urgency": "high"}],
  "journey": {"arc_type": "silent_churn", "current_phase": "deterioration", "early_warning": "early_warning", "days_to_renewal": 87,
              "as_of": "2026-08-31T23:59:59", "journey_url": "/api/journeys/3684?customer_id=415"},
  "approval": {"tier": 1, "approved_at": "2026-09-04T15:11:09", "approver_key_ids": [27], "decision_note": "..."},
  "expected_outcome": {"types": ["renewal_secured", "revenue_protected", "contraction"], "window_days": 150},
  "callback": {"url": "/api/playbooks/report", "token": "cbk_...", "expires_at": "2027-03-03T00:00:00"}
}
```

No raw signal text beyond the cited quotes, no other accounts, no health-score history, no stakeholder emails (names only where the quote already contains them).

**Alternative: polled queue.** `GET /api/playbooks/queue?customer_id=&state=approved` with an `integrate`-scoped key returns the same payloads; `POST /api/playbooks/queue/{execution_id}/claim` leases it (`dispatched`, `claimed_by_key_id`, lease TTL from config). Engines behind a firewall use this; both paths converge on the same `dispatched` state and event row.

---

## 5. Receipts and the INTERVENTION node

**Inbound.** MCP `report_intervention` and `POST /api/playbooks/report`. Auth: an `integrate`-scoped key for the tenant **or** the per-execution callback token (hash stored; single execution; expires with the outcome window). Body: `{execution_id, state, occurred_at, external_ref?, note?, cost_actual?, outcome?: {outcome_type, occurred_at, revenue?, note?}}`.

**State machine** (`playbook_executions.state`; every transition is an event row with `key_id`/`token`, actor label, reason):

```
requested ─► approved ─► dispatched ─► started ─► completed
    │            │            │            │          
    ├► rejected  ├► held      ├► dead_lettered        └► failed
    ├► expired   └► cancelled └► cancelled     (any non-terminal) ─► cancelled
    └► suppressed
```

Illegal transitions are refused with the current state in the error; out-of-order receipts (`completed` before `started`) are accepted only when `occurred_at` ordering is consistent, and both events are recorded.

**Recommendation: a new node type `INTERVENTION`, not `DECISION`.** Reasons: (1) `DECISION` is customer-side or CSV-asserted choice with `chosen_option` (`journey_builder.py:107-113`; old-repo subtype comment `playbook|escalation|exec_engagement`), and overloading it forces every consumer to inspect properties to tell a receipt from an imported decision; (2) `SIGNAL` with role `intervention` exists for CSV history, but SIGNAL is *what the customer said or did* — a vendor action written as SIGNAL breaks that meaning and would be one filter slip away from the leading composite (`leading_series` filters on `kind == 'signal'`; a new kind is excluded by construction, not by a role list); (3) the node must exist at `started` and be updated through `completed`/`failed` — a lifecycle object, which neither existing type is; (4) Ask AI and `get_evidence` need to answer "what did *we* do" as a distinct class.

**Node rules.** Written at the first `started` receipt (not at dispatch — nothing has happened in the customer's world yet): `node_type='INTERVENTION'`, `node_subtype=<action class>`, `source='observed'`, `source_platform='playbook_governance'`, `source_event_id=f'intervention:{execution_id}'`, `occurred_at=started_at`, `confidence=1.0` (a receipt, `confidence_semantics='receipt'`), `properties={playbook_id, version, state, execution_id, approval_ids, citations, external_ref, cost_actual, completed_at|failed_at}`. `failed`/`cancelled` before `started` writes **no node**; a `failed` after `started` updates the node's state — the attempt is a fact, its failure is too. Edges: from each cited evidence node `TRIGGERED` → INTERVENTION (`derivation='playbook_rule'`, `confidence=None` per the WS-2 rule — a rule match is not a calibrated estimate); INTERVENTION `LED_TO` → OUTCOME for outcomes logged through the receipt (`derivation='reported_in_receipt'`) or linked by `log_outcome(linked_signal_ids=['intervention:pbx_…'])` (extend `_find_signal_nodes` to resolve the `intervention:` prefix; `derivation='human_linked'`). Window-only attribution (§6) writes no edge.

**Journey changes** (`journeys/journey_builder.py`): `collect_episodes` reads `INTERVENTION` into a new episode `kind='intervention'` (`episode_id='int:{node_id}'`, `meta={playbook_id, action_class, state, execution_id, citations}`); `detect_phases` adds `kind == 'intervention'` to its skip list (a response, never a trigger — the comment at `:246` already says so); `counterfactual_hooks` includes `kind == 'intervention'` with `state in ('started','completed')`; `narrative._phrase` gains the kind and `_t_intervention` (template `intervention_before_after`) already renders from the hook; `read.get_evidence` adds `INTERVENTION` to the node-type filter with a `what_we_did` flag. Wizard B's `interventions()` reads hooks unchanged and gains `playbook_id` per row. Bump `GENERATOR_VERSION`.

---

## 6. Measurement

Per execution, from the hook: `health_before` / `health_after` (trailing, `kpi_only`) **and** `qual_before` / `qual_after` (leading) — two lifts, reported side by side, never combined; `lift_pts` on each; outcomes in `expected_outcome.window_days` on the same account. Cost = `cost_actual` from the receipt, else `unit_cost` from policy; recorded per execution with its source.

Attribution has three labelled strengths, never merged: **linked** (an edge exists — receipt or human), **windowed** (outcome of an expected type within the window, no edge; `attribution='window'`), **none**. Dollars are reported as two numbers on every row and rollup: `realized_$` = signed `revenue_impact` of *linked* outcomes (protected/expansion positive, lost/at_risk negative, as `log_outcome` stores them) and `exposure_$` = `Account.revenue` at trigger — the narrative number. They are never summed; the rollup prints `realized_$ / exposure_$` as a pair with `n_linked / n_windowed / n_none`.

Per playbook (`GET /api/playbooks/{id}/report`, `WizardRun` row when run through Wizard B): `n_triggered, n_suppressed, n_approved (human / auto), n_rejected, n_expired, n_dispatched, n_dead_lettered, n_started, n_completed, n_failed, median_days_request_to_start, median_trailing_lift, median_leading_lift, share_with_leading_lift ≥ INTERVENTION_LIFT_PTS, outcomes_by_bucket, cost_total, realized_$, exposure_$`, with the same `basis` sentence Wizard B prints (a comparison on this tenant's data, not a causal estimate). Feeds: Wizard B Hindsight's `interventions()` gains a playbook dimension for free; Po1/ROI, when ported, reads `cost_total` and `realized_$` per playbook — the two inputs it lacks today — and must still refuse to divide until a tenant has ≥ N linked outcomes (N in config, pre-registered like `PREREGISTERED` in `evals/lead_time_backtest.py`).

---

## 7. Governance controls checklist

| Control | Mechanism |
|---|---|
| Audit every state change | `playbook_execution_events` append-only: `execution_id, from_state, to_state, at, actor_kind (key/token/policy/system), key_id, key_prefix, caller_ip, reason, detail_json`; plus the existing `ToolAuditLog` row at the chokepoint (`mcp_server/audit.py`) |
| Identity | approvals and receipts store `key_id` from the validated key (never a caller-supplied name); token receipts store the execution's token hash id |
| Rate limits | per account per action class per 30 d from policy; per tenant per day (`max_dispatches_per_day` in policy); evaluation is idempotent so a rebuild storm cannot multiply candidates |
| Double-fire | `trigger_key` unique; suppression window; single non-terminal execution per (account, playbook); `X-CI-Delivery-Id` for the receiver |
| Replay protection | signed timestamp with tolerance; delivery id; callback token bound to one execution and single-purpose; receipts idempotent on `(execution_id, state, occurred_at)` |
| Secrets | endpoint secrets encrypted at rest with a server key (`WEBHOOK_SECRET_KEY` env; Fernet — new dependency), shown once at creation like API keys, rotatable with a 24 h dual-secret window; never in logs or audit rows |
| Tenant isolation | every table carries `customer_id`; endpoints belong to a tenant; a key's tenant must equal the execution's; `allowed_account_ids` honoured at approve and report |
| Data leaving the box | payload v1 only (§4): cited quotes, account name/external id, exposure, arc/phase/urgency, journey URL — no raw text, no other accounts, no KPI history; listed in the data-handling statement (High #9) |
| Kill switch | tenant `FeatureToggle('playbook_dispatch')`, box `PLAYBOOK_DISPATCH_ENABLED`, per-endpoint `is_active`; `automation_level=0` is the default and the safe state |
| Budget | `budget.per_account_per_quarter` count and `cost_cap_per_quarter` per tenant in policy; checked at approve and dispatch; exhaustion is a recorded rejection |
| No fabrication | an execution with no citations is not created; a hook is not created before `started`; window attribution is labelled, never an edge |

**What a SOC2 reviewer will ask** (and where the answer is): who approved this action and with what credential (`approvals.key_id`); what data was sent where (`webhook_deliveries.payload_sha256`, endpoint url, headers minus signature); can anyone bypass approval (`automation_level`, tier 0 only, policy version stamped); how are secrets stored and rotated; what happens on failure (dead-letter, no silent retry forever); can the acting system approve its own work (`integrate` cannot approve); how is an action retracted (`cancelled` with reason, receiver notified by `playbook.execution.cancelled`); retention (execution and event rows are tier-1 permanent, like `SignalReview`).

---

## 8. Data model, tools, routes

**Tables** (all with `customer_id` FK + index, `created_at`; JSON columns for shapes that mirror config):

| Table | Columns |
|---|---|
| `playbooks` (tenant overrides) | `id, customer_id, playbook_id, base_version, enabled, override_json, policy_floor_checked_at, updated_by_key_id, updated_at` |
| `playbook_executions` | `id (pbx_ uuid), customer_id, account_id, playbook_id, playbook_version, policy_version, action_class, approval_tier, state, trigger_key (unique), trigger_episode_ids JSON, citations JSON, rule_trace JSON, journey_as_of, exposure_at_trigger, expires_at, suppressed_by_execution_id, endpoint_id, claimed_by_key_id, dispatched_at, started_at, completed_at, failed_at, external_ref, cost_actual, cost_source, intervention_node_id, callback_token_hash, callback_expires_at, outcome_window_ends_at` |
| `playbook_execution_events` | `id, execution_id, customer_id, from_state, to_state, at, actor_kind, key_id, key_prefix, caller_ip, reason, detail JSON` |
| `approvals` | `id, execution_id, customer_id, tier, decision (approve/reject), key_id (NULL for policy), decided_by_label, policy_version, automation_level, budget_check JSON, note, decided_at` |
| `webhook_endpoints` | `id, customer_id, name, url, action_classes JSON, secret_encrypted, secret_prev_encrypted, secret_rotated_at, is_active, created_by_key_id, last_success_at, last_failure_at` |
| `webhook_deliveries` | `id, execution_id, endpoint_id, customer_id, delivery_id (uuid), attempt, event, payload_sha256, payload_version, sent_at, status_code, response_excerpt, error, next_attempt_at, dead_lettered` |

Model additions: `CustomerConfig.playbook_automation_level` (int, default 0); `CustomerApiKey.scopes` accepts `approve`, `integrate`; new node type `INTERVENTION` and edge type `TRIGGERED` documented on `ContextNode`/`ContextEdge`.

**MCP tools** (all `KEYED_TOOLS`; scope in brackets): `list_playbooks(customer_id)` [read] — library + overrides + per-playbook counts; `evaluate_playbooks(customer_id, account_id=None, dry_run=True)` [write] — candidates with citations, writes only when `dry_run=False`; `get_interventions(customer_id, account_id=None, state=None, playbook_id=None)` [read]; `approve_action(customer_id, execution_id, decision, note=None)` [approve]; `report_intervention(customer_id, execution_id, state, occurred_at, external_ref=None, note=None, cost_actual=None, outcome=None)` [integrate or token]; `configure_playbook(customer_id, playbook_id, enabled=None, override=None)` [admin]; `configure_webhook_endpoint(customer_id, ...)` [admin].

**HTTP** (`playbooks/http.py`, registered in `server.py` beside the journey routes, `_authorize` pattern from `signal_engine/http.py` extended with the two scopes): `GET /api/playbooks`, `POST /api/playbooks/evaluate`, `GET /api/playbooks/executions`, `GET /api/playbooks/executions/{id}` (execution + events + deliveries + node), `POST /api/playbooks/executions/{id}/approve`, `POST /api/playbooks/executions/{id}/cancel`, `POST /api/playbooks/report`, `GET /api/playbooks/queue`, `POST /api/playbooks/queue/{id}/claim`, `POST /api/playbooks/executions/{id}/redeliver`, `GET /api/playbooks/{playbook_id}/report`, `GET|POST /api/playbooks/endpoints`, `POST /api/playbooks/endpoints/{id}/rotate`.

**Read surface changes.** `get_journey` gains `interventions: [{execution_id, playbook_id, state, started_at, hook}]`; `list_journeys` gains `open_executions` (pending approvals count); Ask AI's contract (`ask_ai/answer.py`) may cite `int:` episodes like any other.

---

## 9. Phasing

**MVP — the lifecycle trace end to end (S+M, ~2 weeks).** Policy + library loader with validation against taxonomy buckets and policy floors; `playbook_executions` + events + `approvals`; `evaluate` wired after the three rebuild points, dry-run tool; `approve_action` with the `approve` scope and `key_id` binding; tier 0 auto only at `automation_level=1`; one dispatch path — signed webhook with retry/dead-letter — and the polled queue (cheap, same rows); `report_intervention` (key + token); INTERVENTION node + `TRIGGERED`/`LED_TO` edges; journey kind `intervention`, hooks, narrative template, `get_evidence` flag; per-playbook report with the two-dollar rule; tests: a fixture tenant runs `signal → candidate → approve → deliver (mock receiver verifies HMAC) → started → completed → outcome → journey shows intervention_before_after → report counts`. Also a contract test that no action class in policy has tier < its floor and that every `expected_outcome.type` in the library resolves in every vertical it ships for. Ship with two library playbooks per vertical, `automation_level=0` everywhere.

**Phase 2.** Tier 3 dual approval and budget caps in dollars; `escalation.if_no_receipt_days` and `follow_up` chaining; endpoint secret rotation UI; `playbook.execution.cancelled` outbound event; Wizard B playbook dimension and Po1 inputs; `held` visibility on `/health`; per-role default approvers once RBAC (P11) exists; a second receiver adapter (Salesforce Flow) proving the payload contract.

**Deliberately not built.** An execution engine, templates, or connectors (n8n/Zapier own those); LLM-generated action text (the payload carries citations and a `template_hint`, the workflow renders); confidence-keyed autonomy; auto-approval above tier 0; window attribution as an edge; a dollar ROI before `N` linked outcomes; cross-tenant playbook learning (CDI) before real tenants exist.

**Dependencies on existing work and the governance High list.** High #1 (anonymous-tool hole) — shipped `90b6880`, prerequisite. High #8 (bind decisions to `key_id`) — this design applies it to approvals and receipts; extend `SignalReview` at the same time. High #5 (journey snapshots) — an execution stores `journey_as_of` and the cited node ids today; once snapshots exist, store `snapshot_id` so "what did the journey say when this fired" is replayable. High #7 (budget enforcement) — same enforcement pattern, different budget; share `can_call`-style gating. High #9 (data-handling statement) — must list payload v1. Existing pieces reused unchanged: `journeys/outcomes.log_outcome` (receipt outcomes go through it), `signal_engine/urgency.py` floors, `utils/taxonomy_loader` vocabulary checks, `mcp_server/audit.py`, `api_key_service.py`, `signal_engine/http._authorize`, `FeatureToggle`, `WizardRun`.
