"""
Wizard C routes (Starlette, mounted beside /mcp).

  GET  /api/calibrations?customer_id=…&proposal_id=      read scope — the weights in force, one proposal in full, the list
  POST /api/calibrations/propose   {customer_id}          write scope — the explicit trigger (never from process_data)
  POST /api/calibrations/{id}/approve   {customer_id, note?}   write scope
  POST /api/calibrations/{id}/reject    {customer_id, note?}   write scope

Auth: the same Bearer keys as MCP (signal_engine.http._authorize).
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app, _json


def register_calibration_routes(mcp) -> None:
    from wizards import wizard_c_calibration as wc

    def _bad(e):
        return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/api/calibrations', methods=['GET'], name='calibrations_get')
    async def calibrations_get(request):
        q = request.query_params
        cid = q.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            pid = int(q['proposal_id']) if q.get('proposal_id') else None
            return JSONResponse(_with_app(lambda: wc.get_calibration(int(cid), pid)))
        except ValueError as e:
            return _bad(e)

    @mcp.custom_route('/api/calibrations/propose', methods=['POST'], name='calibrations_propose')
    async def calibrations_propose(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: wc.propose(int(cid))))
        except ValueError as e:
            return _bad(e)

    @mcp.custom_route('/api/calibrations/{proposal_id:int}/approve', methods=['POST'], name='calibrations_approve')
    async def calibrations_approve(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        try:
            return JSONResponse(_with_app(lambda: wc.approve(int(cid), request.path_params['proposal_id'], note=data.get('note'))))
        except ValueError as e:
            return _bad(e)

    @mcp.custom_route('/api/calibrations/{proposal_id:int}/reject', methods=['POST'], name='calibrations_reject')
    async def calibrations_reject(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        try:
            return JSONResponse(_with_app(lambda: wc.reject(int(cid), request.path_params['proposal_id'], note=data.get('note'))))
        except ValueError as e:
            return _bad(e)


ROUTES = ('/api/calibrations', '/api/calibrations/propose', '/api/calibrations/{id}/approve', '/api/calibrations/{id}/reject')
