"""
Playbook governance routes (Starlette, mounted beside /mcp).

  GET  /api/interventions?customer_id=…&account_id=&state=          read scope — rows, stuck ones, per-playbook numbers
  POST /api/interventions/evaluate   {customer_id, account_id?, dry_run?}                          write scope
  POST /api/interventions/{id}/approve   {customer_id, note?}                                     write scope
  POST /api/interventions/{id}/report    {customer_id, state, note?, outcome_type?, outcome_date?, revenue?}   write scope
  GET  /api/playbooks?customer_id=…                                  read scope — the tenant's playbooks + overlay (secret masked)
  POST /api/playbooks  {customer_id, webhook_url?, webhook_secret?, disabled_playbooks?, automation_level?, kill_switch?}   write scope

Auth: the same Bearer keys as MCP. The workflow engine reports back with its
own key (write scope), the way it already calls submit_signal / log_outcome.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app, _json


def register_playbook_routes(mcp) -> None:
    from playbooks import governance as gov
    from playbooks.definitions import playbooks_for_customer, configure_tenant

    def _bad(e):
        return JSONResponse({'error': str(e)}, status_code=400)

    @mcp.custom_route('/api/interventions', methods=['GET'], name='interventions_list')
    async def interventions_list(request):
        q = request.query_params
        cid = q.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        return JSONResponse(_with_app(lambda: gov.list_interventions(int(cid), q.get('account_id'), q.get('state'))))

    @mcp.custom_route('/api/interventions/evaluate', methods=['POST'], name='interventions_evaluate')
    async def interventions_evaluate(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        try:
            return JSONResponse(_with_app(lambda: gov.evaluate(int(cid), data.get('account_id'), bool(data.get('dry_run')))))
        except ValueError as e:
            return _bad(e)

    @mcp.custom_route('/api/interventions/{intervention_id:int}/approve', methods=['POST'], name='interventions_approve')
    async def interventions_approve(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        try:
            return JSONResponse(_with_app(lambda: gov.approve(int(cid), request.path_params['intervention_id'], data.get('note'))))
        except ValueError as e:
            return _bad(e)

    @mcp.custom_route('/api/interventions/{intervention_id:int}/report', methods=['POST'], name='interventions_report')
    async def interventions_report(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        try:
            return JSONResponse(_with_app(lambda: gov.report(int(cid), request.path_params['intervention_id'], data.get('state'),
                                                             note=data.get('note'), outcome_type=data.get('outcome_type'),
                                                             outcome_date=data.get('outcome_date'), revenue=data.get('revenue'))))
        except ValueError as e:
            return _bad(e)

    @mcp.custom_route('/api/playbooks', methods=['GET'], name='playbooks_get')
    async def playbooks_get(request):
        cid = request.query_params.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        return JSONResponse(_with_app(lambda: playbooks_for_customer(int(cid))))

    @mcp.custom_route('/api/playbooks', methods=['POST'], name='playbooks_configure')
    async def playbooks_configure(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        try:
            return JSONResponse(_with_app(lambda: configure_tenant(
                int(cid), webhook_url=data.get('webhook_url'), webhook_secret=data.get('webhook_secret'),
                disabled_playbooks=data.get('disabled_playbooks'), automation_level=data.get('automation_level'),
                kill_switch=data.get('kill_switch'))))
        except ValueError as e:
            return _bad(e)


ROUTES = ('/api/interventions', '/api/interventions/evaluate', '/api/interventions/{id}/approve',
          '/api/interventions/{id}/report', '/api/playbooks')
