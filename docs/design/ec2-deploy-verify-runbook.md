# CustomerIntel — EC2 deploy + verify runbook

*Prepared 2026-11-05. Run everything ON the EC2 box (SSH in). Repo: `~/CustomerIntel`. Env file: `deploy/.env` (git-ignored; never paste it).*

**Target:** the PR #19 branch `feat/tprm-v1-signals-only-vertical` (tip `1a47ede`) = `main` (`6063f70`) + 4 commits. It adds the `tprm_v1` vertical, the `get_evidence_graph` tool and 7 TPRM playbooks; **no schema migration** (DB stays at `0007_worker_heartbeats`), so rolling back to `main` is schema-safe. To deploy `main` instead, use `git checkout main` in step 2.

**Estimated time:** 20–30 min, mostly the image build and the test run (estimate; not measured on the box).

## What "good" looks like (baselines measured off-box on 2026-11-05: Python 3.11, Postgres 16, pinned deps)

| Step | Pass criterion |
|---|---|
| 3 Full tests | `903 passed, 21 skipped` on PR #19 (`887 passed, 21 skipped` on `main`). The 21 skips are static skip markers for code that was never ported — expected. **Any failure: stop.** |
| 4 Post-deploy | `/health`: `status ok`, `db true`, `git_sha 1a47ede`, worker `known true` and `stale false`. `schema_check`: "schema matches the models at head". |
| 5 TPRM smoke | 13 PASS lines then `SMOKE RESULT: ALL PASSED`. (On `main` it correctly prints 4 FAILs.) |

**Stop conditions:** `git status` not clean · step 0 shows anything other than `main` @ `6063f70` (stop and confirm first — the rollback below assumes it) · `git pull --ff-only` fails · backup fails · deploy exits non-zero · any test failure · any smoke FAIL. Stop, keep the log, and share it (step 8).

---

## 0. Look before you touch (read-only)

```bash
cd ~/CustomerIntel
git status --short            # expect NO output
git branch --show-current
git log -1 --format='%h %ad %s' --date=short
```

Expect `main` and `6063f70`. That is your rollback target: if it is anything else, stop and confirm before deploying.

```bash
awk -F= '/^[A-Z_]+=/{printf "%-26s %s\n", $1, ($2==""?"<EMPTY>":"<set>")}' deploy/.env
```

Expect `POSTGRES_PASSWORD`, `MCP_SERVER_API_KEY`, `SESSION_SECRET` = `<set>`. `ANTHROPIC_API_KEY` may be `<EMPTY>` (free-text extraction then uses a keyword stub; the smoke test uses structured signals, so it does not need it).

```bash
df -h / | tail -1
docker ps --format 'table {{.Names}}\t{{.Status}}'
curl -s http://127.0.0.1:8101/health | head -c 700; echo
```

Expect enough free disk for an image build, the `customerintelv1-*` containers up, and `cspulse-caddy` (shared Caddy) up. Note the `git_sha` in `/health`.

## 1. Rollback point (database backup)

```bash
bash deploy/backup_db.sh && ls -lh ~/backups/customerintelv1 | tail -3
```

Expect a new `customerintel_<timestamp>.dump` plus `.sha256` (more than a few KB). The deploy also backs up automatically; doing it first gives you a known file.

## 2. Switch branch and deploy

```bash
git fetch origin
git checkout feat/tprm-v1-signals-only-vertical
git pull --ff-only
git log -1 --format='%h %s'      # expect 1a47ede
```

```bash
bash deploy/deploy_ec2.sh --no-tests 2>&1 | tee ~/deploy-$(date +%F-%H%M).log
```

`--no-tests` is deliberate: the script's own test run pipes through `tail -3`, so failures would be nearly invisible. Step 3 runs the same suite with full output. The script still does: git pull → backup → build and start → migrate (no-op, already at head) → wait for `/health` → `schema_check` → worker check → Caddy route-coverage check → frontend build check → **rewrites the shared `~/caddy/Caddyfile` site block, validates, reloads** → rebuild stale journeys (none expected; generator version is `3.7` on both branches) → seed demo tenants (idempotent) → public check.

