"""
CustomerIntelV1 — HTTP server.

One uvicorn process serving the FastMCP server over streamable-HTTP at
/mcp plus a /health route. Flask exists only to give SQLAlchemy an app
context (mcp_server.common.get_flask_app); nothing is served by Flask.

Environment:
  DATABASE_URL          postgresql://...            (required)
  MCP_SERVER_API_KEY    super-admin Bearer key       (recommended)
  MCP_AUTH_REQUIRED     true|false (default true; onboarding tools stay frictionless)
  MCP_ALLOW_QUERY_KEY   true|false (default false) — accept ?api_key= for single-URL connectors; keys in URLs reach access logs
  SIGNAL_WORKER         true|false (default true) — background signal processing
  FEATURE_SIGNAL_ENGINE true|false (default true) — the /api/signals/* surface
  PORT                  default 8101
  GIT_SHA / BUILD_TIME  surfaced by /health

Schema: db.create_all() at boot — additive only. There is no migration
tool in this build yet; the first deployment starts from an empty DB.

    python server.py                       # serve
    python -c "from server import build_asgi_app"   # tests / ASGI hosts
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger('customerintel.server')
ALLOW_QUERY_KEY = os.environ.get('MCP_ALLOW_QUERY_KEY', 'false').lower() in ('true', '1', 'yes')
VERSION = '0.1.0'
SERVER_NAME = 'CustomerIntelV1'


class BearerAuthMiddleware:
    """Puts the request's Bearer token (or ?api_key= when MCP_ALLOW_QUERY_KEY
    is set — single-URL connectors, demo keys only) into the contextvars that
    mcp_server.auth reads, keyed by mcp-session-id for async propagation,
    plus the caller IP for api_key_service's rate limiting."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        from mcp_server.auth import _current_api_key_var, _current_session_id_var, _session_api_keys
        from api_key_service import caller_ip_var
        headers = dict(scope.get('headers', []))
        auth = headers.get(b'authorization', b'').decode()
        session_id = headers.get(b'mcp-session-id', b'').decode()
        token = ''
        if auth.startswith('Bearer '):
            token = auth[7:].strip()
            if session_id:
                _session_api_keys[session_id] = token
        elif session_id and session_id in _session_api_keys:
            token = _session_api_keys[session_id]
        if not token and ALLOW_QUERY_KEY:
            # Off by default: a key in the URL lands in Caddy/uvicorn access logs and browser history.
            qs = scope.get('query_string', b'').decode()
            if qs:
                p = parse_qs(qs)
                token = (p.get('api_key', [''])[0] or p.get('token', [''])[0]).strip()
                if token:
                    logger.warning('API key accepted from the query string (MCP_ALLOW_QUERY_KEY=true) for %s', scope.get('path'))
                    if session_id:
                        _session_api_keys[session_id] = token
        fwd = headers.get(b'x-forwarded-for', b'').decode()
        client = scope.get('client') or ('unknown', 0)
        ip = (fwd.split(',')[0].strip() if fwd else client[0]) or 'unknown'

        tokens = [caller_ip_var.set(ip)]
        if token:
            tokens.append(_current_api_key_var.set(token))
        if session_id:
            tokens.append(_current_session_id_var.set(session_id))
        try:
            await self.app(scope, receive, send)
        finally:
            caller_ip_var.reset(tokens[0])
            if token:
                _current_api_key_var.reset(tokens[1])
            if session_id:
                _current_session_id_var.reset(tokens[-1])


