"""
Slack Events API → signal. Framework-agnostic: the HTTP route in
signal_engine.http hands over the JSON, the raw body (for the signature)
and the headers.

Setup: a Slack app subscribed to message.channels / message.groups,
request URL https://<host>/api/signals/ingest/slack/events. Map the
workspace and channels on the customer's signal_engine toggle:
    {"slack_team_id": "T0…", "slack_channel_map": {"C04…": <account_id>}}
Channels named #cs-<account-slug> resolve by convention.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _verify_slack_signature(headers: Optional[dict] = None, raw_body: bytes = b'') -> bool:
    """True if valid, or if no SLACK_SIGNING_SECRET is configured (dev)."""
    secret = os.environ.get('SLACK_SIGNING_SECRET')
    if not secret:
        return True
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    timestamp, signature = headers.get('x-slack-request-timestamp', ''), headers.get('x-slack-signature', '')
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    computed = 'v0=' + hmac.new(secret.encode(), f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}".encode(),
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def _resolve_account_from_channel(customer_id: int, channel_id: str, channel_name: Optional[str] = None):
    from models import FeatureToggle, Account
    toggle = FeatureToggle.query.filter_by(customer_id=customer_id, feature_name='signal_engine').first()
    if toggle and toggle.config:
        mapped = (toggle.config.get('slack_channel_map') or {}).get(channel_id)
        if mapped:
            a = Account.query.filter_by(customer_id=customer_id, account_id=int(mapped)).first()
            if a:
                return a.account_id, a.account_name
    if channel_name:
        name = channel_name.lower()
        for prefix in ('cs-', 'customer-', 'acct-', 'account-'):
            if name.startswith(prefix):
                slug = name[len(prefix):]
                for a in Account.query.filter_by(customer_id=customer_id, account_status='active').all():
                    s = a.account_name.lower().replace(' ', '-').replace('_', '-')
                    if slug == s or slug in s or s in slug:
                        return a.account_id, a.account_name
                break
    return None, None


def _resolve_customer_from_team(team_id: str):
    from models import FeatureToggle
    for t in FeatureToggle.query.filter_by(feature_name='signal_engine', enabled=True).all():
        if t.config and t.config.get('slack_team_id') == team_id:
            return t.customer_id
    return None


def handle_slack_event(data: dict, headers: Optional[dict] = None, raw_body: bytes = b'',
                       query: Optional[dict] = None) -> Tuple[int, dict]:
    from signal_engine.ingest_api import engine_enabled, ingest_from_payload
    if not engine_enabled():
        return 403, {'error': 'Signal Engine disabled'}
    if not _verify_slack_signature(headers, raw_body):
        return 401, {'error': 'Invalid signature'}
    data, query = data or {}, query or {}
    if data.get('type') == 'url_verification':
        return 200, {'challenge': data.get('challenge', '')}
    if data.get('type') != 'event_callback':
        return 200, {'ok': True}
    ev = data.get('event') or {}
    if ev.get('type') != 'message' or ev.get('bot_id') or ev.get('subtype') in ('bot_message', 'message_changed', 'message_deleted'):
        return 200, {'ok': True}
    text = (ev.get('text') or '').strip()
    if len(text) < 10:
        return 200, {'ok': True}
    customer_id = _resolve_customer_from_team(data.get('team_id', '')) or query.get('customer_id')
    if not customer_id:
        logger.warning('Slack event from unknown team %s — set slack_team_id on the signal_engine toggle', data.get('team_id'))
        return 200, {'ok': True, 'ignored': 'unknown team'}
    account_id, account_name = _resolve_account_from_channel(int(customer_id), ev.get('channel', ''), ev.get('channel_name'))
    if not account_id:
        return 200, {'ok': True, 'ignored': 'unmapped channel'}
    ts = ev.get('ts')
    occurred = datetime.utcfromtimestamp(float(ts)).isoformat() if ts else None
    code, res = ingest_from_payload('slack', {
        'account_id': account_id, 'customer_id': int(customer_id), 'raw_text': text, 'occurred_at': occurred,
        'participant_list': [{'name': ev.get('user_name') or ev.get('user') or 'slack_user', 'role': 'slack_user'}],
        'source_ref': f"{ev.get('channel')}:{ts}"})
    # Slack expects 200 within 3 s regardless of what we did with it
    return 200, {'ok': True, 'result': res, 'account_name': account_name}