Last line to expect: `done: https://customerintelv1.3-218-251-181.sslip.io  (app)  .../health  (api, sha 1a47ede)`.
Non-fatal notices you may see: "no crontab" (Amazon Linux 2023 needs cronie for the daily backup) and "could not reach … from the box itself (hairpin NAT)".

## 3. Full test suite, full output

Define once per shell (re-run this line if you reconnect):

```bash
COMPOSE="docker compose -p customerintelv1 --env-file deploy/.env -f deploy/docker-compose.customerintelv1.yml"
```

```bash
$COMPOSE exec -T \
  -e DATABASE_URL="$($COMPOSE exec -T customerintelv1-app printenv TEST_DATABASE_URL)" \
  customerintelv1-app python -m pytest tests/ -q -p no:warnings -rfE --durations=10 \
  2>&1 | sed -E 's#(postgresql://[^:]+:)[^@]+@#\1***@#g' | tee ~/pytest-$(date +%F-%H%M).log | tail -40
```

This runs against `customerintel_test` (a separate database in the same Postgres), exactly as the deploy script does — the live `customerintel` database is not touched. The `sed` masks any DB password that a traceback might print. Takes ~2.5 min off-box; may be slower on the instance and loads the box while it runs.

## 4. Post-deploy checks

```bash
$COMPOSE ps
curl -s http://127.0.0.1:8101/health | head -c 900; echo
$COMPOSE exec -T customerintelv1-app python scripts/schema_check.py
```

Expect: postgres/app/worker/web `Up (healthy)`, `customerintelv1-migrate` `Exited (0)`; `/health` as in the table above; `revision: current=0007_worker_heartbeats head=0007_worker_heartbeats` and "schema matches the models at head". If the worker shows `known false` right after deploy, wait one poll interval (~15–60 s) and re-check; if it stays false or `stale true`, read `$COMPOSE logs --tail 50 customerintelv1-worker`.

```bash
curl -sS https://customerintelv1.3-218-251-181.sslip.io/health | head -c 300; echo
curl -sS https://customerintelv1.3-218-251-181.sslip.io/ | grep -c 'id="root"'
```

Expect the same health JSON through the public hostname and `1` (the SPA is being served).

## 5. TPRM smoke test (tested off-box against both branches)

Runs inside the app container (the image already has `fastmcp`). It creates **one throwaway tenant** (`data_origin=synthetic_test`, domain `smoke-<random>.tprm.test`), checks tool registration, auth, the TPRM flow and graph isolation, then deletes the tenant (the deletion stays in the audit log by design). It uses the container's own `MCP_SERVER_API_KEY`; nothing to paste.