def build_asgi_app(database_url: str | None = None, create_schema: bool = True):
    """Build the ASGI app: Flask app context for the DB, tool registration, /health, MCP at /mcp."""
    os.environ['MCP_TRANSPORT'] = 'http'
    if database_url:
        os.environ['DATABASE_URL'] = database_url

    from starlette.responses import JSONResponse
    import mcp_server.common as _common
    from mcp_server.cs_pulse_mcp_server import mcp
    import mcp_server.cs_pulse_onboarding  # noqa: F401 — registers the tools
    import models  # noqa: F401 — metadata for create_all

    app = _common.get_flask_app()
    if create_schema:
        from extensions import db
        with app.app_context():
            db.create_all()

    @mcp.custom_route('/health', methods=['GET'])
    async def health(request):
        from extensions import db
        from sqlalchemy import text
        from models import Customer, JourneyData, WizardRun
        try:
            with app.app_context():
                db.session.execute(text('select 1'))
                from journeys.wizard_a import stale_journey_query, GENERATOR_VERSION
                counts = {
                    'customers': Customer.query.count(),
                    'journeys': JourneyData.query.count(),
                    'stale_journeys': stale_journey_query().count(),   # behind GENERATOR_VERSION; deploy rebuilds them
                    'wizard_runs': WizardRun.query.count(),
                }
            status, db_ok = 200, True
        except Exception as e:  # pragma: no cover — only on a broken DB
            counts, status, db_ok = {'error': str(e)[:200]}, 503, False
        return JSONResponse({
            'server': SERVER_NAME, 'version': VERSION, 'status': 'ok' if db_ok else 'degraded',
            'db': db_ok, 'counts': counts, 'journey_generator_version': GENERATOR_VERSION if db_ok else None,
            'git_sha': os.environ.get('GIT_SHA'), 'build_time': os.environ.get('BUILD_TIME'),
            'mcp_path': '/mcp', 'auth_required': os.environ.get('MCP_AUTH_REQUIRED', 'true'),
            'time': datetime.utcnow().isoformat() + 'Z',
        }, status_code=status)

    @mcp.custom_route('/', methods=['GET'])
    async def root(request):
        return JSONResponse({'server': SERVER_NAME, 'version': VERSION, 'mcp': '/mcp', 'health': '/health'})

    # Signal engine v2: ingest / webhook / process / status routes beside /mcp
    from signal_engine.http import register_signal_routes
    register_signal_routes(mcp)
    from journeys.http import register_journey_routes
    register_journey_routes(mcp)          # read surface: journeys + evidence
    from ask_ai.http import register_ask_routes
    register_ask_routes(mcp)              # POST /api/ask — Ask AI over the journey contract (P10)
    if create_schema:
        from signal_engine.models import ensure_enrichment_columns
        from utils.schema_additive import ensure_additive_columns
        with app.app_context():
            ensure_enrichment_columns(db.engine)   # additive, idempotent (content_hash, occurred_at on existing DBs)
            ensure_additive_columns(db.engine)     # lineage / provenance columns on existing tables

    asgi = mcp.http_app(path='/mcp')
    return BearerAuthMiddleware(asgi)


def main():
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    import uvicorn
    from dotenv import load_dotenv
    load_dotenv()
    if not os.environ.get('DATABASE_URL'):
        raise SystemExit('DATABASE_URL is required')
    if os.environ.get('MCP_AUTH_REQUIRED', 'true').lower() in ('true', '1', 'yes') and not os.environ.get('MCP_SERVER_API_KEY'):
        logger.warning('MCP_SERVER_API_KEY is not set — only customer-scoped keys (create_customer) will work over HTTP')
    app = build_asgi_app()
    if os.environ.get('SIGNAL_WORKER', 'true').lower() in ('true', '1', 'yes'):
        from signal_engine.worker import SignalEnrichmentWorker
        SignalEnrichmentWorker().start()
    port = int(os.environ.get('PORT', '8101'))
    logger.info('%s %s serving MCP at /mcp on 0.0.0.0:%d (sha=%s)', SERVER_NAME, VERSION, port, os.environ.get('GIT_SHA'))
    uvicorn.run(app, host='0.0.0.0', port=port, log_level=os.environ.get('LOG_LEVEL', 'info').lower())


if __name__ == '__main__':
    main()
