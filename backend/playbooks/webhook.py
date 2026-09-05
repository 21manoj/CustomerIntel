"""
The webhook: one signed JSON payload per approval, to the tenant's
configured URL, one retry, then the row shows the error (design §5).

    payload = build_payload(row, account, customer, triggers, approved_by)
    delivery = deliver(url, secret, payload)      # {status, url_host, http_status, attempts, error, at}

Signature: X-CI-Signature: sha256=HMAC_SHA256(secret, '<timestamp>.<body>'),
X-CI-Timestamp: unix seconds. The timestamp is inside the signed string so a
captured payload cannot be replayed with a fresh header.

Minimum necessary: intervention id, playbook id and action class, account
id and name, the trigger quotes with their episode ids, the approver, and
the callback. No raw communication text, no roster, no scores.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_sleep = time.sleep                      # tests replace this; the retry delay comes from config


def sign(secret: str, timestamp: str, body: str) -> str:
    mac = hmac.new(secret.encode('utf-8'), f'{timestamp}.{body}'.encode('utf-8'), hashlib.sha256).hexdigest()
    return f'sha256={mac}'


def verify(secret: str, timestamp: str, body: str, signature: str) -> bool:
    return hmac.compare_digest(sign(secret, timestamp, body), signature or '')


def build_payload(row, account, customer, triggers: list, approved_by: str, disclosure: dict) -> dict:
    from playbooks.definitions import governance
    n = governance()['webhook']['quote_chars']
    return {
        'event': 'intervention.approved',
        'intervention_id': row.id,
        'customer_id': row.customer_id,
        'playbook': {'id': row.playbook_id, 'version': row.playbook_version, 'action_class': row.action_class},
        'account': {'id': account.account_id, 'name': account.account_name, 'external_id': account.external_account_id},
        'urgency': row.urgency,
        'trigger': [{'episode_id': t['episode_id'], 'node_id': t['node_id'], 'role': t.get('role'), 'subtype': t.get('subtype'),
                     'quote': (t.get('quote') or '')[:n], 'occurred_at': t.get('occurred_at')} for t in triggers],
        'approved_by': approved_by,
        'approved_at': row.approved_at.isoformat() if row.approved_at else None,
        'expected_outcome': {'types': row.expected_outcome_types, 'window_days': row.expected_window_days},
        'callback': {'tool': 'report_intervention', 'route': f'/api/interventions/{row.id}/report',
                     'states': governance()['report_states'], 'intervention_id': row.id},
        'data_origin': {'value': disclosure.get('data_origin'), 'synthetic': disclosure.get('synthetic'),
                        'disclosure': disclosure.get('disclosure')},
    }


def deliver(url: Optional[str], secret: Optional[str], payload: dict) -> dict:
    """POST with signature; one retry after the configured delay. Never raises."""
    import httpx
    from playbooks.definitions import governance
    cfg = governance()['webhook']
    at = datetime.utcnow().isoformat()
    if not url:
        return {'status': 'not_configured', 'url_host': None, 'http_status': None, 'attempts': 0,
                'error': 'no webhook_url configured for this tenant (configure_playbooks)', 'at': at}
    if not secret:
        return {'status': 'not_configured', 'url_host': urlparse(url).hostname, 'http_status': None, 'attempts': 0,
                'error': 'no webhook_secret configured for this tenant (configure_playbooks)', 'at': at}
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    attempts = 0
    last_err, last_status = None, None
    for attempt in range(1 + int(cfg['retries'])):
        attempts += 1
        ts = str(int(time.time()))
        headers = {'Content-Type': 'application/json', cfg['signature_header']: sign(secret, ts, body),
                   cfg['timestamp_header']: ts, 'User-Agent': 'CustomerIntelV1-playbooks/1.0'}
        try:
            with httpx.Client(timeout=float(cfg['timeout_seconds'])) as client:
                r = client.post(url, content=body, headers=headers)
            last_status = r.status_code
            if 200 <= r.status_code < 300:
                return {'status': 'delivered', 'url_host': urlparse(url).hostname, 'http_status': r.status_code,
                        'attempts': attempts, 'error': None, 'at': datetime.utcnow().isoformat()}
            last_err = f'HTTP {r.status_code}: {r.text[:120]}'
        except Exception as e:      # connection refused, timeout, TLS
            last_err = f'{type(e).__name__}: {str(e)[:120]}'
        if attempt < int(cfg['retries']):
            _sleep(float(cfg['retry_delay_seconds']))
    logger.warning('webhook delivery failed after %d attempts to %s: %s', attempts, urlparse(url).hostname, last_err)
    return {'status': 'failed', 'url_host': urlparse(url).hostname, 'http_status': last_status, 'attempts': attempts,
            'error': last_err, 'at': datetime.utcnow().isoformat()}
