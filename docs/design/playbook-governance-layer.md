# Playbook governance layer — the minimal version

*2026-09-05. Build plan for the first port. Decided: the platform owns the **governance** of playbooks; the workflows themselves run outside it (n8n, Salesforce Flow, Zapier, a partner's system) and are reached by webhook. The fuller design is kept as `playbook-governance-layer-deferred-appendix.md`; nothing in it is lost, and nothing in it is built until a tenant asks.*

## 1. The gap this closes

The lifecycle trace (`lifecycle-trace-sample.md`) runs create → uploads → process_data → signals → review → outcome → journey → Ask AI with every row cited. Between "the evidence says act" and "an outcome happened" there is no record: an intervention is only a signal with the `intervention` role. This layer adds that record and nothing more: **what was proposed on which evidence, who approved it, that it was sent, and what came back.**

Rules it keeps: everything vertical-specific is data; every proposal cites episode ids; a human approves anything beyond a notification; every state change is in the audit log; no bare numbers in code.

## 2. Playbook definitions — one file per vertical, four fields per playbook

`config/playbooks/<vertical>.json` (base) — a tenant may override the webhook target and switch playbooks off, nothing else in v1.

```json
{
  "version": "1.0",
  "vertical": "saas_premium",
  "playbooks": [
    {
      "id": "seat_truedown_save",
      "trigger": {"roles": ["commercial_pressure", "usage_decline"], "urgency_floor": "high", "renewal_within_days": 120},
      "action_class": "schedule",
      "approval": "human",
      "expected_outcome": {"types": ["renewal_secured", "revenue_protected", "contraction"], "window_days": 90}
    },
    {
      "id": "champion_departure_sponsor_rebuild",
      "trigger": {"roles": ["champion_change"], "urgency_floor": "critical"},
      "action_class": "escalate",
      "approval": "human",
      "expected_outcome": {"types": ["renewal_secured", "churn_lost"], "window_days": 120}
    },
    {
      "id": "expansion_intent_handoff",
      "trigger": {"roles": ["expansion_intent"], "urgency_floor": "medium"},
      "action_class": "notify",
      "approval": "auto",
      "expected_outcome": {"types": ["expansion_closed", "expansion_opportunity"], "window_days": 90}
    }
  ]
}
```

dc2_s adds `thermal_escalation` (`infra_incident`, critical, escalate, human). Validation at load: roles must exist in the taxonomy, outcome types must be in the tenant's revenue buckets (the same check `log_outcome` makes), `action_class` ∈ {notify, schedule, escalate, offer}, `approval` ∈ {auto, human} and `auto` is allowed only for `notify`.

## 3. One table

`interventions`: id · customer_id · account_id · playbook_id · playbook_version · state · trigger_episode_ids (JSON) · trigger_quote (the first cited quote) · proposed_at · approved_at · approved_by_key_id · sent_at · delivery (JSON: url host, http status, attempt count, error) · closed_at · closed_state (done | failed | cancelled) · outcome_node_id · node_id (the INTERVENTION node) · notes.

States: **proposed → approved → sent → closed**. Who and when for every transition are also rows in the existing `tool_audit_log` (they already carry the key). No events table, no approvals table.

Idempotency: one row per (account, playbook_id, sorted trigger_episode_ids). A trigger that already has a row is never proposed again. Suppression: no second proposal for the same (account, playbook) while one is open or within `window_days` of one that closed.

## 4. Three tools (+ one read)

- `evaluate_playbooks(customer_id, account_id=None, dry_run=False)` — runs where journeys already rebuild (after signal processing, review, outcome logging, process_data). Reads the journey's latest leading month (roles, urgency floors on the cited nodes), the renewal date, the account's revenue. Writes `proposed` rows with citations. `dry_run` returns what it would propose.
- `approve_intervention(customer_id, intervention_id, note=None)` — keyed (write scope or server key); `auto` playbooks call it as `decided_by='policy'` at evaluate time. On approval the platform **sends** the payload (below), writes the INTERVENTION node, and moves to `sent`. A failed send is `sent` with `delivery.error` set and visible in `list_interventions`, retried once; no queue.
- `report_intervention(customer_id, intervention_id, state, note=None, outcome_type=None, outcome_date=None, revenue=None)` — what the external workflow calls back with: `started` (informational), `done`, `failed`, `cancelled`. An outcome, if given, goes through `log_outcome` and is linked to the node.
- `list_interventions(customer_id, account_id=None, state=None)` — the read; also stuck ones (sent, no report within N days from config).

HTTP: `POST /api/interventions/{id}/approve`, `POST /api/interventions/{id}/report`, `GET /api/interventions`.

## 5. The webhook

One JSON payload per approval, to the tenant's configured URL, signed with the tenant's shared secret (`X-CI-Signature: sha256=<hmac of body>`, `X-CI-Timestamp`). Minimum necessary: intervention id, playbook id and action class, account id and name, the trigger quotes with their episode ids, the approver, and the callback intervention id. No raw communication text, no roster, no scores. One retry after a short delay; then the row shows the error. The workflow engine reports back with `report_intervention` using its own key, as it can already call `submit_signal` and `log_outcome`.

## 6. The node and the measurement

An `INTERVENTION` node (new node_type; `observed`; `source_platform='playbook'`; `source_event_id=intervention:<id>`; properties: playbook_id, action_class, approved_by, trigger_episode_ids, delivery status) is written **when the payload is sent**, not when the engine says it started — an engine that never calls back is itself a finding and must be visible on the journey. Edges: from each trigger evidence node (`LED_TO`), and to the outcome node when one is reported.

The journey builder reads it as an episode of kind `intervention`; `counterfactual_hooks` and the narrative's `intervention_before_after` template work unchanged; Wizard B's intervention before/after already reads those hooks. Per-playbook reporting is a query over the table: proposed, approved, sent, closed done/failed, outcomes within window, realized $ (from linked outcomes) and exposure $ (account revenue) as two numbers, never summed.

## 7. Governance that ships with v1

Human approval for everything except `notify`; per-tenant `automation_level` (0 = everything human, 1 = notify auto) and a kill switch that stops evaluate and send; one open proposal per (account, playbook); audit rows for propose/approve/send/report with key ids; the payload carries citations and nothing that is not needed; secrets stored per tenant, never in the repo; the read surface shows stuck interventions.

## 8. Size and order

About a week: config + loader + validation (S), table + node + tools (M), webhook send + signature + one retry (S), evaluate wired to the rebuild points (S), narrative/read surface (S), tests including a fake receiving endpoint. Depends on nothing new; binds approvals to `key_id` as governance High #8 asks.

## 9. Deferred (see the appendix)

Approval tiers 2–3 and dual approval · dollar budgets per action class · `approve` / `integrate` key scopes · dead-letter queue and polled-queue alternative · events table · escalation and follow-up chaining · three-strength attribution · LLM-rendered action text · endpoint secret rotation UI.
