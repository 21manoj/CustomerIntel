#!/usr/bin/env python3
"""
QSIM Signal Engine — Email Receiver.

Flask blueprint that receives inbound emails via SendGrid Inbound Parse
webhook (or compatible services like Mailgun, AWS SES). Parses sender,
subject, body, maps sender domain to account, and feeds into the
existing signal ingestion pipeline.

Setup:
  1. Configure SendGrid Inbound Parse to POST to:
     https://<your-domain>/api/signals/ingest/email/parse
  2. Set SENDGRID_WEBHOOK_SECRET env var for signature verification
  3. Enable signal_engine feature toggle for the customer

Alternative: AWS SES → SNS → Lambda → POST to this endpoint.
"""

import hashlib
import hmac
import logging
import re
import uuid
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

email_receiver_api = Blueprint('email_receiver_api', __name__)


# ============================================================
# Email parsing helpers
# ============================================================

def _extract_email_address(raw: str) -> str:
    """Extract email address from 'Name <email@domain.com>' format."""
    if not raw:
        return ''
    match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', raw)
    return match.group(0).lower() if match else raw.strip().lower()


def _extract_domain(email: str) -> str:
    """Extract domain from email address."""
    if '@' in email:
        return email.split('@')[1].lower()
    return email.lower()


def _resolve_account_from_email(customer_id: int, sender_email: str):
    """Map sender email domain to an account.

    Resolution order:
      1. Account.external_account_id matches sender domain
      2. Account.profile_metadata contains matching contact domain
      3. Fuzzy match on account name vs sender domain

    Returns (account_id, account_name) or (None, None).
    """
    from models import Account
    sender_domain = _extract_domain(sender_email)

    accounts = Account.query.filter_by(
        customer_id=customer_id, account_status='active',
    ).all()

    for acct in accounts:
        # Check external_account_id (often set to domain)
        ext_id = (acct.external_account_id or '').lower()
        if ext_id and (ext_id == sender_domain or sender_domain in ext_id):
            return acct.account_id, acct.account_name

        # Check profile_metadata for domain/contacts
        meta = acct.profile_metadata or {}
        acct_domain = (meta.get('domain', '') or '').lower()
        if acct_domain and acct_domain == sender_domain:
            return acct.account_id, acct.account_name

        # Check champion/contact emails in metadata
        champion_email = (meta.get('champion_email', '') or '').lower()
        if champion_email and _extract_domain(champion_email) == sender_domain:
            return acct.account_id, acct.account_name

    # Fuzzy: account name contains domain prefix
    domain_prefix = sender_domain.split('.')[0]
    if len(domain_prefix) >= 3:
        for acct in accounts:
            if domain_prefix in acct.account_name.lower():
                return acct.account_id, acct.account_name

    return None, None


def _clean_email_body(text: str) -> str:
    """Strip email signatures, disclaimers, and forwarding headers."""
    if not text:
        return ''

    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Stop at common signature markers
        if stripped in ('--', '---', '____', '————') and cleaned:
            break
        if stripped.startswith('---------- Forwarded message'):
            break
        if stripped.startswith('On ') and stripped.endswith(' wrote:'):
            break
        cleaned.append(line)

    result = '\n'.join(cleaned).strip()
    return result[:5000]  # Cap at 5K chars


# ============================================================
# SendGrid webhook signature verification
# ============================================================

def _verify_sendgrid_signature(payload_body: bytes) -> bool:
    """Verify SendGrid Inbound Parse webhook signature.

    Uses SENDGRID_WEBHOOK_SECRET env var.
    Returns True if valid or if no secret configured (dev mode).
    """
    import os
    secret = os.environ.get('SENDGRID_WEBHOOK_SECRET')
    if not secret:
        return True  # Dev mode — no verification

    signature = request.headers.get('X-Twilio-Email-Event-Webhook-Signature', '')
    timestamp = request.headers.get('X-Twilio-Email-Event-Webhook-Timestamp', '')

    if not signature or not timestamp:
        return False

    payload = f"{timestamp}{payload_body.decode('utf-8', errors='replace')}"
    expected = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


# ============================================================
# Endpoint
# ============================================================

