"""
Ask AI route (Starlette, mounted beside /mcp).

  POST /api/ask  {customer_id, question, account_id?, as_of?}   read scope

Auth: the same Bearer keys as MCP, read scope. 400 on a missing field,
404 when the account has no journey, 502 when the model call fails.
"""
from __future__ import annotations

import logging

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app

logger = logging.getLogger(__name__)


def register_ask_routes(mcp) -> None:
    from ask_ai.answer import ask

    @mcp.custom_route('/api/ask', methods=['POST'], name='ask')
    async def ask_route(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid or not (data.get('question') or '').strip():
            return JSONResponse({'error': 'customer_id and question are required'}, status_code=400)
        try:
            res = _with_app(lambda: ask(int(cid), data['question'], account_id=data.get('account_id'), as_of=data.get('as_of')))
        except LookupError as e:
            return JSONResponse({'error': str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)
        except Exception as e:
            logger.exception('ask failed for customer %s: %s', cid, e)
            return JSONResponse({'error': f'answer failed: {str(e)[:200]}'}, status_code=502)
        return JSONResponse(res)


ROUTES = ('/api/ask',)