```bash
$COMPOSE exec -T customerintelv1-app python - <<'PY'
"""CustomerIntel post-deploy smoke. Runs INSIDE the app container (fastmcp is in the image).
Reads MCP_SERVER_API_KEY from the container env; prints PASS/FAIL lines; exit 1 on any FAIL.
Writes ONE throwaway tenant (data_origin=synthetic_test) and deletes it at the end."""
import asyncio, json, os, sys, uuid
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

URL = os.environ.get('SMOKE_MCP_URL', 'http://127.0.0.1:8101/mcp')
SERVER_KEY = os.environ.get('MCP_SERVER_API_KEY', '')
fails = 0

def check(label, ok, detail='', why=None):
    global fails
    print(('PASS  ' if ok else 'FAIL  ') + label + (f'   [{detail}]' if detail else ''))
    if not ok and why is not None:
        print('      why: ' + json.dumps(why, default=str)[:300])
    fails += 0 if ok else 1

async def call(tool, args, key=None):
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    async with Client(StreamableHttpTransport(URL, headers=headers)) as c:
        r = await c.call_tool(tool, {k: v for k, v in args.items() if v is not None}, raise_on_error=False)
    data = getattr(r, 'data', None)
    if not isinstance(data, dict):
        sc = getattr(r, 'structured_content', None)
        data = sc.get('result', sc) if isinstance(sc, dict) else None
    if data is None:
        for b in (r.content or []):
            t = getattr(b, 'text', None)
            if t:
                try: data = json.loads(t)
                except Exception: data = {'text': t}
                break
    return bool(getattr(r, 'is_error', False)), (data or {})

async def main():
    async with Client(StreamableHttpTransport(URL)) as c:
        names = sorted(t.name for t in await c.list_tools())
    check('tools/list answers without a key', len(names) > 0, f'{len(names)} tools')
    check('get_evidence_graph is registered (needs PR #19)', 'get_evidence_graph' in names)

    err, body = await call('list_journeys', {'customer_id': 1})
    check('a keyed tool is refused without a key', err and 'requires an API key' in json.dumps(body), 'expected refusal')

    tag = uuid.uuid4().hex[:8]; domain = f'smoke-{tag}.tprm.test'
    err, c = await call('create_customer', dict(name=f'Smoke {tag}', domain=domain, vertical='tprm_v1',
            admin_email=f'smoke@{domain}', admin_name='Smoke', data_origin='synthetic_test'))
    cid, key = c.get('customer_id'), c.get('api_key')
    check('create_customer(vertical=tprm_v1) issues a tenant key', not err and cid and key, f'customer_id={cid}', why=c)
    if not (cid and key):
        print(f"\nSMOKE RESULT: {fails} FAILED (stopped early: no tenant to test with)")
        sys.exit(1)
    if SERVER_KEY:
        err, b = await call('list_journeys', dict(customer_id=cid), key=SERVER_KEY)
        check('a keyed tool works with the server key (Bearer)', not err, why=b)
    else:
        print('SKIP  server-key check: MCP_SERVER_API_KEY not set in this container')
    try:
        csv = 'source_account_id,account_name,industry,region,arr\nV-1,Smoke Vendor,Payments,US,0\n'
        err, up = await call('upload_csv', dict(customer_id=cid, file_type='account_details.csv', csv_content=csv), key=key)
        check('upload_csv accepts the vendor row', not err and up.get('status') in ('success', 'validation'), str(up.get('status')))
        err, _ = await call('process_data', dict(customer_id=cid), key=key)
        check('process_data runs', not err)
        items = [
            {'account_name': 'Smoke Vendor', 'source_type': 'manual', 'raw_text': 'breach disclosed', 'occurred_at': '2026-08-03T10:00:00Z', 'signal_type': 'vendor_data_breach'},
            {'account_name': 'Smoke Vendor', 'source_type': 'manual', 'raw_text': 'remediation done', 'occurred_at': '2026-09-02T10:00:00Z', 'signal_type': 'vendor_remediation_completed'},
        ]
        err, imp = await call('import_communications', dict(customer_id=cid, communications=items, process_now=True), key=key)
        check('import_communications queues both structured signals (no LLM)', not err and imp.get('queued') == 2, f"queued={imp.get('queued')}")
        err, j = await call('list_journeys', dict(customer_id=cid), key=key)
        acct = next((x for x in (j.get('journeys') or []) if x.get('account_name') == 'Smoke Vendor'), None)
        check('the vendor has a journey', bool(acct))
        aid = acct and acct.get('account_id')
        err, g = await call('get_evidence_graph', dict(customer_id=cid, account_id=aid), key=key)
        check('get_evidence_graph returns the 2 evidence nodes', not err and len(g.get('nodes', [])) == 2, f"nodes={len(g.get('nodes', []))} edges={len(g.get('edges', []))}")
        err, g0 = await call('get_evidence_graph', dict(customer_id=cid, account_id=999999), key=key)
        check('a bogus account_id returns an empty graph (no leak)', not err and not g0.get('nodes') and not g0.get('edges'))
        err, ev = await call('evaluate_playbooks', dict(customer_id=cid, dry_run=True), key=key)
        check('evaluate_playbooks sees the 7 TPRM playbooks', not err and ev.get('vertical') == 'tprm_v1' and len(ev.get('playbooks_considered', [])) == 7, f"{len(ev.get('playbooks_considered', []))} considered")
    finally:
        if SERVER_KEY:
            err, d = await call('delete_customer', dict(customer_id=cid, confirm_domain=domain, reason='ec2 smoke cleanup'), key=SERVER_KEY)
            check('throwaway tenant deleted', not err, f'customer_id={cid}')
        else:
            print(f'NOTE  no server key here: delete tenant {cid} ({domain}) by hand')
    print(f"\nSMOKE RESULT: {'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)

asyncio.run(main())
PY
```

