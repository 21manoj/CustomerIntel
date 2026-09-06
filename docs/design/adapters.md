# Adapters — the first receivers of the playbook webhook, and the first inbound source

*2026-09-05. Build note for `backend/adapters/`. Closes the gap the showcase run left open (`showcase/README.md`: both interventions ran with `delivery = not_configured` because nothing received the webhook) and ports rows 8–9 of `port-inventory.md` the way the rebuild rules ask — not the old `notifications_api` / `providers/` shape, but the smallest set of pieces that let a partner run the loop today.*

## 1. What this is for

The governance layer (`playbook-governance-layer.md` §5, §10) sends one signed payload per approval and waits for `report_intervention`. Three things did not exist: something that receives and answers, something a tenant can import into the workflow engine they already have, and a way in for the history a prospect exports from the incumbent tool (the Gainsight/ChurnZero backtest wedge in the conference notes). Each is an adapter *around* the contract; **the contract itself does not change** — same payload, same signature, same callback, same states.

## 2. Four pieces

| # | Piece | Where | What it does |
|---|---|---|---|
| 1 | Reference receiver | `adapters/receiver/` (`python -m adapters.receiver`) | Standalone Starlette app. Verifies `X-CI-Signature` = `sha256=HMAC(secret, '<X-CI-Timestamp>.<body>')` and the timestamp tolerance; 401 on a missing/bad signature or a stale timestamp. Idempotent per `intervention_id` (a replay is acknowledged `already_received`, never re-processed). Appends every event to a JSONL log. Calls the platform back: `started` right after acknowledging, then `done` after `auto_done_after_seconds` (policy `auto_done`) or never (policy `manual` — a person closes it through `report_intervention`). |
| 2 | n8n workflow | `adapters/n8n/intervention_webhook.workflow.json` + README | Importable workflow: Webhook → Code (HMAC verify, tolerance) → IF → Respond 200 / 401 → HTTP Request `started` → Wait → HTTP Request `done`. Same scheme, same callback. Not executed here (no n8n in this environment); the README says so. |
| 3 | Slack notify | `adapters/slack_notify.py`, hooked in `governance.approve` | Optional per-tenant `slack_webhook_url` in the playbook overlay. When set, an approved **`notify`-class** intervention also posts a minimal Slack message (account, playbook, quote, intervention id — no scores, no raw text). Result lands in `delivery.slack`; `delivery.status` and everything that reads it are untouched. The URL is a secret (Slack's incoming-webhook URLs are bearer-equivalent): stored per tenant, never returned, `slack_webhook_url_set` only. |
| 4 | Gainsight Timeline source | `adapters/sources/gainsight_timeline.py`, tool `import_from_source`, route `POST /api/sources/{source}/import` | Parses a Timeline activity export (CSV) into `import_communications` items and runs the communications lane. `source_ref = gainsight:timeline:<Activity ID>` is checked against existing signals **before** ingest, so a second import of the same file is reported as `already_imported` (`duplicates` stays what `import_communications` means by it: same text on the same account inside the dedup window), not a second copy. Unknown accounts and rejected rows come back in the result with their row numbers (the `import_communications` shape plus `parse`); `processed.still_pending` says how many of the signals this import queued no pass materialised. Journeys are rebuilt once, for every account touched. |

## 3. Decisions

- **Ack first, call back after.** `approve()` holds the row in `approved` until the receiver answers; a callback made inside the request would hit `only a sent one can start`. The receiver responds, then calls back after `callback.initial_delay_seconds` with `callback.retries` — the same guard a partner's engine needs, so it lives in the reference implementation and in the n8n README.
- **Idempotency is the receiver's, keyed on `intervention_id`.** The platform retries once; a partner may replay from a queue. The receiver's memory is the JSONL log (reloaded at start), so a restart does not re-run a delivered intervention.
- **Slack is a second entry, not a second channel.** Only `notify` (the class `auto` approval is allowed for). A failed Slack post is recorded in `delivery.slack.status = failed` and does not mark the webhook delivery failed — a partner reading `delivery_problem` sees the workflow engine's state, as before.
- **The Gainsight adapter is a declared transform in config, not a per-tenant column map.** Column aliases, activity-type → `source_type`, limits: all in `config/adapters.json` under `sources.gainsight_timeline`. Free-text notes go through the extraction path (no Gainsight field is a taxonomy subtype); the author becomes the accountability participant; unknown columns fold into `attributes` like the CSV uploads do. Gainsight's own scorecard/health are **not** read — the conference note keeps them as the backtest comparator, never our health.
- **No schema change.** The overlay field lives in `FeatureToggle('playbooks').config`; `source_ref` already exists on `qualitative_signals`. No Alembic revision.
- **Every number in `config/adapters.json`** (`adapters/settings.py` raises on a missing key, like `signal_engine/settings.py`). Secrets only via env / CLI: `CI_RECEIVER_SECRET`, `CI_PLATFORM_KEY`.
- **One http switch.** `playbooks.definitions.insecure_http_allowed()` (the env named by governance → `webhook.insecure_http_env`, i.e. `PLAYBOOK_WEBHOOK_ALLOW_HTTP`) is the only thing that lets a plain-http `webhook_url` or Slack URL through; the Slack validator calls it rather than re-reading the env.
- **One gate for the source import.** `adapters.sources.import_from_source` checks the customer exists and raises `ValueError`; the MCP tool maps that to `ToolError`, the route to 400. Neither surface carries its own copy of the check.

## 4. Running the receiver against a tenant

```
cd backend && CI_RECEIVER_SECRET=… CI_PLATFORM_URL=https://customerintelv1.3-218-251-181.sslip.io CI_PLATFORM_KEY=csp_… \
  .venv/bin/python -m adapters.receiver --port 8210 --customer-id 10 --log /var/log/ci_receiver.jsonl
configure_playbooks(10, webhook_url='https://<public host>/hook', webhook_secret='<same secret>')
```
The platform key needs **write** scope for that customer (it calls `report_intervention`); the server key works too. `--policy manual` leaves interventions at `started`. The receiver needs the `backend/` tree on its path (it reads the contract from `config/playbook_governance.json` and `playbooks/webhook.py`'s `verify`) and only `starlette`, `uvicorn`, `httpx` from the venv. Its `/health` and `/received` reads are unauthenticated — bind to localhost or front it with the tunnel / reverse proxy that exposes `/hook`. Nothing in this branch was pointed at the live box.

## 5. Tests (`tests/test_adapters.py`, real Postgres)

Receiver alone with a fake platform: good signature → `started` callback; replay → `already_received`, no second callback; bad/missing signature and stale timestamp → 401, nothing logged as received. Receiver against the real governance layer in-process (platform ASGI app and receiver both on uvicorn threads): `approve` → `delivered` → the callback moves the row to `sent` + `started_at`, the auto-done policy closes it `done`; a wrong secret → the receiver answers 401 and the platform records `delivery.status = failed`, `http_status = 401`, two attempts. Slack: masked in every read, second delivery entry only for `notify`, https/host rule. Gainsight sample end to end: parse report, journeys rebuilt once, nothing left pending, second import all `already_imported` by `source_ref`, unknown account / empty-notes / bad-date / in-file duplicate rows reported with row numbers, row cap and unknown customer refused on both surfaces. Tools and routes keyed.

## 6. Known gaps

n8n JSON validated by structure only (never imported into an n8n instance). Receiver idempotency is per process + log file (no shared store); its read endpoints are unauthenticated. `import_from_source` drains the tenant's whole pending queue when `process_now` is set (the lane's `process_pending` is per tenant, not per import), so `processed.processed` can exceed `queued` if other signals were waiting. No tenant column map for the Gainsight export (config aliases only). ChurnZero / CTA / scorecard lanes not built (backlog P-series). Slack posts are plain text; no threading, no per-playbook channel.
