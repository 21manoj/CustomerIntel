"""
Inbound email → signal. SendGrid Inbound Parse (multipart form) or a
plain JSON body (Mailgun, SES, anything). Framework-agnostic: the HTTP
route in signal_engine.http hands over the parsed form/JSON and headers.

Customer resolution: recipient `signals-{customer_id}@…`, else a
customer_id query param / JSON field. Account resolution: sender domain
against Account.external_account_id, profile domain, champion email, or a
name match. The sender is kept as the signal's person (resolved later by
the pipeline against the roster).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _extract_email_address(raw: str) -> str:
    if not raw:
        return ''
    m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', raw)
    return m.group(0).lower() if m else raw.strip().lower()


def _extract_domain(email: str) -> str:
    return email.split('@')[1].lower() if '@' in email else email.lower()


def _resolve_account_from_email(customer_id: int, sender_email: str):
    """(account_id, account_name) for the sender's domain, or (None, None)."""
    from models import Account
    domain = _extract_domain(sender_email)
    accounts = Account.query.filter_by(customer_id=customer_id, account_status='active').all()
    for a in accounts:
        ext = (a.external_account_id or '').lower()
        meta = a.profile_metadata or {}
        if ext and (ext == domain or domain in ext):
            return a.account_id, a.account_name
        if (meta.get('domain') or '').lower() == domain:
            return a.account_id, a.account_name
        for k in ('primary_champion_email', 'champion_email', 'csm_email'):
            if meta.get(k) and _extract_domain(meta[k].lower()) == domain and k != 'csm_email':
                return a.account_id, a.account_name
    prefix = domain.split('.')[0]
    if len(prefix) >= 3:
        for a in accounts:
            if prefix in a.account_name.lower():
                return a.account_id, a.account_name
    return None, None


def _clean_email_body(text: str) -> str:
    """Strip signatures, disclaimers and forwarded/quoted history."""
    if not text:
        return ''
    out = []
    for line in text.split('\n'):
        s = line.strip()
        if s in ('--', '---', '____', '————') and out:
            break
        if s.startswith('---------- Forwarded message') or (s.startswith('On ') and s.endswith(' wrote:')):
            break
        out.append(line)
    return '\n'.join(out).strip()[:5000]


def _verify_sendgrid_signature(payload_body: bytes, headers: Optional[dict] = None) -> bool:
    """True if valid, or if no SENDGRID_WEBHOOK_SECRET is configured (dev)."""
    secret = os.environ.get('SENDGRID_WEBHOOK_SECRET')
    if not secret:
        return True
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    signature = headers.get('x-twilio-email-event-webhook-signature', '')
    timestamp = headers.get('x-twilio-email-event-webhook-timestamp', '')
    if not signature or not timestamp:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}{payload_body.decode('utf-8', errors='replace')}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def handle_inbound_email(fields: dict, headers: Optional[dict] = None, raw_body: bytes = b'',
                         query: Optional[dict] = None) -> Tuple[int, dict]:
    """fields = SendGrid form fields or a JSON body (from/to/subject/text/html)."""
    from signal_engine.ingest_api import engine_enabled, customer_engine_enabled, ingest_from_payload
    if not engine_enabled():
        return 403, {'error': 'Signal Engine disabled'}
    if not _verify_sendgrid_signature(raw_body, headers):
        return 401, {'error': 'Invalid signature'}
    fields, query = fields or {}, query or {}
    sender = fields.get('from') or fields.get('sender') or ''
    recipient = fields.get('to') or fields.get('recipient') or ''
    subject = fields.get('subject') or ''
    body = fields.get('text') or fields.get('body') or ''
    if not body.strip() and fields.get('html'):
        body = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', fields['html'])).strip()
    sender_email = _extract_email_address(sender)
    if not sender_email:
        return 400, {'error': 'No sender email found'}
    if not body.strip():
        return 400, {'error': 'Empty email body'}
    m = re.search(r'signals-(\d+)@', recipient)
    customer_id = int(m.group(1)) if m else (query.get('customer_id') or fields.get('customer_id'))
    if not customer_id:
        return 400, {'error': 'Cannot determine customer_id', 'hint': 'Use recipient format: signals-{customer_id}@…'}
    customer_id = int(customer_id)
    if not customer_engine_enabled(customer_id):
        return 403, {'error': f'Signal Engine not enabled for customer {customer_id}'}
    account_id, account_name = _resolve_account_from_email(customer_id, sender_email)
    if not account_id:
        return 404, {'error': f'Cannot map sender {sender_email} to any account', 'customer_id': customer_id,
                     'sender_domain': _extract_domain(sender_email),
                     'hint': 'Set external_account_id on the account to the sender domain'}
    cleaned = _clean_email_body(body)
    raw_text = f'Subject: {subject}\n\n{cleaned}' if subject else cleaned
    code, res = ingest_from_payload('email', {
        'account_id': account_id, 'customer_id': customer_id, 'raw_text': raw_text,
        'occurred_at': fields.get('date') or fields.get('occurred_at'),
        'participant_list': [{'name': sender_email, 'role': 'email_sender'}],
        'source_ref': fields.get('message_id') or fields.get('Message-ID')})
    res.update({'account_name': account_name, 'sender': sender_email, 'subject': subject[:100] or None})
    return code, res
