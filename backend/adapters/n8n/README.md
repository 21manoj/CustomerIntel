# n8n workflow — receiving the intervention webhook

`intervention_webhook.workflow.json` is an importable n8n workflow that does what `adapters/receiver` does: verify the platform's signature, acknowledge, report `started`, do the work, report `done`.

**Not executed here.** There is no n8n in the build environment; the JSON was written against n8n's export format (nodes / connections / settings, `n8n-nodes-base.*` types, current type versions) and checked by structure only. Import it, open each node once, and run one test execution before pointing a tenant at it.

## Flow

```
Webhook (POST /webhook/customerintel/intervention, raw body)
  → Code "Verify signature"      sha256=HMAC_SHA256(secret, '<X-CI-Timestamp>.<raw body>'), timing-safe; timestamp within tolerance
  → IF verified
      true  → Respond 200 {status: accepted}  → Wait 1 s → HTTP report_intervention `started` (4 tries)
                                              → Wait 60 s ("do the work": replace with your own steps)
                                              → HTTP report_intervention `done`
      false → Respond 401 {error: missing_signature | bad_timestamp | stale_timestamp | bad_signature | bad_payload}
```

The 1 s wait before `started` is deliberate: the platform holds the intervention in `approved` until your 200 comes back, then commits `sent`; a callback inside the request would be refused with "only a sent one can start". The `started` request retries on a non-2xx for the same reason.

The workflow is not idempotent by itself — if the platform retries a delivery (it does, once, on a non-2xx) or your queue replays, n8n runs twice; the platform refuses the second `started`/`done` on a closed row, so nothing is double-counted on the record, but your own steps would run again. Add a Data Store / Redis check on `intervention_id` before "Do the work" if that matters for your steps.

## Import steps

1. n8n → Workflows → Import from File → this JSON.
2. **Environment variables** on the n8n host (the Code node reads them; both flags are needed):
   - `CI_WEBHOOK_SECRET` — the tenant's shared secret (the same value given to `configure_playbooks(webhook_secret=…)`).
   - `CI_PLATFORM_URL` — e.g. `https://customerintelv1.3-218-251-181.sslip.io` (no trailing slash).
   - `CI_TIMESTAMP_TOLERANCE_SECONDS` — optional, default 300.
   - `NODE_FUNCTION_ALLOW_BUILTIN=crypto` and `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` — the Code node uses Node's `crypto` and `$env`.
3. **Credential**: create a *Header Auth* credential named `CustomerIntelV1 platform key` with name `Authorization` and value `Bearer <key>`, where the key has **write** scope for the tenant (or is the server key). Open both HTTP Request nodes and select it (the JSON carries a placeholder credential id).
4. Activate the workflow; the production URL is `https://<your n8n>/webhook/customerintel/intervention`.
5. On the platform: `configure_playbooks(customer_id, webhook_url='https://<your n8n>/webhook/customerintel/intervention', webhook_secret='<the same secret>')`.
6. Test: `approve_intervention` on a proposed row; `list_interventions` should show `delivery.status = delivered`, then `started_at`, then `closed_state = done` after the wait.

## What the payload carries

See `backend/playbooks/webhook.py build_payload`: intervention id, customer id, playbook (id, version, action class), account (id, name, external id), urgency, the cited trigger quotes with their episode/node ids, approver, expected outcome, the callback route, and the data-origin disclosure. No raw communication text, no roster, no scores. To report an outcome with `done`, add `outcome_type`, `outcome_date`, `revenue` to the last node's JSON body (`report_intervention` logs it through the outcome lane).
