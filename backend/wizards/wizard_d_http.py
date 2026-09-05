"""
Wizard D (Foresight) read route (Starlette, mounted beside /mcp).

  GET /api/forecast?customer_id=…[&account_id=…]    read scope — the latest run: portfolio block + rows, or one account's block

Auth: the same Bearer keys as MCP (read scope).
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app


def register_forecast_routes(mcp) -> None:
    from wizards.wizard_d_foresight import get_forecast
    from journeys.read import origin_block

    @mcp.custom_route('/api/forecast', methods=['GET'], name='forecast_get')
    async def forecast_get(request):
        q = request.query_params
        cid = q.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        aid = q.get('account_id')
        res = _with_app(lambda: get_forecast(int(cid), int(aid) if aid else None))
        if res is None:
            return JSONResponse({'error': "no Foresight run yet (trigger_wizard d / process_data)"}, status_code=404)
        return JSONResponse(_with_app(lambda: {'customer_id': int(cid), **origin_block(int(cid)), **res}))


ROUTES = ('/api/forecast',)
