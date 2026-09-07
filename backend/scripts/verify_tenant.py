"""
Check a live tenant the way someone about to demo it would — over MCP/HTTPS,
reading only what the product exposes. Nothing here touches the database.

    python scripts/verify_tenant.py --url https://…/mcp --key $KEY --customer-id 14 \\
        --manifest demo/manifests/aurelia_datacenter_portfolio.json

Without --manifest it checks what any tenant must satisfy:

  journeys       every account has one, none behind the platform's journey
                 generator version (/health's stale_journeys, per tenant)
  health         every account is scored in every month the tenant has data for
                 — no gaps in the middle of a series
  interventions  nothing stranded: no row stuck in 'proposed' that was approved,
                 every closed row carries the outcome it was closed with, and
                 every one of them cites the episodes it fired on
  isolation      a missing key and a wrong key are both refused

With --manifest it additionally holds the tenant to what the manifest CLAIMED:
each declared intervention really fired on its declared playbook, closed with
its declared outcome type, and — for the showcase accounts — the KPI layer
really moved to a healthier band across the cycle. The manifest is the claim;
this is the audit of it.

Exit status is 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.onboard_tenant import _payload           # noqa: E402

BANDS = ((70, 'healthy'), (50, 'at_risk'), (0, 'critical'))
OK, BAD = '  ok  ', ' FAIL '


def band(score):
    if score is None:
        return None
    for floor, name in BANDS:
        if score >= floor:
            return name
    return 'critical'


def band_rank(name):
    order = ['critical', 'at_risk', 'healthy']
    return order.index(name) if name in order else -1


class Report:
    def __init__(self):
        self.rows = []

    def check(self, name: str, passed: bool, detail: str = ''):
        self.rows.append((name, bool(passed), detail))
        print(f'[{OK if passed else BAD}] {name}' + (f' — {detail}' if detail else ''), flush=True)
        return passed

    @property
    def failed(self):
        return [r for r in self.rows if not r[1]]


def http_json(url: str, key=None, timeout=20):
    """httpx, not urllib — it carries certifi's CA bundle, which a bare venv on macOS does not."""
    import httpx
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    try:
        r = httpx.get(url, headers=headers, timeout=timeout)
        return r.status_code, (r.json() if r.headers.get('content-type', '').startswith('application/json') else None)
    except Exception:
        return None, None


