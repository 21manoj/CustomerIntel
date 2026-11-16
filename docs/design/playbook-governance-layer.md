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

## 10. Shipped (2026-09-05) — what the build is, where it differs from the plan above

Built on `main`: `config/playbook_governance.json` (every number and enum), `config/playbooks/{saas_premium,dc2_s,datacenter_v1,healthcare_provider}.json`, `models.Intervention` (the one table), `playbooks/` (`definitions`, `governance`, `webhook`, `http`), six MCP tools (`get_playbooks`, `configure_playbooks`, `evaluate_playbooks`, `approve_intervention`, `report_intervention`, `list_interventions`) + the HTTP routes in §4, the `INTERVENTION` node as a journey episode of kind `intervention` (generator 3.4), tests in `backend/tests/test_playbook_governance.py` (15, with a local receiving endpoint that verifies the signature).

Deliberate differences from §2–§7, each for a reason:

- **`roles_match`** on a trigger: `any` (default) or `all`. `seat_truedown_save` uses `all` — commercial pressure *and* usage decline; on `any` it would fire on every budget remark.
- **dc2_s `thermal_escalation` floor is `high`, not `critical`.** The structural floor for `infra_incident` in `signal_engine.json` is `high`; a structured incident row never reaches `critical`, so the playbook would never fire on the CSV lane (the guard-never-fires class).
- **Two more tools** than the three-plus-one: `configure_playbooks` (webhook target + secret, switched-off playbooks, automation level, kill switch — the tenant overlay §2 allows had nowhere to be set) and `get_playbooks` (the read of the definitions and the overlay, secret masked). The overlay lives in `FeatureToggle(feature_name='playbooks')`; the secret is in that row, never in the repo, never returned.
- **Declining a proposal** is `report_intervention(state='cancelled')` on a proposed row (audited as `intervention.declined`); no separate reject tool.
- **The INTERVENTION node is written on every approval**, also when delivery failed or no endpoint is configured (`delivery.status` = `failed` | `not_configured`). The approval happened; the delivery problem is a finding the journey and the narrative say out loud.
- **Signature** covers `'<timestamp>.<body>'`, with the timestamp in `X-CI-Timestamp` — a captured payload cannot be replayed under a fresh header. Webhook URLs are `https` only; plain `http` needs `PLAYBOOK_WEBHOOK_ALLOW_HTTP=true` (tests, a local engine).
- **Evaluation hook**: `run_wizard_a(..., evaluate_playbooks=True)` runs the evaluation after it commits, so every rebuild point (signal processing, review, outcome, process_data, trigger_wizard, the stale-journey rebuild at deploy) proposes. `approve`/`report` rebuild with `evaluate_playbooks=False`; no re-entry. Off via `evaluate_on_journey_rebuild` in the governance config.
- **Audit rows** for propose / approve / send / started / done / failed / cancelled / declined / configure are written as `surface='playbook'`, `tool='intervention.<transition>'` with the caller's key kind and id (`system` for the rebuild hook and for policy approvals).
- **Counterfactual hooks** now compare outcomes at day granularity (an outcome is a decision *date*; an intervention is timestamped) — a same-day outcome counts as after.
- **Two numbers on the read**: `by_playbook[].realized_revenue` (linked outcomes, signed) and `exposure_revenue` (account revenue on the rows), with a note that says never to sum them.

Not built, per §9, and one more: the step stubs / simulated close / closure report the user asked for later (backlog).

Same day, two loose ends closed: the INTERVENTION node title is built by one helper, `playbooks.governance.intervention_title(label, account_name)` = `"<label> — <account>"` (the narrative cuts at the first ` — `, so a label with one is refused at load; the labels moved to colon form for that reason), and `scripts/repair_intervention_titles.py` (dry run by default, `--apply`, `--customer-id`) re-titles nodes written under an older label and rebuilds their journeys. `/health` counts interventions (`total`, `by_state`, `stuck`, `delivery_problems`) through `playbooks.governance.health_counts()`, the same `_stuck` / delivery definitions `list_interventions` uses.

