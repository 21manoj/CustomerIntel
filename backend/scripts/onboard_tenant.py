"""
Onboard a tenant the way a customer or FDE would — over MCP/HTTPS with a
key, never touching the database. Nothing here is a shortcut: every step is
a tool call the platform audits.

    python scripts/onboard_tenant.py --url https://…/mcp --key $KEY --dir ./files \\
        --name "Lumen Workflows" --domain lumen.demo --vertical saas_premium --data-origin synthetic_demo

`--dir` holds what a customer would hand over (or what `demo/generate.py --out-dir` wrote):
  account_details.csv                required
  kpi_measurements.csv               optional (absent = signals-only tenant)
  enhanced_qualitative_signals.csv   optional (typed export: CRM flags etc.)
  communications.jsonl               optional (raw communications; one object per line)
  outcomes.csv                       optional (linked_signal_id may carry communication refs; rewritten to engine ids)

Order (signals-first): create → roster/typed CSVs → process_data → communications (batches ≤500, engine
processes + rebuilds journeys) → outcomes (refs → signal ids) → process_data → summary + receipt JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
from datetime import datetime
from pathlib import Path

BATCH = 500


def _payload(result):
    """CallToolResult → dict (structured content when the server gives it, else the text block)."""
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

    d = Path(args.dir)
    files = {f: (d / f).read_text() for f in ('account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv')
             if (d / f).exists()}
    if 'account_details.csv' not in files:
        raise SystemExit(f'{d}/account_details.csv is required')
    comms = [json.loads(l) for l in (d / 'communications.jsonl').read_text().splitlines() if l.strip()] if (d / 'communications.jsonl').exists() else []
    outcomes = (d / 'outcomes.csv').read_text() if (d / 'outcomes.csv').exists() else None
    receipt = {'url': args.url, 'started_at': datetime.utcnow().isoformat() + 'Z', 'steps': []}

    def say(msg):
        print(msg); receipt['steps'].append(msg)

    transport = StreamableHttpTransport(args.url, headers={'Authorization': f'Bearer {args.key}'})
    async with Client(transport) as client:
        async def call(tool, **kw):
            r = await client.call_tool(tool, kw, raise_on_error=False)
            body = _payload(r)
            if getattr(r, 'is_error', False):
                raise SystemExit(f'{tool} failed: {body}')
            return body

        c = await call('create_customer', name=args.name, domain=args.domain, vertical=args.vertical,
                       admin_email=args.admin_email or f'admin@{args.domain}', admin_name=args.admin_name, data_origin=args.data_origin)
        cid = c['customer_id']
        receipt.update({'customer_id': cid, 'data_origin': c['data_origin'], 'disclosure': c['disclosure'],
                        'customer_key_prefix': (c.get('api_key') or '')[:12] + '…' if c.get('api_key') else None})
        say(f"1 create_customer → customer_id={cid} data_origin={c['data_origin']} key_issued={'yes' if c.get('api_key') else 'no'}")
        if c.get('api_key') and args.save_key:
            Path(args.save_key).write_text(c['api_key']); os.chmod(args.save_key, 0o600)
            say(f"  customer key saved to {args.save_key} (shown once by the server)")

        for ft, content in files.items():
            u = await call('upload_csv', customer_id=cid, file_type=ft, csv_content=content)
            say(f"2 upload_csv {ft} → upload_id={u.get('upload_id')} rows={u.get('row_count')} warnings={len(u.get('warnings') or [])}")
        pd1 = await call('process_data', customer_id=cid)
        say(f"3 process_data → run_id={pd1.get('run_id')} status={pd1.get('status')} steps={pd1.get('steps_completed')}")

        by_ref = {}
        for i in range(0, len(comms), BATCH):
            chunk = comms[i:i + BATCH]
            imp = await call('import_communications', customer_id=cid, communications=chunk, process_now=True)
            by_ref.update(imp.get('by_ref') or {})
            say(f"4 import_communications [{i}:{i + len(chunk)}] → queued={imp['queued']} duplicates={imp['duplicates']} "
                f"unknown_accounts={len(imp['unknown_accounts'])} rejected={len(imp['rejected'])} "
                f"processed={(imp.get('processed') or {}).get('processed')} journeys_rebuilt={(imp.get('processed') or {}).get('journeys_rebuilt')}")
            if imp['unknown_accounts'] or imp['rejected']:
                say(f"  ! unknown_accounts={imp['unknown_accounts'][:5]} rejected={imp['rejected'][:5]}")

        if outcomes:
            rows = list(csv.DictReader(io.StringIO(outcomes)))
            for r in rows:
                if r.get('linked_signal_id'):
                    r['linked_signal_id'] = by_ref.get(r['linked_signal_id'], r['linked_signal_id'])
            buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
            u = await call('upload_csv', customer_id=cid, file_type='outcomes.csv', csv_content=buf.getvalue())
            say(f"5 upload_csv outcomes.csv → upload_id={u.get('upload_id')} rows={u.get('row_count')} (linked refs rewritten: {sum(1 for r in rows if r.get('linked_signal_id'))})")
            pd2 = await call('process_data', customer_id=cid)
            say(f"6 process_data → run_id={pd2.get('run_id')} status={pd2.get('status')} steps={pd2.get('steps_completed')}")

        port = await call('list_journeys', customer_id=cid)
        say(f"7 list_journeys → {port['accounts']} accounts · disclosure: {port['disclosure'][:80]}…")
        for row in port['journeys']:
            say(f"   {row['account_name']:28s} arc={row['arc_type'] or '-':24s} state={row['state']:12s} live_months={row['live_months']} "
                f"latest={row['latest'].get('month')} kpi={row['latest'].get('kpi_only')} qual={row['latest'].get('qual')} {row['latest'].get('early_warning') or ''} lead_days={row.get('lead_days')}")
        receipt['portfolio'] = port
    receipt['finished_at'] = datetime.utcnow().isoformat() + 'Z'
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(receipt, indent=1, default=str))
        print(f"receipt: {args.receipt}")
    return receipt


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', required=True, help='MCP endpoint, e.g. https://host/mcp')
    ap.add_argument('--key', default=os.environ.get('CI_API_KEY'), help='Bearer key (or env CI_API_KEY)')
    ap.add_argument('--dir', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--domain', required=True)
    ap.add_argument('--vertical', required=True)
    ap.add_argument('--data-origin', required=True, help='real | synthetic_demo | synthetic_replay | synthetic_test')
    ap.add_argument('--admin-email')
    ap.add_argument('--admin-name', default='Admin')
    ap.add_argument('--save-key', help='write the customer key the server returns (shown once) to this file')
    ap.add_argument('--receipt', help='write a JSON receipt of every step here')
    args = ap.parse_args(argv)
    if not args.key:
        raise SystemExit('--key or CI_API_KEY is required')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
