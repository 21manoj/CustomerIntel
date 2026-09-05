"""
Adapter routes (Starlette, mounted beside /mcp).

  POST /api/sources/{source}/import     write scope
        JSON      {customer_id, content, process_now?, dry_run?}
        multipart file=<export.csv> customer_id=… process_now=… dry_run=…
  GET  /api/sources                      read scope (customer_id=…) — the registered sources and their columns

Auth: the same Bearer keys as MCP (signal_engine.http._authorize).
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from signal_engine.http import _authorize, _with_app, _json


def _flag(v, default: bool) -> bool:
    if v is None or v == '':
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('1', 'true', 'yes')


def register_adapter_routes(mcp) -> None:
    from adapters.sources import import_from_source, describe, SOURCES

    @mcp.custom_route('/api/sources', methods=['GET'], name='sources_list')
    async def sources_list(request):
        cid = request.query_params.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'read'))
        if not ok:
            return JSONResponse(err, status_code=401)
        return JSONResponse({'sources': [describe(s) for s in sorted(SOURCES)]})

    @mcp.custom_route('/api/sources/{source}/import', methods=['POST'], name='sources_import')
    async def sources_import(request):
        ctype = request.headers.get('content-type', '')
        if ctype.startswith('multipart/form-data'):
            form = await request.form()
            f = form.get('file')
            if f is None:
                return JSONResponse({'error': 'No file uploaded. Send the export as `file`.'}, status_code=400)
            data = {'customer_id': form.get('customer_id'), 'content': (await f.read()).decode('utf-8', errors='replace'),
                    'process_now': form.get('process_now'), 'dry_run': form.get('dry_run')}
        else:
            data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid, 'write'))
        if not ok:
            return JSONResponse(err, status_code=401)
        if not cid:
            return JSONResponse({'error': 'customer_id is required'}, status_code=400)
        try:
            return JSONResponse(_with_app(lambda: import_from_source(
                int(cid), request.path_params['source'], data.get('content') or '',
                process_now=_flag(data.get('process_now'), True), dry_run=_flag(data.get('dry_run'), False))))
        except ValueError as e:
            return JSONResponse({'error': str(e)}, status_code=400)


ROUTES = ('/api/sources', '/api/sources/{source}/import')
