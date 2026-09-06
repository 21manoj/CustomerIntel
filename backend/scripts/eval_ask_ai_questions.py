"""
Quality check for the curated Ask AI questions (config/ask_ai_questions.json) —
runs every question against every real account (and the portfolio) of one or
more live tenants, over the real HTTP surface (POST /api/ask, the same route
an agent or the UI hits), and reports the answer engine's own honesty signals:
dropped/unsupported sentences, unverified numbers, and evidence_gaps.

This does NOT prove the model never hallucinates — it proves the validator
(ask_ai/answer.py: every sentence must cite an id actually shown to it) holds
up across every curated question on real data, and flags any question whose
own citation mechanics look broken. Without ANTHROPIC_API_KEY set, the server
answers via the deterministic keyword-matching stub (STUB_MODEL), not the real
LLM — this run exercises the citation/validation pipeline and scope routing,
not real-model hallucination resistance. Set ANTHROPIC_API_KEY on the server
and re-run for that.

    python scripts/eval_ask_ai_questions.py --url http://localhost:8101 --email admin@dev-preview.local \\
        --password DevPreview123! --customer-id 1 --customer-id 2

Uses the session-cookie surface (POST /app/api/ask, the same route the UI calls) rather
than the Bearer-key /api/ask, so this exercises the exact path a real user hits.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

QUESTIONS_PATH = Path(__file__).resolve().parent.parent / 'config' / 'ask_ai_questions.json'


def load_questions() -> dict:
    data = json.loads(QUESTIONS_PATH.read_text())
    return {k: v for k, v in data.items() if not k.startswith('_')}


def run_one(client: httpx.Client, customer_id: int, question: str, account_id: int | None, route: str) -> dict:
    try:
        r = client.post(route, json={'customer_id': customer_id, 'question': question, 'account_id': account_id}, timeout=60)
    except httpx.HTTPError as e:
        return {'error': f'transport: {e}'}
    if r.status_code != 200:
        return {'error': f'HTTP {r.status_code}: {r.text[:200]}'}
    body = r.json()
    return {
        'sentences_kept': len(body.get('sentences') or []),
        'unsupported': len(body.get('unsupported') or []),
        'unverified_numbers': sum(len(s.get('unverified_numbers') or []) for s in body.get('sentences') or []),
        'evidence_gaps': len(body.get('evidence_gaps') or []),
        'confidence': body.get('confidence'),
        'model': body.get('model'),
        'answer_empty': not (body.get('answer') or '').strip(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='http://localhost:8101')
    ap.add_argument('--email', help='session-cookie mode: an admin login (admin sees every tenant)')
    ap.add_argument('--password', help='session-cookie mode password')
    ap.add_argument('--bearer', help='Bearer-key mode instead of login: MCP_SERVER_API_KEY or a customer key; uses /api/ask + the ask MCP tool for the account list')
    ap.add_argument('--customer-id', type=int, action='append', required=True, dest='customer_ids')
    ap.add_argument('--report', help='write the full per-question JSON here')
    args = ap.parse_args(argv)
    if not args.bearer and not (args.email and args.password):
        print('need either --bearer or --email/--password')
        return 1

    questions = load_questions()
    client = httpx.Client(base_url=args.url)
    route = '/api/ask' if args.bearer else '/app/api/ask'
    if args.bearer:
        client.headers['Authorization'] = f'Bearer {args.bearer}'
    else:
        login = client.post('/app/api/auth/login', json={'email': args.email, 'password': args.password})
        if login.status_code != 200:
            print(f'login failed: HTTP {login.status_code}: {login.text[:200]}')
            return 1

    def list_accounts(cid: int) -> list:
        if not args.bearer:
            r = client.get('/app/api/portfolio', params={'customer_id': cid})
            return r.json().get('accounts') or [] if r.status_code == 200 else []
        import asyncio
        from fastmcp import Client as MCPClient
        from fastmcp.client.transports import StreamableHttpTransport

        async def _fetch():
            transport = StreamableHttpTransport(f'{args.url}/mcp', headers={'Authorization': f'Bearer {args.bearer}'})
            async with MCPClient(transport) as c:
                r = await c.call_tool('list_journeys', {'customer_id': cid}, raise_on_error=False)
                data = getattr(r, 'data', None) or {}
                return data.get('journeys') or []
        rows = asyncio.run(_fetch())
        return [{'account_id': r['account_id'], 'account_name': r['account_name']} for r in rows]

    rows = []
    flags = []
    for cid in args.customer_ids:
        accounts = list_accounts(cid)
        if not accounts:
            print(f'! customer {cid}: no accounts found (or portfolio load failed) — skipping')
            continue
        print(f'\n== customer_id={cid} — {len(accounts)} accounts ==')
        for role, qs in questions.items():
            for q in qs:
                if q['scope'] == 'portfolio':
                    res = run_one(client, cid, q['text'], None, route)
                    res.update(role=role, question_id=q['id'], customer_id=cid, account_id=None, account_name=None)
                    rows.append(res)
                    if 'error' in res or res.get('answer_empty'):
                        flags.append(res)
                else:
                    for a in accounts:
                        res = run_one(client, cid, q['text'], a['account_id'], route)
                        res.update(role=role, question_id=q['id'], customer_id=cid, account_id=a['account_id'], account_name=a['account_name'])
                        rows.append(res)
                        if 'error' in res:
                            flags.append(res)

    total = len(rows)
    errors = [r for r in rows if 'error' in r]
    empty = [r for r in rows if not r.get('error') and r.get('answer_empty')]
    unsupported_total = sum(r.get('unsupported', 0) for r in rows if 'error' not in r)
    unverified_total = sum(r.get('unverified_numbers', 0) for r in rows if 'error' not in r)
    models_seen = sorted({r['model'] for r in rows if r.get('model')})

    print(f'\n{total} calls · {len(errors)} error(s) · {len(empty)} empty answer(s) · '
          f'{unsupported_total} sentence(s) dropped as unsupported across all calls · '
          f'{unverified_total} unverified-number flag(s) · model(s) used: {models_seen}')

    if errors:
        print('\n-- errors --')
        for e in errors[:20]:
            print(f"  [{e['role']}/{e['question_id']}] customer={e['customer_id']} account={e.get('account_name')}: {e['error']}")

    if empty:
        print('\n-- questions that produced NO grounded sentence at all (candidates to rewrite or drop) --')
        seen = set()
        for e in empty:
            key = (e['role'], e['question_id'])
            if key in seen:
                continue
            seen.add(key)
            print(f"  [{e['role']}/{e['question_id']}] e.g. customer={e['customer_id']} account={e.get('account_name')}")

    if args.report:
        Path(args.report).write_text(json.dumps(rows, indent=1, default=str))
        print(f'\nfull report: {args.report}')

    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
