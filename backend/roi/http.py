"""
Power-of-1 / ROI read routes (Starlette, mounted beside /mcp). Read scope.

  GET /api/roi/priorities?customer_id=…&account_id=     ranked accounts, cited; portfolio totals
  GET /api/roi/power-of-1?customer_id=…&account_id=     $ per pillar / KPI point on the tenant's own base, labelled
  GET /api/roi?customer_id=…                            realized vs exposure per playbook / pillar, ledger, hindsight, sensitivity
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app


def register_roi_routes(mcp) -> None:
    from roi.priorities import investment_priorities
    from roi.power_of_1 import power_of_1
    from roi.measured import roi

    def _guard(request):
        cid = request.query_params.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return cid, JSONResponse(err, status_code=401)
        if not cid:
            return cid, JSONResponse({'error': 'customer_id is required'}, status_code=400)
        return cid, None

    def _run(fn):
        try:
            return JSONResponse(_with_app(fn))
        except ValueError as e:                       # unknown vertical / missing economics: fail closed, say why
            return JSONResponse({'error': str(e)}, status_code=422)

    @mcp.custom_route('/api/roi/priorities', methods=['GET'], name='roi_priorities')
    async def roi_priorities(request):
        cid, err = _guard(request)
        if err:
            return err
        aid = request.query_params.get('account_id')
        return _run(lambda: investment_priorities(int(cid), int(aid) if aid else None))

    @mcp.custom_route('/api/roi/power-of-1', methods=['GET'], name='roi_power_of_1')
    async def roi_power_of_1(request):
        cid, err = _guard(request)
        if err:
            return err
        aid = request.query_params.get('account_id')
        return _run(lambda: power_of_1(int(cid), int(aid) if aid else None))

    @mcp.custom_route('/api/roi', methods=['GET'], name='roi_measured')
    async def roi_measured(request):
        cid, err = _guard(request)
        if err:
            return err
        return _run(lambda: roi(int(cid)))


ROUTES = ('/api/roi/priorities', '/api/roi/power-of-1', '/api/roi')
