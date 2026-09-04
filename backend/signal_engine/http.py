"""
HTTP surface for the signal engine — Starlette routes mounted on the
CustomerIntelV1 server beside /mcp (server.register_signal_routes).

  POST /api/signals/ingest/{source}   one route per pipeline.SOURCE_TYPES, JSON, Bearer key
  POST /api/signals/ingest/transcript/upload      multipart .txt/.vtt/.srt, Bearer key
  POST /api/signals/ingest/email/parse            SendGrid Inbound Parse (signature + customer toggle)
  POST /api/signals/ingest/slack/events           Slack Events API (signature + customer toggle)
  POST /api/signals/process                       run the pipeline now, Bearer key
  GET  /api/signals/review-queue?customer_id=…    Bearer key
  GET  /api/signals/status

Auth for the JSON routes: the same Bearer keys as MCP — the server key,
or a customer key with write scope for that customer (the middleware puts
the token in the auth contextvar). Webhooks carry no key; they are
signature-verified and gated by the customer's signal_engine toggle.
"""
from __future__ import annotations

import json
import logging

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def _authorize(customer_id) -> tuple:
    """(ok, error_body). Server key → ok. Customer key → must match customer and carry write scope."""
    from mcp_server.auth import extract_api_key, validate_server_key, validate_customer_key, check_scope, MCP_AUTH_REQUIRED
    raw = extract_api_key()
    if not raw:
        return (not MCP_AUTH_REQUIRED), {'error': 'Bearer API key required'}
    if validate_server_key(raw):
        return True, None
    rec = validate_customer_key(raw)
    if not rec:
        return False, {'error': 'Invalid or expired API key'}
    if customer_id is not None and int(rec.customer_id) != int(customer_id):
        return False, {'error': f'API key does not have access to customer {customer_id}'}
    if not check_scope(rec, 'write'):
        return False, {'error': 'API key lacks write scope'}
    return True, None


def _with_app(fn):
    from mcp_server.common import get_flask_app
    with get_flask_app().app_context():
        return fn()


async def _json(request):
    try:
        return await request.json()
    except Exception:
        return {}


def register_signal_routes(mcp) -> None:
    from signal_engine.ingest_api import ingest_from_payload, ingest_transcript_file, review_queue, status_payload
    from signal_engine.email_receiver import handle_inbound_email
    from signal_engine.slack_events import handle_slack_event

    from signal_engine.pipeline import SOURCE_TYPES
    for source in SOURCE_TYPES:
        def _make(src):
            async def ingest(request):
                data = await _json(request)
                ok, err = _with_app(lambda: _authorize(data.get('customer_id')))
                if not ok:
                    return JSONResponse(err, status_code=401)
                code, body = _with_app(lambda: ingest_from_payload(src, data))
                return JSONResponse(body, status_code=code)
            return ingest
        mcp.custom_route(f'/api/signals/ingest/{source}', methods=['POST'], name=f'signals_ingest_{source}')(_make(source))

    @mcp.custom_route('/api/signals/ingest/transcript/upload', methods=['POST'], name='signals_transcript_upload')
    async def transcript_upload(request):
        form = await request.form()
        f = form.get('file')
        if f is None:
            return JSONResponse({'error': 'No file uploaded. Send a .txt, .vtt, or .srt file.'}, status_code=400)
        content = (await f.read()).decode('utf-8', errors='replace')
        ok, err = _with_app(lambda: _authorize(form.get('customer_id')))
        if not ok:
            return JSONResponse(err, status_code=401)
        code, body = _with_app(lambda: ingest_transcript_file(
            f.filename, content, form.get('account_id'), form.get('customer_id'), form.get('consent_verified', ''),
            form.get('occurred_at')))
        return JSONResponse(body, status_code=code)

    @mcp.custom_route('/api/signals/ingest/email/parse', methods=['POST'], name='signals_email_parse')
    async def email_parse(request):
        raw = await request.body()
        ctype = request.headers.get('content-type', '')
        if 'json' in ctype:
            fields = json.loads(raw or b'{}')
        else:
            form = await request.form()
            fields = {k: (v if isinstance(v, str) else '') for k, v in form.items()}
        code, body = _with_app(lambda: handle_inbound_email(fields, dict(request.headers), raw, dict(request.query_params)))
        return JSONResponse(body, status_code=code)

    @mcp.custom_route('/api/signals/ingest/slack/events', methods=['POST'], name='signals_slack_events')
    async def slack_events(request):
        raw = await request.body()
        try:
            data = json.loads(raw or b'{}')
        except json.JSONDecodeError:
            data = {}
        code, body = _with_app(lambda: handle_slack_event(data, dict(request.headers), raw, dict(request.query_params)))
        return JSONResponse(body, status_code=code)

    @mcp.custom_route('/api/signals/process', methods=['POST'], name='signals_process')
    async def process(request):
        data = await _json(request)
        cid = data.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid))
        if not ok:
            return JSONResponse(err, status_code=401)
        from signal_engine.pipeline import process_pending
        res = _with_app(lambda: process_pending(customer_id=cid, limit=int(data.get('limit', 50))))
        return JSONResponse(res)

    @mcp.custom_route('/api/signals/review-queue', methods=['GET'], name='signals_review_queue')
    async def review(request):
        q = request.query_params
        cid = q.get('customer_id')
        ok, err = _with_app(lambda: _authorize(cid))
        if not ok:
            return JSONResponse(err, status_code=401)
        code, body = _with_app(lambda: review_queue(int(cid) if cid else None, q.get('account_id'), q.get('urgency'),
                                                    int(q.get('page', 1)), int(q.get('per_page', 25))))
        return JSONResponse(body, status_code=code)

    @mcp.custom_route('/api/signals/status', methods=['GET'], name='signals_status')
    async def status(request):
        return JSONResponse(status_payload())


ROUTES = (
    '/api/signals/ingest/manual', '/api/signals/ingest/email', '/api/signals/ingest/slack',
    '/api/signals/ingest/transcript', '/api/signals/ingest/ticket', '/api/signals/ingest/crm_activity',
    '/api/signals/ingest/meeting', '/api/signals/ingest/external',
    '/api/signals/ingest/transcript/upload', '/api/signals/ingest/email/parse', '/api/signals/ingest/slack/events',
    '/api/signals/process', '/api/signals/review-queue', '/api/signals/status',
)