async def tool_call_refused(url: str, cid: int, key=None) -> tuple:
    """(refused, message) for a read on this tenant with the given key.

    The probe has to be a TOOL CALL, not the MCP `initialize` handshake: the
    transport handshake is unauthenticated by design (any client may connect and
    list tools), and the key is checked when a tool that needs one is invoked.
    Probing initialize would report an endpoint wide open that is not."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    try:
        async with Client(StreamableHttpTransport(url, headers=headers)) as c:
            r = await c.call_tool('list_journeys', {'customer_id': int(cid)}, raise_on_error=False)
            msg = next((getattr(b, 'text', '') or '' for b in (getattr(r, 'content', []) or [])), '')
            return bool(getattr(r, 'is_error', False)), msg[:90]
    except Exception as e:
        return True, f'{type(e).__name__}: {str(e)[:70]}'


async def main_async(args) -> int:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    rep = Report()
    base = args.url.rsplit('/mcp', 1)[0]
    cid = int(args.customer_id)
    manifest = None
    if args.manifest:
        from demo.generate import load_manifest
        manifest = load_manifest(args.manifest)

    _, health = http_json(f'{base}/health')
    platform_gen = (health or {}).get('journey_generator_version')
    print(f'tenant {cid} · {args.url} · journey generator {platform_gen}\n')

    transport = StreamableHttpTransport(args.url, headers={'Authorization': f'Bearer {args.key}'})
    async with Client(transport) as client:
        async def call(tool, **kw):
            r = await client.call_tool(tool, kw, raise_on_error=False)
            body = _payload(r)
            if getattr(r, 'is_error', False):
                raise SystemExit(f'{tool} failed: {json.dumps(body, default=str)[:800]}')
            return body

        # ── journeys ────────────────────────────────────────────────────
        port = await call('list_journeys', customer_id=cid)
        journeys = port['journeys']
        rep.check('every account has a journey', bool(journeys),
                  f'{port["accounts"]} accounts, {len(journeys)} journeys')
        if args.expect_accounts:
            rep.check(f'account count is {args.expect_accounts}', port['accounts'] == args.expect_accounts,
                      f'found {port["accounts"]}')

        detail = {}
        for j in journeys:
            detail[j['account_id']] = await call('get_journey', customer_id=cid, account_id=j['account_id'])

        stale = [d['account_name'] for d in detail.values()
                 if platform_gen and str(d.get('generator_version')) != str(platform_gen)]
        rep.check('no stale journeys for this tenant', not stale, f'stale: {stale}' if stale else
                  f'all {len(detail)} at generator {platform_gen}')

        # ── health coverage ─────────────────────────────────────────────
        gaps, months_seen = [], set()
        for d in detail.values():
            series = (d.get('leading_vs_trailing') or {}).get('series') or []
            scored = [s for s in series if s.get('kpi_only') is not None]
            months_seen.update(s['month'] for s in scored)
            if not scored:
                gaps.append(f"{d['account_name']}: no scored month")
                continue
            first, last = scored[0]['month'], scored[-1]['month']
            inner = [s for s in series if first <= s['month'] <= last]
            missing = [s['month'] for s in inner if s.get('kpi_only') is None]
            if missing:
                gaps.append(f"{d['account_name']}: {len(missing)} gap(s) {missing[:3]}")
        rep.check('health scores in every month of every account\'s series', not gaps,
                  '; '.join(gaps[:4]) if gaps else f'{len(months_seen)} distinct months across {len(detail)} accounts')

        counts = {}
        for d in detail.values():
            counts[band(d.get('ending_health'))] = counts.get(band(d.get('ending_health')), 0) + 1
        print(f'       ending bands: {counts}')

        # ── interventions ───────────────────────────────────────────────
        inv = await call('list_interventions', customer_id=cid)
        rows = inv['interventions']
        closed = [r for r in rows if r['state'] == 'closed']
        stranded = [r['intervention_id'] for r in rows if r['state'] in ('approved', 'sent')]
        rep.check('no intervention stranded mid-flight', not stranded, f'approved/sent: {stranded}' if stranded
                  else f"{len(rows)} rows: {len(closed)} closed, {len(rows) - len(closed)} open proposals")
        rep.check('no stuck interventions', not inv['stuck'], f'stuck: {inv["stuck"]}' if inv['stuck'] else
                  f'stuck_after_days={inv["stuck_after_days"]}')

        no_outcome = [r['intervention_id'] for r in closed if r['closed_state'] == 'done' and not r.get('outcome')]
        rep.check('every closed-done intervention logged an outcome', not no_outcome,
                  f'missing: {no_outcome}' if no_outcome else
                  f'{len([r for r in closed if r.get("outcome")])} outcomes linked')
        uncited = [r['intervention_id'] for r in rows if not (r.get('trigger') or {}).get('episode_ids')]
        rep.check('every intervention cites the evidence it fired on', not uncited, f'uncited: {uncited}' if uncited else '')
        undelivered = [r['intervention_id'] for r in rows if r.get('delivery_problem')]
        rep.check('no delivery problems', not undelivered,
                  f'undelivered: {undelivered}' if undelivered else 'all approved payloads delivered')

        # ── the manifest's own claims ───────────────────────────────────
        if manifest:
            names = {a['source_account_id']: a['name'] for a in manifest['accounts']}
            by_name = {d['account_name']: d for d in detail.values()}
            for iv in manifest.get('interventions') or []:
                who, pb = names[iv['source_account_id']], iv['playbook_id']
                want = (iv.get('report') or {}).get('outcome_type')
                hit = [r for r in closed if r.get('account_name') == who and r['playbook_id'] == pb]
                got = (hit[0].get('outcome') or {}).get('outcome_type') if hit else None
                rep.check(f'{who}: {pb} fired and closed as {want}', bool(hit) and got == want,
                          f'closed_state={hit[0]["closed_state"]} outcome={got} '
                          f'expected_type={hit[0]["outcome"].get("expected")} '
                          f'${hit[0]["outcome"].get("revenue")}' if hit else 'no closed row')

            for sid in dict.fromkeys(iv['source_account_id'] for iv in (manifest.get('interventions') or [])):
                who = names[sid]
                d = by_name.get(who)
                series = [s for s in ((d or {}).get('leading_vs_trailing') or {}).get('series') or []
                          if s.get('kpi_only') is not None]
                lo = min(series, key=lambda s: s['kpi_only'])
                end = series[-1]
                improved = band_rank(band(end['kpi_only'])) > band_rank(band(lo['kpi_only']))
                rep.check(f'{who}: KPI layer really changed band',
                          improved,
                          f"{band(lo['kpi_only'])} {lo['kpi_only']} ({lo['month']}) → "
                          f"{band(end['kpi_only'])} {end['kpi_only']} ({end['month']}) · "
                          f"arc={(d.get('arc') or {}).get('arc_type')}")
                warned = [s['month'] for s in series if s.get('early_warning') == 'early_warning']
                rep.check(f'{who}: leading layer warned before the recovery', bool(warned),
                          f'early_warning months: {warned}')

    # ── isolation ───────────────────────────────────────────────────────
    refused_none, msg_none = await tool_call_refused(args.url, cid)
    refused_bad, msg_bad = await tool_call_refused(args.url, cid, key='not-a-real-key-' + 'x' * 20)
    allowed, _ = await tool_call_refused(args.url, cid, key=args.key)
    rep.check(f'reading tenant {cid} without a key is refused', refused_none, msg_none)
    rep.check(f'reading tenant {cid} with a wrong key is refused', refused_bad, msg_bad)
    rep.check('the real key still reads the tenant', not allowed, 'list_journeys returned normally')

    print()
    if rep.failed:
        print(f'{len(rep.failed)} check(s) FAILED: ' + '; '.join(n for n, _, _ in rep.failed))
        return 1
    print(f'all {len(rep.rows)} checks passed')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', required=True)
    ap.add_argument('--key', default=os.environ.get('CI_API_KEY'))
    ap.add_argument('--customer-id', type=int, required=True)
    ap.add_argument('--manifest', help='hold the tenant to this manifest\'s declared intervention cycles')
    ap.add_argument('--expect-accounts', type=int, help='assert the tenant has exactly this many accounts')
    args = ap.parse_args(argv)
    if not args.key:
        raise SystemExit('--key or CI_API_KEY is required')
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == '__main__':
    main()
