"""
Drive a manifest's whole story over MCP/HTTPS — tranche by tranche, closing
the loop between them. Nothing here touches the database.

    python scripts/drive_tenant_story.py --url https://…/mcp --key $KEY \\
        --manifest demo/manifests/aurelia_datacenter.json --receipt out/receipt.json

Why this exists (and why onboard_tenant.py is not enough): a playbook
evaluates the LATEST leading month (playbooks.governance._match), so a tenant
loaded in one shot can only ever fire on its final month. A story where an
account gets into trouble, a playbook fires, the intervention closes, and
MONTHS LATER a different problem starts a second cycle can only be built the
way a real tenant lives — data arriving over time, with the product reacting
to each arrival before the next one lands.

A manifest declares that shape:

    "tranches":      [{"id": "t1", "through_day": -266}, ...]        ascending
    "interventions": [{"tranche": "t1", "source_account_id": "CEREBRIX",
                       "playbook_id": "incident_escalation",
                       "approve_note": "...",
                       "report": {"state": "done", "outcome_type": "revenue_protected",
                                  "outcome_day": -250, "revenue": 400000, "note": "..."}}]

Per tranche: demo.generate.slice_manifest → generate → upload_csv (roster, KPI
rows, CSM flags) → process_data → import_communications → outcomes (refs
rewritten to engine signal ids) → process_data. Then, for each intervention
declared on that tranche: evaluate_playbooks → approve_intervention →
report_intervention (which logs the outcome and links it to the INTERVENTION
node). The next tranche's data only arrives after that cycle has closed, so
the recovery that follows genuinely did not exist when the loop was closed.

A declared intervention whose playbook did not actually fire is a hard failure
that prints what the evaluator DID propose and why it skipped the rest — the
manifest is a claim about the product's behaviour, not a script it obeys.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.onboard_tenant import _payload           # noqa: E402  (same CallToolResult → dict rule)

BATCH = 500
CSV_ORDER = ('account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv')


def _csv_text(rows: list, columns: list) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction='ignore')
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


async def run(args) -> dict:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from demo.generate import OUTCOME_COLUMNS, generate, load_manifest, slice_manifest, expand_accounts
    from demo.manifest_v2 import plan_communications

    manifest = load_manifest(args.manifest)
    tranches = manifest.get('tranches') or []
    if not tranches:
        raise SystemExit(f'{args.manifest} declares no "tranches" — use scripts/onboard_tenant.py for a single load')
    t0 = datetime.fromisoformat(manifest['timeline']['t0'])
    names = {a.get('source_account_id') or a['name']: a['name'] for a in expand_accounts(manifest)}
    by_tranche: dict = {}
    for iv in manifest.get('interventions') or []:
        by_tranche.setdefault(iv['tranche'], []).append(iv)

    receipt = {'url': args.url, 'manifest': manifest['manifest_id'], 'started_at': datetime.utcnow().isoformat() + 'Z',
               'steps': [], 'tranches': [], 'interventions': []}

    def say(msg):
        print(msg, flush=True)
        receipt['steps'].append(msg)

    transport = StreamableHttpTransport(args.url, headers={'Authorization': f'Bearer {args.key}'})
    async with Client(transport) as client:
        async def call(tool, **kw):
            r = await client.call_tool(tool, kw, raise_on_error=False)
            body = _payload(r)
            if getattr(r, 'is_error', False):
                raise SystemExit(f'{tool} failed: {json.dumps(body, default=str)[:2000]}')
            return body

        if args.customer_id:
            cid = int(args.customer_id)
            say(f'0 existing tenant customer_id={cid}')
        else:
            c = await call('create_customer', name=manifest['customer_name'],
                           domain=f"{manifest['domain_prefix']}.demo", vertical=manifest['vertical'],
                           admin_email=f"admin@{manifest['domain_prefix']}.demo", admin_name='Demo Admin',
                           data_origin=args.data_origin)
            cid = c['customer_id']
            receipt.update({'customer_id': cid, 'data_origin': c.get('data_origin'), 'disclosure': c.get('disclosure')})
            say(f"0 create_customer → customer_id={cid} data_origin={c.get('data_origin')} "
                f"key_issued={'yes' if c.get('api_key') else 'no'}")
            if c.get('api_key') and args.save_key:
                Path(args.save_key).write_text(c['api_key'])
                os.chmod(args.save_key, 0o600)
                say(f'  customer key → {args.save_key} (the server shows it once)')
        receipt['customer_id'] = cid

        for t in tranches:
            tid, through = t['id'], int(t['through_day'])
            sliced = slice_manifest(manifest, through)
            files = generate(sliced)
            comms = plan_communications(sliced, sliced['accounts'])
            asof = (t0 + timedelta(days=through)).date().isoformat()
            say(f"\n── tranche {tid} · data through day {through} ({asof}) ─────────────")

            for ft in CSV_ORDER:
                if ft not in files:
                    continue
                u = await call('upload_csv', customer_id=cid, file_type=ft, csv_content=files[ft])
                say(f"  upload_csv {ft:34s} rows={u.get('row_count')} warnings={len(u.get('warnings') or [])}")
            pd1 = await call('process_data', customer_id=cid)
            say(f"  process_data status={pd1.get('status')} steps={len(pd1.get('steps_completed') or [])} "
                f"errors={len(pd1.get('errors') or [])}")

            by_ref = {}
            for i in range(0, len(comms), BATCH):
                chunk = [{'ref': c['ref'], 'source_account_id': c['source_account_id'], 'source_type': c['source_type'],
                          'text': c['text'], 'source_ref': c['source_ref'], 'participants': c['participants'],
                          'occurred_at': c['occurred_at'].isoformat() if hasattr(c['occurred_at'], 'isoformat') else c['occurred_at'],
                          } for c in comms[i:i + BATCH]]
                imp = await call('import_communications', customer_id=cid, communications=chunk, process_now=True)
                by_ref.update(imp.get('by_ref') or {})
                p = imp.get('processed') or {}
                say(f"  import_communications [{i}:{i + len(chunk)}] queued={imp.get('queued')} dup={imp.get('duplicates')} "
                    f"unknown={len(imp.get('unknown_accounts') or [])} rejected={len(imp.get('rejected') or [])} "
                    f"processed={p.get('processed')} journeys_rebuilt={p.get('journeys_rebuilt')}")
                if imp.get('unknown_accounts') or imp.get('rejected'):
                    raise SystemExit(f"  ! unknown_accounts={imp['unknown_accounts'][:5]} rejected={imp['rejected'][:5]}")

            if files.get('outcomes.csv'):
                rows = list(csv.DictReader(io.StringIO(files['outcomes.csv'])))
                linked = 0            # by_ref is keyed by the manifest's own communication ref (we send it as 'ref')
                for r in rows:
                    if r.get('linked_signal_id'):
                        r['linked_signal_id'] = by_ref.get(r['linked_signal_id'], r['linked_signal_id'])
                        linked += 1
                u = await call('upload_csv', customer_id=cid, file_type='outcomes.csv', csv_content=_csv_text(rows, OUTCOME_COLUMNS))
                say(f"  upload_csv outcomes.csv                   rows={u.get('row_count')} linked_refs_rewritten={linked}")
                pd2 = await call('process_data', customer_id=cid)
                say(f"  process_data status={pd2.get('status')} errors={len(pd2.get('errors') or [])}")

            receipt['tranches'].append({'id': tid, 'through_day': through, 'as_of': asof,
                                        'communications': len(comms), 'files': sorted(files)})

            for iv in by_tranche.get(tid, []):
                receipt['interventions'].append(await cycle(call, say, cid, iv, names, t0, args.wait_ack))

        say('\n── portfolio ─────────────────────────────────────────────────')
        port = await call('list_journeys', customer_id=cid)
        receipt['portfolio'] = port
        say(f"list_journeys → {port['accounts']} accounts")
        for row in sorted(port['journeys'], key=lambda r: r['account_name']):
            latest = row.get('latest') or {}
            say(f"  {row['account_name']:26s} arc={(row.get('arc_type') or '-'):24s} state={row.get('state', '-'):12s} "
                f"months={row.get('live_months')} latest={latest.get('month')} kpi={latest.get('kpi_only')} "
                f"qual={latest.get('qual')} {latest.get('early_warning') or ''}")
        inv = await call('list_interventions', customer_id=cid)
        receipt['interventions_final'] = inv
        say(f"list_interventions → {inv['count']} rows: " +
            ', '.join(f"{s['playbook_id']}(closed_done={s['closed_done']} proposed={s['proposed']})" for s in inv['by_playbook']))

    receipt['finished_at'] = datetime.utcnow().isoformat() + 'Z'
    if args.receipt:
        Path(args.receipt).parent.mkdir(parents=True, exist_ok=True)
        Path(args.receipt).write_text(json.dumps(receipt, indent=1, default=str))
        print(f'\nreceipt: {args.receipt}')
    return receipt


async def cycle(call, say, cid: int, iv: dict, names: dict, t0: datetime, wait_ack: float = 0.0) -> dict:
    """One declared intervention cycle: evaluate → approve → report (+ outcome).
    Fails loudly, with the evaluator's own reasons, if the playbook did not fire."""
    account_name, pb_id = names[iv['source_account_id']], iv['playbook_id']
    ev = await call('evaluate_playbooks', customer_id=cid)
    open_rows = (await call('list_interventions', customer_id=cid, state='proposed'))['interventions']
    match = next((r for r in open_rows if r.get('account_name') == account_name and r['playbook_id'] == pb_id), None)
    if not match:
        proposed = [f"{p.get('account_name')}/{p['playbook_id']}" for p in ev.get('proposed') or []]
        skipped = [f"{s.get('account_id')}/{s['playbook_id']}:{s['reason']}" for s in ev.get('skipped') or []
                   if s['playbook_id'] == pb_id][:8]
        raise SystemExit(f"! {account_name}: playbook {pb_id!r} did not fire.\n"
                         f"  proposed this pass: {proposed or 'none'}\n"
                         f"  skip reasons for {pb_id}: {skipped or 'none'}\n"
                         f"  open proposals: {[(r.get('account_name'), r['playbook_id']) for r in open_rows]}")
    iid = match['intervention_id']
    say(f"  ▸ {account_name} · {pb_id} #{iid} proposed · urgency={match.get('urgency')} "
        f"roles={match['trigger'].get('roles')} cites={match['trigger'].get('episode_ids')}")

    ap = await call('approve_intervention', customer_id=cid, intervention_id=iid,
                    note=iv.get('approve_note') or f'approved for {account_name}')
    delivery = (ap.get('delivery') or {}).get('status')
    say(f"    approve → state={ap['state']} delivery={delivery} node={ap.get('node_id')}")

    # A delivered payload means a workflow engine now owns this row; it acks 'started' on its own
    # thread. Let that land before reporting 'done', or the ack arrives at a closed row and the
    # engine logs a failure that is ours, not its.
    acked = None
    if delivery == 'delivered' and wait_ack > 0:
        for _ in range(int(wait_ack * 2)):
            await asyncio.sleep(0.5)
            rows = (await call('list_interventions', customer_id=cid, account_id=match['account_id']))['interventions']
            row = next((r for r in rows if r['intervention_id'] == iid), None)
            if row and row.get('last_report_at'):
                acked = row['last_report_at']
                break
        say(f"    ack     → {'workflow engine acked at ' + acked if acked else f'no ack within {wait_ack}s (proceeding)'}")

    rep = iv.get('report') or {}
    kw = {'customer_id': cid, 'intervention_id': iid, 'state': rep['state'], 'note': rep.get('note')}
    if rep.get('outcome_type'):
        kw['outcome_type'] = rep['outcome_type']
        if rep.get('outcome_day') is not None:
            kw['outcome_date'] = (t0 + timedelta(days=int(rep['outcome_day']))).date().isoformat()
        if rep.get('revenue') is not None:
            kw['revenue'] = float(rep['revenue'])
    rp = await call('report_intervention', **kw)
    oc = rp.get('outcome') or {}
    say(f"    report  → state={rp['state']}/{rp.get('closed_state')} outcome={oc.get('outcome_type')} "
        f"${oc.get('revenue')} in_window={oc.get('in_window')} expected={oc.get('expected')} "
        f"journeys_rebuilt={rp.get('journeys_rebuilt')}")
    return {'account_name': account_name, 'playbook_id': pb_id, 'intervention_id': iid, 'state': rp['state'],
            'closed_state': rp.get('closed_state'), 'delivery': (ap.get('delivery') or {}).get('status'),
            'trigger_roles': match['trigger'].get('roles'), 'trigger_quote': match['trigger'].get('quote'),
            'urgency': match.get('urgency'), 'outcome': oc, 'node_id': ap.get('node_id'), 'workflow_ack_at': acked}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', required=True, help='MCP endpoint, e.g. https://host/mcp')
    ap.add_argument('--key', default=os.environ.get('CI_API_KEY'), help='Bearer key (or env CI_API_KEY)')
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--customer-id', type=int, help='drive an existing tenant instead of creating one')
    ap.add_argument('--data-origin', default='synthetic_demo')
    ap.add_argument('--save-key', help='write the customer key the server returns (shown once) here')
    ap.add_argument('--receipt', help='write a JSON receipt of every step here')
    ap.add_argument('--wait-ack', type=float, default=15.0,
                    help="seconds to wait for a delivered intervention's workflow engine to ack 'started' "
                         'before reporting the close (0 disables)')
    args = ap.parse_args(argv)
    if not args.key:
        raise SystemExit('--key or CI_API_KEY is required')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