Expect 13 `PASS` lines and `SMOKE RESULT: ALL PASSED` (exit code 0). A `FAIL` prints a `why:` line — capture it (step 8).

## 6. Optional: let a single-URL connector authenticate with `?api_key=`

Only if you want the claude.ai-style connector to work. Off by default because keys in URLs land in Caddy/uvicorn access logs and browser history. Verified off-box: with the flag on, `?api_key=` and `?token=` work and a wrong key is refused.

```bash
echo 'MCP_ALLOW_QUERY_KEY=true' >> deploy/.env
```

```bash
$COMPOSE up -d
```

Use a **tenant (customer) key** in the URL, not `MCP_SERVER_API_KEY` (that one is platform-wide). When the connector calls a tool the app logs "API key accepted from the query string" (`$COMPOSE logs --tail 20 customerintelv1-app | grep "query string"`). Any key that has been pasted into a chat or a URL should be treated as exposed — rotate it. To turn it off: delete that line from `deploy/.env` and run `$COMPOSE up -d` again.

## 7. Rollback

**Code only (preferred; PR #19 has no migration)** — this returns the box to `main`'s tip (`6063f70`). `deploy_ec2.sh` always runs `git pull --ff-only`, so it can only deploy the tip of a branch, never an older commit; that is why step 0 insists the box starts on `main` @ `6063f70`.

```bash
git checkout main && git pull --ff-only
```

```bash
bash deploy/deploy_ec2.sh --no-tests --no-seed
```

**Database (only if data was damaged — destructive, asks you to type RESTORE):**

```bash
ls -1t ~/backups/customerintelv1/customerintel_*.dump | head -3
```

```bash
bash deploy/restore_db.sh ~/backups/customerintelv1/customerintel_YYYYMMDDTHHMMSSZ.dump
```

Replace the file name with the dump you made in step 1.

The dump is at revision `0007`, same as the code, so no migration is needed afterwards. If you ever restore an *older* dump, run `$COMPOSE run --rm customerintelv1-migrate` before starting the app (the app no longer migrates at boot in this topology).

**Caddy:** each deploy saves `~/caddy/Caddyfile.bak` first and restores it automatically if the new file fails validation.

## 8. What to capture

Capture these when reporting a run (secrets are masked or never printed):

1. Step 0: branch + SHA before (expect `main` @ `6063f70`), and the `.env` key list (the `<set>/<EMPTY>` lines).
2. Step 2: the last ~30 lines of `~/deploy-*.log`.
3. Step 3: the last 40 lines (the `tee`'d log is `~/pytest-*.log`).
4. Step 4: `$COMPOSE ps` and the `/health` JSON.
5. Step 5: the full smoke output.

## Not verified off-box (what this runbook is for)

The Docker image build and compose wiring, the frontend build, the shared-Caddy rewrite on the real box, real LLM extraction (the smoke uses structured signals), and the signal-worker container's behaviour over time.
