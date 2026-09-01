#!/usr/bin/env python3
"""
QSIM Signal Engine — Slack Events Handler.

Flask blueprint that receives Slack Events API webhooks (message.channels)
and feeds them into the signal ingestion pipeline.

Setup:
  1. Create a Slack App at api.slack.com/apps
  2. Enable Event Subscriptions with Request URL:
     https://<your-domain>/api/signals/ingest/slack/events
  3. Subscribe to bot events: message.channels, message.groups
  4. Install app to workspace, invite bot to monitored channels
  5. Set env vars: SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN
  6. Configure channel→account mapping in FeatureToggle.config:
     enable_features(customer_id, ['signal_engine'])
     then set config: {"slack_channel_map": {"C04ABC123": account_id}}

Channel → Account mapping:
  Option A: FeatureToggle config (per-customer)
  Option B: Channel naming convention: #cs-{account_name}
  Option C: Manual mapping via MCP tool (future)
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

slack_events_api = Blueprint('slack_events_api', __name__)


# ============================================================
# Slack signature verification
# ============================================================

def _verify_slack_signature() -> bool:
    """Verify Slack request signature using signing secret.

    Returns True if valid or if no signing secret configured (dev mode).
    """
    import os
    signing_secret = os.environ.get('SLACK_SIGNING_SECRET')
    if not signing_secret:
        return True  # Dev mode

    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    signature = request.headers.get('X-Slack-Signature', '')

    if not timestamp or not signature:
        return False

    # Reject requests older than 5 minutes (replay attack prevention)
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{request.get_data(as_text=True)}"
    computed = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature)


# ============================================================
# Channel → Account resolution
# ============================================================

def _resolve_account_from_channel(customer_id: int, channel_id: str, channel_name: str = None):
    """Map Slack channel to account_id.

    Resolution order:
      1. FeatureToggle config: {"slack_channel_map": {"C04ABC": 123}}
      2. Channel naming convention: #cs-{account_name_slug}
      3. Returns (None, None) if no match

    Returns (account_id, account_name) or (None, None).
    """
    from models import FeatureToggle, Account

    # Option A: Explicit mapping in feature toggle config
    try:
        toggle = FeatureToggle.query.filter_by(
            customer_id=customer_id, feature_name='signal_engine',
        ).first()
        if toggle and toggle.config:
            channel_map = toggle.config.get('slack_channel_map', {})
            mapped_id = channel_map.get(channel_id)
            if mapped_id:
                acct = Account.query.filter_by(
                    customer_id=customer_id, account_id=int(mapped_id),
                ).first()
                if acct:
                    return acct.account_id, acct.account_name
    except Exception as e:
        logger.debug("Channel map lookup failed: %s", e)

    # Option B: Channel naming convention #cs-{slug}
    if channel_name:
        name_lower = channel_name.lower()
        # Strip common prefixes
        for prefix in ('cs-', 'customer-', 'acct-', 'account-'):
            if name_lower.startswith(prefix):
                slug = name_lower[len(prefix):]
                accounts = Account.query.filter_by(
                    customer_id=customer_id, account_status='active',
                ).all()
                for acct in accounts:
                    acct_slug = acct.account_name.lower().replace(' ', '-').replace('_', '-')
                    if slug == acct_slug or slug in acct_slug or acct_slug in slug:
                        return acct.account_id, acct.account_name
                break

    return None, None


def _resolve_customer_from_team(team_id: str):
    """Map Slack team_id (workspace) to customer_id.

    Looks for customers with signal_engine enabled and
    slack_team_id in their feature toggle config.

    Returns customer_id or None.
    """
    from models import FeatureToggle
    try:
        toggles = FeatureToggle.query.filter_by(
            feature_name='signal_engine', enabled=True,
        ).all()
        for t in toggles:
            if t.config and t.config.get('slack_team_id') == team_id:
                return t.customer_id
    except Exception:
        pass
    return None


# ============================================================
# Endpoint
# ============================================================

@slack_events_api.route('/api/signals/ingest/slack/events', methods=['POST'])
def handle_slack_event():
    """Handle Slack Events API webhook.

    Handles two types of requests:
      1. URL Verification Challenge (required by Slack during setup)
      2. Event Callbacks (message.channels, message.groups)

    POST body (challenge):
    {
        "type": "url_verification",
        "challenge": "abc123...",
        "token": "..."
    }

    POST body (event):
    {
        "type": "event_callback",
        "team_id": "T0ABC123",
        "event": {
            "type": "message",
            "channel": "C04ABC123",
            "user": "U0ABC123",
            "text": "Customer X is evaluating alternatives...",
            "ts": "1234567890.123456"
        }
    }
    """
    import os
    enabled = os.environ.get('FEATURE_SIGNAL_ENGINE', 'false').lower() in ('true', '1', 'yes')
    if not enabled:
        return jsonify({'error': 'Signal Engine disabled'}), 403

    # Verify Slack signature
    if not _verify_slack_signature():
        return jsonify({'error': 'Invalid signature'}), 401

    data = request.get_json(force=True)
    event_type = data.get('type', '')

    # Handle URL verification challenge
    if event_type == 'url_verification':
        challenge = data.get('challenge', '')
        return jsonify({'challenge': challenge})

    # Handle event callbacks
    if event_type != 'event_callback':
        return jsonify({'ok': True})  # Acknowledge unknown types

    event = data.get('event', {})
    msg_type = event.get('type', '')

    # Only process message events (not reactions, joins, etc.)
    if msg_type != 'message':
        return jsonify({'ok': True})

    # Skip bot messages, edits, and thread replies (for now)
    if event.get('bot_id') or event.get('subtype') in ('bot_message', 'message_changed', 'message_deleted'):
        return jsonify({'ok': True})

    text = event.get('text', '').strip()
    if not text or len(text) < 10:  # Skip very short messages
        return jsonify({'ok': True})

    channel_id = event.get('channel', '')
    team_id = data.get('team_id', '')
    user_id = event.get('user', '')
    ts = event.get('ts', '')

    # Resolve customer from Slack workspace
    customer_id = _resolve_customer_from_team(team_id)

    # Fallback: check query param or request header
    if not customer_id:
        customer_id = request.args.get('customer_id', type=int)

    if not customer_id:
        logger.warning(
            "Slack event from unknown team %s — configure slack_team_id in signal_engine feature toggle",
            team_id,
        )
        return jsonify({'ok': True})  # Acknowledge but don't process

    # Resolve account from channel
    try:
        from extensions import db
        channel_name = event.get('channel_name', '')
        account_id, account_name = _resolve_account_from_channel(
            customer_id, channel_id, channel_name,
        )
    except Exception as e:
        logger.warning("Account resolution failed for channel %s: %s", channel_id, e)
        account_id, account_name = None, None

    if not account_id:
        logger.debug(
            "Slack message from unmapped channel %s (team %s, customer %d)",
            channel_id, team_id, customer_id,
        )
        return jsonify({'ok': True})  # Acknowledge but don't process

    # Create signal
    try:
        from extensions import db
        from models import QualitativeSignal

        signal_id = str(uuid.uuid4())
        signal = QualitativeSignal(
            signal_id=signal_id,
            customer_id=customer_id,
            account_id=account_id,
            signal_type='slack',
            content=text[:2000],
            sentiment='neutral',
            signal_date=datetime.utcnow().date(),
        )

        for attr, val in [
            ('source_type', 'slack'),
            ('raw_text', text),
            ('requires_review', False),
            ('consent_verified', True),
            ('composite_signal_id', signal_id),
        ]:
            try:
                setattr(signal, attr, val)
            except Exception:
                pass

        # Store Slack user as stakeholder
        try:
            signal.stakeholder_roles = [{'name': user_id, 'role': 'slack_user'}]
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
            "Slack signal ingested: id=%s channel=%s account=%s(%d) customer=%d",
            signal_id, channel_id, account_name, account_id, customer_id,
        )

        # Slack expects 200 OK within 3 seconds
        return jsonify({'ok': True})

    except Exception as e:
        logger.exception("Slack signal ingestion failed: %s", e)
        return jsonify({'ok': True})  # Still return 200 to avoid Slack retries


# ============================================================
# Channel mapping management
# ============================================================

@slack_events_api.route('/api/signals/slack/channels', methods=['GET'])
def list_channel_mappings():
    """List configured Slack channel → account mappings for a customer."""
    import os
    enabled = os.environ.get('FEATURE_SIGNAL_ENGINE', 'false').lower() in ('true', '1', 'yes')
    if not enabled:
        return jsonify({'error': 'Signal Engine disabled'}), 403

    customer_id = request.args.get('customer_id', type=int)
    if not customer_id:
        return jsonify({'error': 'customer_id required'}), 400

    try:
        from models import FeatureToggle
        toggle = FeatureToggle.query.filter_by(
            customer_id=customer_id, feature_name='signal_engine',
        ).first()
        config = toggle.config if toggle else {}
        return jsonify({
            'customer_id': customer_id,
            'slack_team_id': config.get('slack_team_id'),
            'slack_channel_map': config.get('slack_channel_map', {}),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@slack_events_api.route('/api/signals/slack/channels', methods=['POST'])
def update_channel_mapping():
    """Add or update a Slack channel → account mapping.

    POST body:
    {
        "customer_id": 407,
        "channel_id": "C04ABC123",
        "account_id": 301,
        "slack_team_id": "T0ABC123"  (optional, set once)
    }
    """
    import os
    enabled = os.environ.get('FEATURE_SIGNAL_ENGINE', 'false').lower() in ('true', '1', 'yes')
    if not enabled:
        return jsonify({'error': 'Signal Engine disabled'}), 403

    data = request.get_json(force=True)
    customer_id = data.get('customer_id')
    channel_id = data.get('channel_id')
    account_id = data.get('account_id')

    if not all([customer_id, channel_id, account_id]):
        return jsonify({'error': 'customer_id, channel_id, and account_id required'}), 400

    try:
        from extensions import db
        from models import FeatureToggle

        toggle = FeatureToggle.query.filter_by(
            customer_id=customer_id, feature_name='signal_engine',
        ).first()

        if not toggle:
            return jsonify({'error': 'Signal Engine not enabled for this customer'}), 403

        config = toggle.config or {}
        channel_map = config.get('slack_channel_map', {})
        channel_map[channel_id] = account_id
        config['slack_channel_map'] = channel_map

        # Optionally set team_id
        team_id = data.get('slack_team_id')
        if team_id:
            config['slack_team_id'] = team_id

        toggle.config = config
        db.session.commit()

        return jsonify({
            'status': 'updated',
            'customer_id': customer_id,
            'channel_id': channel_id,
            'account_id': account_id,
            'slack_channel_map': channel_map,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500
