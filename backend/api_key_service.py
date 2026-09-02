"""
API key service — customer-scoped MCP keys.

  generate_api_key : create a key (the full key is returned exactly once)
  validate_api_key : prefix lookup + SHA-256 compare + active/expiry check
  revoke_api_key   : deactivate
  list_api_keys

Key format csp_{scope}_{random40}; only the SHA-256 hash is stored.
Ported 2026-09-02 (deployment of CustomerIntelV1) — the HTTP transport's
known gap from Tier 2A. One change: the caller IP for rate limiting comes
from a contextvar the ASGI middleware sets (server.py), not flask.request
— there is no Flask request under uvicorn. Failure tracking is per
process (in-memory), same as the old repo.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from extensions import db
from models import CustomerApiKey

logger = logging.getLogger(__name__)

_KEY_PREFIX_LEN = 12
_RANDOM_BYTES = 30
VALID_SCOPES = {'read', 'write', 'admin'}
VALID_PARTNER_TIERS = {'direct_enterprise', 'reseller', 'technology_partner', 'referral'}

_RATE_LIMIT_MAX_FAILURES = 5
_RATE_LIMIT_WINDOW_SEC = 300
_RATE_LIMIT_BLOCK_SEC = 60
_RATE_LIMIT_CLEANUP_INTERVAL = 600

_fail_tracker: dict = defaultdict(list)
_last_cleanup: float = 0.0

# Set per request by server.BearerAuthMiddleware; 'unknown' outside HTTP.
caller_ip_var: contextvars.ContextVar[str] = contextvars.ContextVar('api_key_caller_ip', default='unknown')


def _get_caller_ip() -> str:
    return caller_ip_var.get('unknown') or 'unknown'


def _cleanup_stale_entries():
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _RATE_LIMIT_CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    for ip in [ip for ip, ts in _fail_tracker.items() if not ts or ts[-1] < cutoff]:
        del _fail_tracker[ip]


def _is_rate_limited(ip: str) -> bool:
    _cleanup_stale_entries()
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    _fail_tracker[ip] = [t for t in _fail_tracker[ip] if t > cutoff]
    if len(_fail_tracker[ip]) >= _RATE_LIMIT_MAX_FAILURES:
        return now - _fail_tracker[ip][-1] < _RATE_LIMIT_BLOCK_SEC
    return False


def _record_failure(ip: str):
    _fail_tracker[ip].append(time.time())


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


def _build_key(scope: str) -> str:
    return f'csp_{scope}_{secrets.token_urlsafe(_RANDOM_BYTES)}'


def generate_api_key(customer_id: int, created_by: int, name: str, scopes: list = None,
                     expires_at: datetime = None, allowed_account_ids: list = None,
                     partner_tier: str = None) -> tuple:
    """Returns (full_key, key_record). The full key is never persisted."""
    if not name or not name.strip():
        raise ValueError('API key name is required')
    scopes = scopes or ['read']
    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        raise ValueError(f'Invalid scopes: {invalid}. Valid scopes: {VALID_SCOPES}')
    if partner_tier is not None and partner_tier not in VALID_PARTNER_TIERS:
        raise ValueError(f"Invalid partner_tier '{partner_tier}'. Valid: {sorted(VALID_PARTNER_TIERS)}")
    primary = 'admin' if 'admin' in scopes else 'write' if 'write' in scopes else 'read'
    full_key = _build_key(primary)
    record = CustomerApiKey(
        customer_id=customer_id, created_by=created_by, key_prefix=full_key[:_KEY_PREFIX_LEN],
        key_hash=_hash_key(full_key), name=name.strip(), scopes=scopes,
        allowed_account_ids=allowed_account_ids, is_active=True, expires_at=expires_at,
        partner_tier=partner_tier,
    )
    db.session.add(record)
    db.session.commit()
    logger.info('API key created: id=%s prefix=%s customer=%s scopes=%s', record.id, record.key_prefix, customer_id, scopes)
    return full_key, record


def validate_api_key(raw_key: str) -> Optional[CustomerApiKey]:
    if not raw_key or len(raw_key) < _KEY_PREFIX_LEN:
        return None
    ip = _get_caller_ip()
    if _is_rate_limited(ip):
        logger.warning('Rate limited: ip=%s', ip)
        return None
    candidate_hash = _hash_key(raw_key)
    for c in CustomerApiKey.query.filter_by(key_prefix=raw_key[:_KEY_PREFIX_LEN], is_active=True).all():
        if c.key_hash == candidate_hash:
            if c.expires_at and c.expires_at < datetime.utcnow():
                logger.info('API key id=%s expired', c.id)
                return None
            c.last_used_at = datetime.utcnow()
            c.last_used_ip = ip if ip != 'unknown' else c.last_used_ip
            db.session.commit()
            # Callers over HTTP validate BEFORE entering the tool's app
            # context (mcp_server.auth opens a temporary one), so the
            # record must carry loaded state without a session: commit()
            # expired it — reload, then detach.
            db.session.refresh(c)
            db.session.expunge(c)
            return c
    _record_failure(ip)
    logger.info('API key validation failed: prefix=%s ip=%s', raw_key[:_KEY_PREFIX_LEN], ip)
    return None


def revoke_api_key(key_id: int) -> bool:
    record = db.session.get(CustomerApiKey, key_id)
    if not record:
        return False
    if record.is_active:
        record.is_active = False
        db.session.commit()
        logger.info('API key revoked: id=%s customer=%s', record.id, record.customer_id)
    return True


def list_api_keys(customer_id: int, include_revoked: bool = False) -> list:
    q = CustomerApiKey.query.filter_by(customer_id=customer_id)
    if not include_revoked:
        q = q.filter_by(is_active=True)
    return [k.to_dict() for k in q.order_by(CustomerApiKey.created_at.desc()).all()]
