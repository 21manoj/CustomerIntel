"""
Tool-call audit (G2: governed control plane). record() is called from the
two chokepoints every authenticated entry passes through:
  mcp_server.auth.require_auth_if_key_present   (every MCP tool)
  signal_engine.http._authorize                 (every HTTP route)
It never raises and never blocks the call it records.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def record(surface: str, tool: str, customer_id=None, *, key_kind: str, key_record=None, outcome: str, detail: str = None) -> None:
    """Write one audit row. Opens its own Flask app context when the caller has none.

    Over HTTP the MCP auth check runs BEFORE the tool opens its app context, so
    every MCP call failed here with "Working outside of application context", was swallowed by
    the except below, and left no row (found on production 2026-11-16). The key fields are read
    off key_record before the context is entered: it was loaded in another session."""
    try:
        from flask import has_app_context
        if has_app_context():
            _write(surface, tool, customer_id, key_kind, getattr(key_record, 'id', None),
                   getattr(key_record, 'key_prefix', None), outcome, detail)
            return
        key_id, key_prefix = getattr(key_record, 'id', None), getattr(key_record, 'key_prefix', None)
        from mcp_server.common import get_flask_app
        with get_flask_app().app_context():
            _write(surface, tool, customer_id, key_kind, key_id, key_prefix, outcome, detail)
    except Exception as e:  # pragma: no cover — audit must never break the call
        logger.error('audit record failed for %s/%s: %s', surface, tool, e)


def _write(surface, tool, customer_id, key_kind, key_id, key_prefix, outcome, detail) -> None:
    try:
        from extensions import db
        from models import ToolAuditLog
        from api_key_service import _get_caller_ip
        row = ToolAuditLog(
            transport=os.environ.get('MCP_TRANSPORT', 'stdio'), surface=surface, tool=(tool or '?')[:80],
            customer_id=int(customer_id) if customer_id not in (None, '') else None,
            key_kind=key_kind, key_id=key_id, key_prefix=key_prefix,
            caller_ip=_get_caller_ip(), outcome=outcome, detail=(detail or None) and str(detail)[:255],
        )
        db.session.add(row)
        db.session.commit()
    except Exception as e:  # pragma: no cover — audit must never break the call
        logger.error('audit record failed for %s/%s: %s', surface, tool, e)
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass


def query(customer_id=None, tool: str = None, outcome: str = None, limit: int = 100) -> list:
    from models import ToolAuditLog
    q = ToolAuditLog.query
    if customer_id is not None:
        q = q.filter_by(customer_id=int(customer_id))
    if tool:
        q = q.filter_by(tool=tool)
    if outcome:
        q = q.filter_by(outcome=outcome)
    return [{'id': r.id, 'at': r.at.isoformat() if r.at else None, 'transport': r.transport, 'surface': r.surface, 'tool': r.tool,
             'customer_id': r.customer_id, 'key_kind': r.key_kind, 'key_id': r.key_id, 'key_prefix': r.key_prefix,
             'caller_ip': r.caller_ip, 'outcome': r.outcome, 'detail': r.detail}
            for r in q.order_by(ToolAuditLog.id.desc()).limit(int(limit)).all()]
