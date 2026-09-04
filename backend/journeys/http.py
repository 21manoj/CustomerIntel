"""
Journey / evidence read routes (Starlette, mounted beside /mcp).

  GET /api/journeys?customer_id=…                          portfolio rows
  GET /api/journeys/{account_id}?customer_id=…&compact=1   one journey + evidence index
  GET /api/evidence?customer_id=…&account_id=&role=&since=&until=&node_ids=1,2&include_rejected=1

Auth: the same Bearer keys as MCP, read scope.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app


def register_journey_routes(mcp) -> None:
    from journeys.read import list_journeys, get_journey, get_evidence

    @mcp.custom_route('/api/journeys', methods=['GET'], name='journeys_list')
    async def journeys_list(request):
        cid = request.query_params.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        return JSONResponse({'journeys': _with_app(lambda: list_journeys(int(cid)))})

    @mcp.custom_route('/api/journeys/{account_id:int}', methods=['GET'], name='journeys_get')
    async def journeys_get(request):
        cid = request.query_params.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        compact = request.query_params.get('compact', '') in ('1', 'true')
        j = _with_app(lambda: get_journey(int(cid), request.path_params['account_id'], compact=compact))
        if j is None:
            return JSONResponse({'error': 'no journey for this account (run process_data / trigger_wizard a)'}, status_code=404)
        return JSONResponse(j)

    @mcp.custom_route('/api/evidence', methods=['GET'], name='evidence_get')
    async def evidence(request):
        q = request.query_params
        cid = q.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        ids = [i for i in (q.get('node_ids') or '').split(',') if i.strip()]
        rows = _with_app(lambda: get_evidence(int(cid), q.get('account_id'), ids or None, q.get('role'), q.get('since'), q.get('until'),
                                              include_rejected=q.get('include_rejected', '') in ('1', 'true'),
                                              limit=int(q.get('limit', 200))))
        return JSONResponse({'evidence': rows, 'count': len(rows)})


ROUTES = ('/api/journeys', '/api/journeys/{account_id}', '/api/evidence')