## Fact triggers (added 2026-11-16)

A trigger can fire on recorded facts as well as on communications. This is a platform option for every vertical; the enums live in `config/playbook_governance.json` → `fact_triggers`.

- `trigger.kpi`: a list of `{code, op, value, periods?}`. Each reads the account's latest `periods` readings of that KPI in `kpi_measurements` (default 1) and holds when every one breaches. `kpi_match` is `any` (default) or `all`. Codes must be in the vertical's KPI catalog.
- `trigger.date`: `{field, within_days | overdue_by_days, label?}`. Reads a date in the account profile and compares it with the journey's `as_of`. `within_days` also fires for a date already past.
- `trigger.urgency`: the urgency a proposal carries when no communication is cited.
- `roles`, `kpi` and `date` are ANDed; a trigger needs at least one. `urgency_floor` needs `roles`.

A fact-triggered proposal cites the reading or date it fired on in `interventions.trigger_facts` (migration 0009), in the webhook payload (`trigger_facts`) and on the INTERVENTION node. The idempotency key includes the reading date or the date value, so the same fact never proposes twice and a new reading is new evidence.

## Steps and autonomy levels (added 2026-11-16)

Platform options for every vertical. Every rule and number is in `config/playbook_governance.json` → `autonomy`; a vertical adds only what is its own, in its playbook file.

**Steps.** A playbook may carry `steps`: 2 to `max_steps` entries of `{id, label, action_class, effects?}`. Each step is approved, sent and reported on its own. `report_intervention(state='done')` on a step that is not the last closes that step and starts the next; `done` on the last step, or any report with an outcome, closes the intervention (later steps are marked `skipped`). The row's state list is unchanged: a later step that waits for a person leaves the row `sent` with `awaiting_approval: true`. There is one INTERVENTION node per intervention; a later step updates its properties. The webhook payload carries `step {number, of, id, label, action_class}`.

**Levels.** `automation_level` is 0 to 4, per tenant, with an optional level per action class (`automation_levels_by_class`).

| Level | Policy approves |
|---|---|
| 0 | nothing; every approval is a person's |
| 1 | notify playbooks declared `approval: auto` |
| 2 | notify and schedule, once a person has approved that playbook's plan (remembered per playbook version) |
| 3 | notify and schedule on their own |
| 4 | escalate too; each is marked `review_required` and listed under `awaiting_review` until `review_intervention` |

The platform sets a ceiling (`ceiling`, and `ceiling_by_class`); a tenant's level is set at or below it. Each proposal records the decision and its reason in `approval_policy`.

**Always a person**, at every level: the `offer` action class; a playbook or step whose `effects` include `money`, `contract` or `exit`; an account that carries one of the vertical's `always_human.account_flags` (a top-level block in the vertical's playbook file, naming account profile fields).

**Lowering.** A declined proposal or step, or a `failed` report, lowers the level for that action class by `lower_by` (never below `lower_floor`) and withdraws that playbook's plan approval. It is written as a per-class level, to the tenant's `autonomy_log`, and to the audit log as `intervention.autonomy_lowered`. Nothing raises a level except `configure_playbooks`. `get_playbooks` returns `autonomy.suggested_raises`: per action class, a run of `suggest_raise_after` approvals by people with no decline or failure since. It is a suggestion only.

**Stop switch.** The existing `kill_switch`. While it is on, evaluation and every approval are refused, and a step reported done leaves the next step waiting (`stop_switch_on`).

Surfaces: `configure_playbooks(automation_levels_by_class=…)`, the new `review_intervention` tool, `POST /api/interventions/{id}/review` and `POST /app/api/interventions/{id}/review`. Columns: `interventions.steps`, `step_index`, `approval_policy`, `review_required`, `reviewed_at`, `reviewed_by` (migration 0010). Tests: `backend/tests/test_playbook_autonomy.py`.
