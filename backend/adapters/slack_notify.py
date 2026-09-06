"""
Slack notify adapter — the optional second delivery for notify-class interventions
(docs/design/adapters.md §2.3).

    validate_url(url)                    scheme + host rule (https, hooks.slack.com; plain http only under the
                                         governance layer's insecure-http env, for a local fake endpoint)
    post(url, row, account, playbook) → {status, http_status, error, at}

Minimal message: account, playbook, the trigger quote, the intervention id. No scores, no raw
communication text, no roster. The result goes into delivery.slack; delivery.status (the workflow
engine's delivery) is untouched, so nothing that reads delivery_problem changes meaning.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from adapters import settings

logger = logging.getLogger(__name__)


def applies_to(action_class: str) -> bool:
    return action_class in settings.get('slack', 'action_classes')


def validate_url(url: str) -> str:
    """The Slack incoming-webhook URL is a secret and a target: https on an allowed host, or plain
    http on any host only when the governance layer's insecure-http env is set (tests, a local fake)."""
    from playbooks.definitions import insecure_http_allowed
    url = (url or '').strip()
    u = urlparse(url)
    hosts = settings.get('slack', 'allowed_hosts')
    if u.scheme == 'https' and u.netloc and u.hostname in hosts:
        return url
    if u.scheme in ('http', 'https') and u.netloc and insecure_http_allowed():
        return url
    raise ValueError(f'slack_webhook_url must be an https URL on {"/".join(hosts)}')


def build_message(row, account, playbook: dict) -> dict:
    n = int(settings.get('slack', 'quote_chars'))
    quote = (row.trigger_quote or '').strip()[:n]
    name = getattr(account, 'account_name', None) or f'account {row.account_id}'
    label = playbook.get('label') or row.playbook_id
    text = (f"Intervention #{row.id} — {label} — {name}\n"
            f"Playbook: {row.playbook_id} ({row.action_class}), urgency {row.urgency or 'n/a'}\n"
            f"Evidence: \"{quote}\"\n"
            f"Report back: report_intervention(intervention_id={row.id})")
    return {'text': text}


def post(url: Optional[str], row, account, playbook: dict) -> dict:
    """POST Slack's incoming-webhook format. One attempt, never raises."""
    import httpx
    at = datetime.utcnow().isoformat()
    if not url:
        return {'status': 'not_configured', 'http_status': None, 'error': None, 'at': at}
    http_status = None
    try:
        with httpx.Client(timeout=float(settings.get('slack', 'timeout_seconds'))) as client:
            r = client.post(url, json=build_message(row, account, playbook),
                            headers={'User-Agent': settings.get('slack', 'user_agent')})
        http_status = r.status_code
        if 200 <= r.status_code < 300:
            return {'status': 'delivered', 'http_status': r.status_code, 'error': None, 'at': datetime.utcnow().isoformat()}
        err = f'HTTP {r.status_code}: {r.text[:120]}'
    except Exception as e:                # connection refused, timeout, TLS
        err = f'{type(e).__name__}: {str(e)[:120]}'
    logger.warning('slack notify for intervention #%s failed: %s', row.id, err)
    return {'status': 'failed', 'http_status': http_status, 'error': err, 'at': datetime.utcnow().isoformat()}
