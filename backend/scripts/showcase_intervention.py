#!/usr/bin/env python3
"""
One governed intervention on a live tenant, through the tools only (MCP over HTTPS + a key) —
the same user-scoped path scripts/onboard_tenant.py uses. No database access.

    python scripts/showcase_intervention.py --url https://host/mcp --key … --customer-id 10 \
        [--playbook champion_departure_sponsor_rebuild] [--account-name Northstar] \
        [--report done --outcome-type renewal_secured --outcome-date 2026-09-05 --revenue 420000] \
        [--signal-type escalation --signal-text "…" --signal-date 2026-09-01]   # add evidence first when nothing fires
        [--receipt docs/design/showcase/<name>_intervention_receipt.json]

Steps, each printed and kept in the receipt: get_playbooks → (optional submit_signal) → evaluate_playbooks dry_run →
evaluate_playbooks → approve_intervention → report_intervention started → report_intervention <state> (+ outcome)
→ list_interventions → get_journey (the intervention episode, its hook, the narrative sentence that cites it).
"""
import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path


def _payload(result):
    data = getattr(result, 'data', None)
    if isinstance(data, dict):
        return data
    sc = getattr(result, 'structured_content', None)
    if isinstance(sc, dict):
        return sc.get('result', sc)
    for block in getattr(result, 'content', []) or []:
        text = getattr(block, 'text', None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {'text': text}
    return {}


async def run(args) -> dict:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    cid = int(args.customer_id)
    receipt = {'url': args.url, 'customer_id': cid, 'started_at': datetime.utcnow().isoformat() + 'Z', 'steps': [], 'calls': []}

    def say(msg):
        print(msg); receipt['steps'].append(msg)

    transport = StreamableHttpTransport(args.url, headers={'Authorization': f'Bearer {args.key}'})
    async with Client(transport) as client:
        async def call(tool, **kw):
            r = await client.call_tool(tool, kw, raise_on_error=False)
            body = _payload(r)
            receipt['calls'].append({'tool': tool, 'args': {k: v for k, v in kw.items() if k != 'webhook_secret'}, 'result': body})
            if getattr(r, 'is_error', False):
                raise SystemExit(f'{tool} failed: {body}')
            return body

        pb = await call('get_playbooks', customer_id=cid)
        say(f"1 get_playbooks → vertical={pb['vertical']} playbooks={[p['id'] for p in pb['playbooks']]} "
            f"level={pb['tenant']['automation_level']} kill={pb['tenant']['kill_switch']} webhook={'set' if pb['tenant']['webhook_url'] else 'none'}")
        journeys = await call('list_journeys', customer_id=cid)
        acct = None
        for row in journeys['journeys']:
            if args.account_name and args.account_name.lower() in row['account_name'].lower():
                acct = row
        if args.account_name and not acct:
            raise SystemExit(f'no account matching {args.account_name!r}')
        if args.signal_type and acct:
            s = await call('submit_signal', customer_id=cid, account_id=acct['account_id'], raw_text=args.signal_text,
                           source_type=args.signal_source, signal_type=args.signal_type, occurred_at=args.signal_date, process_now=True)
            say(f"2 submit_signal {args.signal_type} on {acct['account_name']} → {s['status']} node={(s.get('evidence') or {}).get('node_id')} "
                f"journeys_rebuilt={s.get('journeys_rebuilt')} (the rebuild hook already evaluated the playbooks)")
        dry = await call('evaluate_playbooks', customer_id=cid, account_id=acct['account_id'] if acct else None, dry_run=True)
        say(f"3 evaluate_playbooks dry_run → status={dry['status']} accounts={dry['accounts_evaluated']} would_propose="
            f"{[(p['account_name'], p['playbook_id'], p['urgency']) for p in dry['proposed']]} skipped={[(s['account_id'], s['playbook_id'], s['reason']) for s in dry['skipped']][:8]}")
        ev = await call('evaluate_playbooks', customer_id=cid, account_id=acct['account_id'] if acct else None)
        say(f"4 evaluate_playbooks → proposed={[(p['account_name'], p['playbook_id'], p.get('intervention_id')) for p in ev['proposed']]} auto={ev['auto_approved']}")
        li = await call('list_interventions', customer_id=cid, account_id=acct['account_id'] if acct else None, state='proposed')
        cands = [v for v in li['interventions'] if not args.playbook or v['playbook_id'] == args.playbook]
        if not cands:
            say(f"   no proposed intervention{' for ' + args.playbook if args.playbook else ''}; nothing to approve. Rows: "
                f"{[(v['intervention_id'], v['account_name'], v['playbook_id'], v['state']) for v in li['interventions']]}")
            receipt['finished_at'] = datetime.utcnow().isoformat() + 'Z'
            return receipt
        target = cands[0]
        say(f"   approving #{target['intervention_id']} {target['playbook_id']} on {target['account_name']} urgency={target['urgency']} "
            f"cites={target['trigger']['episode_ids']} quote={target['trigger']['quote'][:90]!r}")
        ap = await call('approve_intervention', customer_id=cid, intervention_id=target['intervention_id'], note=args.approve_note)
        say(f"5 approve_intervention → state={ap['state']} approved_by={ap['approved_by']} delivery={ap['delivery']['status']} "
            f"({ap['delivery'].get('error') or ap['delivery'].get('url_host')}) node={ap['node_id']} journeys_rebuilt={ap['journeys_rebuilt']}")
        say(f"   payload keys={sorted(ap['payload'])} trigger={[(t['episode_id'], t['role']) for t in ap['payload']['trigger']]}")
        st = await call('report_intervention', customer_id=cid, intervention_id=target['intervention_id'], state='started', note=args.started_note)
        say(f"6 report_intervention started → started_at={st['started_at']}")
        kw = {'state': args.report, 'note': args.report_note}
        if args.outcome_type:
            kw.update({'outcome_type': args.outcome_type, 'outcome_date': args.outcome_date, 'revenue': args.revenue})
        rp = await call('report_intervention', customer_id=cid, intervention_id=target['intervention_id'], **kw)
        say(f"7 report_intervention {args.report} → state={rp['state']} closed_state={rp['closed_state']} outcome={rp.get('outcome')}")
        li2 = await call('list_interventions', customer_id=cid)
        s = next(x for x in li2['by_playbook'] if x['playbook_id'] == target['playbook_id'])
        say(f"8 list_interventions → {li2['count']} rows, stuck={li2['stuck']}; {target['playbook_id']}: {json.dumps({k: v for k, v in s.items() if k != 'note'})}")
        j = await call('get_journey', customer_id=cid, account_id=target['account_id'])
        ep = next((e for e in j['episodes'] if e['kind'] == 'intervention' and (e.get('meta') or {}).get('intervention_id') == target['intervention_id']), None)
        hook = next((h for h in j['counterfactual_hooks'] if ep and h['episode_id'] == ep['episode_id']), None)
        cited = [sn['text'] for ch in (j.get('narrative') or {}).get('chapters', []) for sn in ch['sentences'] if ep and ep['episode_id'] in sn['cites']]
        say(f"9 get_journey {j['account_name']} → episode={ep and ep['episode_id']} phase={j.get('current_phase')} hook_outcomes_after={hook and hook['outcomes_after']} "
            f"narrative={cited[:1]} disclosure={j.get('disclosure', '')[:60]}…")
        receipt.update({'intervention_id': target['intervention_id'], 'account_id': target['account_id'], 'playbook_id': target['playbook_id'],
                        'node_id': ap['node_id'], 'delivery': ap['delivery'], 'closed_state': rp['closed_state'], 'outcome': rp.get('outcome')})
    receipt['finished_at'] = datetime.utcnow().isoformat() + 'Z'
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(receipt, indent=1, default=str))
        print(f'receipt: {args.receipt}')
    return receipt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', required=True)
    ap.add_argument('--key', default=os.environ.get('CI_API_KEY'))
    ap.add_argument('--customer-id', required=True)
    ap.add_argument('--account-name')
    ap.add_argument('--playbook')
    ap.add_argument('--signal-type'); ap.add_argument('--signal-text'); ap.add_argument('--signal-date'); ap.add_argument('--signal-source', default='meeting')
    ap.add_argument('--approve-note', default='approved from the showcase run')
    ap.add_argument('--started-note', default='workflow reported started')
    ap.add_argument('--report', default='done', choices=['done', 'failed', 'cancelled'])
    ap.add_argument('--report-note', default='workflow reported done')
    ap.add_argument('--outcome-type'); ap.add_argument('--outcome-date'); ap.add_argument('--revenue', type=float)
    ap.add_argument('--receipt')
    args = ap.parse_args(argv)
    if not args.key:
        raise SystemExit('--key or CI_API_KEY is required')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
