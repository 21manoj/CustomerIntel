"""One-off: load the customer359 datacenter_v1 fixture into the local dev tenant
(customer_id=1, created by dev_seed_ui.py) so the frontend has real rows to render."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = 'http://localhost:8101'
FIXTURE = Path(__file__).resolve().parent.parent / 'tests/fixtures/customer359_datacenter_v1'


def _payload(result):
    data = getattr(result, 'data', None)
    if isinstance(data, dict):
        return data
    for block in getattr(result, 'content', []) or []:
        text = getattr(block, 'text', None)
        if text:
            return json.loads(text)
    return {}


async def main(customer_id: int):
    transport = StreamableHttpTransport(f'{BASE}/mcp')
    async with Client(transport) as client:
        for ft in ('account_details', 'enhanced_qualitative_signals', 'kpi_measurements', 'outcomes'):
            content = (FIXTURE / f'{ft}.csv').read_text()
            r = await client.call_tool('upload_csv', {'customer_id': customer_id, 'file_type': f'{ft}.csv', 'csv_content': content}, raise_on_error=False)
            print(ft, '→', _payload(r))
        r = await client.call_tool('process_data', {'customer_id': customer_id}, raise_on_error=False)
        print('process_data →', json.dumps(_payload(r), indent=2)[:2000])


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
