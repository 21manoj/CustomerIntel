"""One-off local-dev helper: creates a throwaway tenant + admin login for testing the
frontend against a local backend. Not part of the product; not committed to run in CI.

    .venv/bin/python scripts/dev_seed_ui.py
"""
from __future__ import annotations

import asyncio
import json

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = 'http://localhost:8101'


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
            return json.loads(text)
    return {}


async def main():
    transport = StreamableHttpTransport(f'{BASE}/mcp')
    async with Client(transport) as client:
        r = await client.call_tool('create_customer', {
            'name': 'Dev Preview Co', 'domain': 'dev-preview.local', 'vertical': 'datacenter_v1',
            'admin_email': 'admin@dev-preview.local', 'admin_name': 'Dev Admin', 'data_origin': 'synthetic_demo',
        }, raise_on_error=False)
        c = _payload(r)
        print('create_customer:', json.dumps(c, indent=2))
        cid = c['customer_id']
        token = c['admin_setup_token']

    async with httpx.AsyncClient(base_url=BASE) as http:
        resp = await http.post('/app/api/auth/set-password', json={'token': token, 'new_password': 'DevPreview123!'})
        print('set-password:', resp.status_code, resp.text)

        resp = await http.post('/app/api/auth/login', json={'email': 'admin@dev-preview.local', 'password': 'DevPreview123!'})
        print('login:', resp.status_code, resp.text)
        cookie = resp.cookies.get('ci_session')

        resp = await http.get('/app/api/me', cookies={'ci_session': cookie})
        print('me:', resp.status_code, resp.text)

        resp = await http.get('/app/api/portfolio', params={'customer_id': cid}, cookies={'ci_session': cookie})
        print('portfolio:', resp.status_code, resp.text)

    print(f'\nLogin with: admin@dev-preview.local / DevPreview123!  (customer_id={cid})')


if __name__ == '__main__':
    asyncio.run(main())