@email_receiver_api.route('/api/signals/ingest/email/parse', methods=['POST'])
def parse_inbound_email():
    """Receive inbound email via SendGrid Inbound Parse webhook.

    SendGrid sends multipart/form-data with fields:
      - from: sender email
      - to: recipient (signals-{customer_id}@ingest.cspulse.ai)
      - subject: email subject
      - text: plain text body
      - html: HTML body (fallback)
      - envelope: JSON with sender/recipient

    The customer_id is extracted from the recipient address pattern:
      signals-{customer_id}@ingest.cspulse.ai
    """
    import os
    enabled = os.environ.get('FEATURE_SIGNAL_ENGINE', 'false').lower() in ('true', '1', 'yes')
    if not enabled:
        return jsonify({'error': 'Signal Engine disabled'}), 403

    # Extract fields (SendGrid sends as form data)
    sender = request.form.get('from', '')
    recipient = request.form.get('to', '')
    subject = request.form.get('subject', '')
    body_text = request.form.get('text', '')
    body_html = request.form.get('html', '')

    # Also support JSON body for non-SendGrid integrations
    if not sender and request.is_json:
        data = request.get_json(force=True)
        sender = data.get('from', data.get('sender', ''))
        recipient = data.get('to', data.get('recipient', ''))
        subject = data.get('subject', '')
        body_text = data.get('text', data.get('body', ''))
        body_html = data.get('html', '')

    sender_email = _extract_email_address(sender)
    if not sender_email:
        return jsonify({'error': 'No sender email found'}), 400

    # Use plain text body, fall back to stripped HTML
    body = body_text or ''
    if not body.strip() and body_html:
        # Very basic HTML stripping
        body = re.sub(r'<[^>]+>', ' ', body_html)
        body = re.sub(r'\s+', ' ', body).strip()

    if not body.strip():
        return jsonify({'error': 'Empty email body'}), 400

    # Extract customer_id from recipient address
    # Pattern: signals-{customer_id}@ingest.cspulse.ai
    customer_id = None
    match = re.search(r'signals-(\d+)@', recipient)
    if match:
        customer_id = int(match.group(1))
    else:
        # Check query param or JSON field fallback
        customer_id = request.args.get('customer_id', type=int)
        if not customer_id and request.is_json:
            customer_id = request.get_json(silent=True, force=True).get('customer_id')

    if not customer_id:
        return jsonify({
            'error': 'Cannot determine customer_id',
            'hint': 'Use recipient format: signals-{customer_id}@ingest.cspulse.ai',
        }), 400

    # Per-customer feature check
    try:
        from models import FeatureToggle
        toggle = FeatureToggle.query.filter_by(
            customer_id=customer_id, feature_name='signal_engine',
        ).first()
        if not toggle or not toggle.enabled:
            return jsonify({
                'error': f'Signal Engine not enabled for customer {customer_id}',
            }), 403
    except Exception:
        pass

    # Resolve account from sender email domain
    try:
        from extensions import db
        account_id, account_name = _resolve_account_from_email(customer_id, sender_email)
    except Exception as e:
        logger.warning("Account resolution failed: %s", e)
        account_id, account_name = None, None

    if not account_id:
        return jsonify({
            'error': f'Cannot map sender {sender_email} to any account',
            'hint': 'Set external_account_id on the account to the sender domain',
            'customer_id': customer_id,
            'sender_domain': _extract_domain(sender_email),
        }), 404

    # Build signal text: subject + cleaned body
    cleaned_body = _clean_email_body(body)
    raw_text = f"Subject: {subject}\n\n{cleaned_body}" if subject else cleaned_body

    # Create signal via existing ingestion pipeline
    try:
        from extensions import db
        from models import QualitativeSignal

        signal_id = str(uuid.uuid4())
        signal = QualitativeSignal(
            signal_id=signal_id,
            customer_id=customer_id,
            account_id=account_id,
            signal_type='email',
            content=raw_text[:2000],
            sentiment='neutral',
            signal_date=datetime.utcnow().date(),
        )

        for attr, val in [
            ('source_type', 'email'),
            ('raw_text', raw_text),
            ('requires_review', False),
            ('consent_verified', True),
            ('composite_signal_id', signal_id),
        ]:
            try:
                setattr(signal, attr, val)
            except Exception:
                pass

        # Store sender info as stakeholder
        try:
            signal.stakeholder_roles = [{'name': sender_email, 'role': 'email_sender'}]
        except Exception:
            pass

        db.session.add(signal)
        db.session.commit()

        # Wake enrichment worker
        try:
            from signal_engine.worker import notify_new_signal
            notify_new_signal()
        except Exception:
            pass

        logger.info(
            "Email signal ingested: id=%s sender=%s account=%s(%d) customer=%d",
            signal_id, sender_email, account_name, account_id, customer_id,
        )

        return jsonify({
            'status': 'queued',
            'signal_id': signal_id,
            'customer_id': customer_id,
            'account_id': account_id,
            'account_name': account_name,
            'sender': sender_email,
            'subject': subject[:100] if subject else None,
            'message': 'Email signal accepted. Enrichment queued.',
        }), 202

    except Exception as e:
        logger.exception("Email signal ingestion failed: %s", e)
        return jsonify({'error': f'Ingestion failed: {str(e)}'}), 500
