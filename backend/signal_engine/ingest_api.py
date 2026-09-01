#!/usr/bin/env python3
"""
QSIM Signal Engine — Ingestion API.

Flask blueprint providing webhook endpoints for signal ingestion:
  POST /api/signals/ingest/slack
  POST /api/signals/ingest/email
  POST /api/signals/ingest/transcript
  POST /api/signals/ingest/manual
  GET  /api/signals/review-queue

All endpoints are feature-toggled: require FEATURE_SIGNAL_ENGINE=true
AND per-customer signal_engine feature enabled.

Consent: Transcript ingestion requires consent_verified=true in payload.
"""

import logging
import uuid
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

signal_api = Blueprint('signal_api', __name__)


# ============================================================
# Feature toggle guard
# ============================================================

def _require_signal_engine(f):
    """Decorator: requires FEATURE_SIGNAL_ENGINE=true."""
    @wraps(f)
    def decorated(*args, **kwargs):
        import os
        enabled = os.environ.get('FEATURE_SIGNAL_ENGINE', 'false').lower() in ('true', '1', 'yes')
        if not enabled:
            return jsonify({
                'error': 'Signal Engine is disabled',
                'hint': 'Set FEATURE_SIGNAL_ENGINE=true to enable',
            }), 403
        return f(*args, **kwargs)
    return decorated


def _check_customer_signal_engine(customer_id: int) -> bool:
    """Check per-customer signal_engine feature toggle in DB."""
    try:
        from models import FeatureToggle as FTModel
        from extensions import db
        toggle = FTModel.query.filter_by(
            customer_id=customer_id,
            feature_name='signal_engine',
        ).first()
        return toggle.enabled if toggle else False
    except Exception:
        return False


# ============================================================
# Shared ingest logic
# ============================================================

def _ingest_signal(source_type: str):
    """Common ingestion handler for all source types.

    Validates payload, creates QualitativeSignal record,
    queues for LLM enrichment (async).

    Returns 202 Accepted with raw_signal_id.
    """
    data = request.get_json(force=True)

    # Required fields
    account_id = data.get('account_id')
    customer_id = data.get('customer_id')
    raw_text = data.get('raw_text', '').strip()

    if not account_id or not customer_id:
        return jsonify({'error': 'account_id and customer_id are required'}), 400
    if not raw_text:
        return jsonify({'error': 'raw_text is required'}), 400

    # Per-customer feature check
    if not _check_customer_signal_engine(customer_id):
        return jsonify({
            'error': f'Signal Engine not enabled for customer {customer_id}',
            'hint': 'Enable via enable_features(customer_id, ["signal_engine"])',
        }), 403

    # Consent check for transcripts
    if source_type == 'transcript':
        if not data.get('consent_verified', False):
            return jsonify({
                'error': 'Transcript ingestion requires consent_verified=true',
                'hint': 'Verify participant consent before submitting transcript data',
            }), 400

    # Generate signal ID
    signal_id = str(uuid.uuid4())
    timestamp = data.get('timestamp', datetime.utcnow().isoformat())

    try:
        from extensions import db
        from models import QualitativeSignal

        # Parse timestamp
        if isinstance(timestamp, str):
            try:
                parsed_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).date()
            except ValueError:
                parsed_date = datetime.utcnow().date()
        else:
            parsed_date = datetime.utcnow().date()

        # Create raw signal record
        signal = QualitativeSignal(
            signal_id=signal_id,
            customer_id=int(customer_id),
            account_id=int(account_id),
            signal_type=source_type,
            content=raw_text[:2000],  # Truncate for safety
            sentiment='neutral',  # Placeholder — enrichment will update
            signal_date=parsed_date,
        )

        # Set QSIM enrichment columns (if migration has run)
        for attr, val in [
            ('source_type', source_type),
            ('raw_text', raw_text),
            ('requires_review', False),
            ('consent_verified', data.get('consent_verified', source_type != 'transcript')),
            ('composite_signal_id', signal_id),
        ]:
            try:
                setattr(signal, attr, val)
            except Exception:
                pass  # Column not yet migrated

        # Store participant list as stakeholder_roles
        participants = data.get('participant_list', [])
        if participants:
            try:
                signal.stakeholder_roles = participants
            except Exception:
                pass

        db.session.add(signal)
        db.session.commit()

        logger.info(
            "QSIM signal ingested: id=%s source=%s account=%s customer=%s",
            signal_id, source_type, account_id, customer_id,
        )

        # Wake the background enrichment worker immediately
        try:
            from signal_engine.worker import notify_new_signal
            notify_new_signal()
        except Exception:
            pass  # Worker may not be started yet

        return jsonify({
            'raw_signal_id': signal_id,
            'signal_db_id': signal.signal_id,
            'status': 'queued',
            'source_type': source_type,
            'account_id': account_id,
            'customer_id': customer_id,
            'message': 'Signal accepted. Enrichment will run asynchronously.',
        }), 202

    except Exception as e:
        logger.exception("Signal ingestion failed: %s", e)
        return jsonify({'error': f'Ingestion failed: {str(e)}'}), 500


# ============================================================
# Endpoints
# ============================================================

@signal_api.route('/api/signals/ingest/slack', methods=['POST'])
@_require_signal_engine
def ingest_slack():
    """Ingest a signal from Slack.

    POST body:
    {
        "source_type": "slack",
        "account_id": 301,
        "customer_id": 407,
        "timestamp": "2026-03-21T10:30:00Z",
        "raw_text": "Hey team, we're evaluating alternatives...",
        "participant_list": [{"name": "Jane Doe", "role": "VP Infrastructure"}],
        "thread_or_channel_id": "C04ABCD1234"
    }
    """
    return _ingest_signal('slack')


@signal_api.route('/api/signals/ingest/email', methods=['POST'])
@_require_signal_engine
def ingest_email():
    """Ingest a signal from email."""
    return _ingest_signal('email')


@signal_api.route('/api/signals/ingest/transcript', methods=['POST'])
@_require_signal_engine
def ingest_transcript():
    """Ingest a signal from a call transcript.

    REQUIRES consent_verified=true in payload.
    """
    return _ingest_signal('transcript')


@signal_api.route('/api/signals/ingest/manual', methods=['POST'])
@_require_signal_engine
def ingest_manual():
    """Ingest a manually-entered signal (CSM observation)."""
    return _ingest_signal('manual')


# ============================================================
# Transcript File Upload
# ============================================================

def _parse_vtt(content: str) -> str:
    """Parse WebVTT (.vtt) format, stripping timestamps and headers."""
    import re
    lines = content.split('\n')
    text_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip WEBVTT header, NOTE blocks, empty lines
        if stripped.startswith('WEBVTT') or stripped.startswith('NOTE'):
            continue
        # Skip timestamp lines: 00:00:01.000 --> 00:00:04.000
        if re.match(r'\d{2}:\d{2}:\d{2}', stripped):
            continue
        # Skip cue identifiers (pure numbers)
        if stripped.isdigit():
            continue
        if stripped:
            text_lines.append(stripped)
    return ' '.join(text_lines)


def _parse_srt(content: str) -> str:
    """Parse SubRip (.srt) format, stripping timestamps and sequence numbers."""
    import re
    lines = content.split('\n')
    text_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip sequence numbers
        if stripped.isdigit():
            continue
        # Skip timestamp lines: 00:00:01,000 --> 00:00:04,000
        if re.match(r'\d{2}:\d{2}:\d{2},\d{3}', stripped):
            continue
        if stripped:
            text_lines.append(stripped)
    return ' '.join(text_lines)


@signal_api.route('/api/signals/ingest/transcript/upload', methods=['POST'])
@_require_signal_engine
def upload_transcript():
    """Upload a meeting transcript file for signal extraction.

    Accepts multipart file upload (.txt, .vtt, .srt) or JSON with raw_text.
    Strips timestamps from VTT/SRT formats and extracts plain text.

    Multipart form fields:
      - file: The transcript file (.txt, .vtt, .srt)
      - account_id: (required) Account ID
      - customer_id: (required) Customer ID
      - consent_verified: (required, must be 'true') Participant consent

    JSON body alternative:
      {
          "raw_text": "...",
          "account_id": 301,
          "customer_id": 407,
          "consent_verified": true
      }
    """
    # Handle JSON body (existing behavior)
    if request.is_json:
        return _ingest_signal('transcript')

    # Handle multipart file upload
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded. Send a .txt, .vtt, or .srt file.'}), 400

    account_id = request.form.get('account_id', type=int)
    customer_id = request.form.get('customer_id', type=int)
    consent = request.form.get('consent_verified', '').lower()

    if not account_id or not customer_id:
        return jsonify({'error': 'account_id and customer_id are required'}), 400

    if consent != 'true':
        return jsonify({
            'error': 'consent_verified must be "true"',
            'hint': 'Verify participant consent before submitting transcript data',
        }), 400

    # Per-customer feature check
    if not _check_customer_signal_engine(customer_id):
        return jsonify({
            'error': f'Signal Engine not enabled for customer {customer_id}',
        }), 403

    # Read and parse file
    filename = file.filename or 'transcript.txt'
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'txt'

    try:
        content = file.read().decode('utf-8', errors='replace')
    except Exception as e:
        return jsonify({'error': f'Failed to read file: {str(e)}'}), 400

    if not content.strip():
        return jsonify({'error': 'File is empty'}), 400

    # Parse based on format
    if ext == 'vtt':
        raw_text = _parse_vtt(content)
    elif ext == 'srt':
        raw_text = _parse_srt(content)
    else:
        raw_text = content.strip()

    if not raw_text or len(raw_text) < 20:
        return jsonify({'error': 'Parsed transcript too short (< 20 chars)'}), 400

    # Cap at 10K chars for transcripts
    raw_text = raw_text[:10000]

    # Create signal
    try:
        from extensions import db
        from models import QualitativeSignal

        signal_id = str(uuid.uuid4())
        parsed_date = datetime.utcnow().date()

        signal = QualitativeSignal(
            signal_id=signal_id,
            customer_id=customer_id,
            account_id=account_id,
            signal_type='transcript',
            content=raw_text[:2000],
            sentiment='neutral',
            signal_date=parsed_date,
        )

        for attr, val in [
            ('source_type', 'transcript'),
            ('raw_text', raw_text),
            ('requires_review', False),
            ('consent_verified', True),
            ('composite_signal_id', signal_id),
        ]:
            try:
                setattr(signal, attr, val)
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
            "Transcript signal ingested: id=%s file=%s account=%d customer=%d chars=%d",
            signal_id, filename, account_id, customer_id, len(raw_text),
        )

        return jsonify({
            'raw_signal_id': signal_id,
            'status': 'queued',
            'source_type': 'transcript',
            'account_id': account_id,
            'customer_id': customer_id,
            'filename': filename,
            'format': ext,
            'text_length': len(raw_text),
            'message': 'Transcript accepted. Enrichment will run asynchronously.',
        }), 202

    except Exception as e:
        logger.exception("Transcript ingestion failed: %s", e)
        return jsonify({'error': f'Ingestion failed: {str(e)}'}), 500


# ============================================================
# Review Queue
# ============================================================

@signal_api.route('/api/signals/review-queue', methods=['GET'])
@_require_signal_engine
def get_review_queue():
    """Get signals requiring human review.

    Signals enter the review queue when:
    - Any confidence score < 0.6 on a critical field
    - LLM returned invalid/partial response
    - Transcript without verified consent
    - Dedup confidence < 0.7

    Query params:
      customer_id: (required) filter by customer
      account_id: (optional) filter by account
      urgency: (optional) filter by urgency level
      page: (default 1)
      per_page: (default 25)
    """
    customer_id = request.args.get('customer_id', type=int)
    if not customer_id:
        return jsonify({'error': 'customer_id is required'}), 400

    account_id = request.args.get('account_id', type=int)
    urgency = request.args.get('urgency')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)

    try:
        from models import QualitativeSignal, Account

        # Filter directly by customer_id (no account join needed)
        query = QualitativeSignal.query.filter(QualitativeSignal.customer_id == customer_id)

        # Filter to signals needing review (if enrichment column exists)
        try:
            query = query.filter(QualitativeSignal.requires_review == True)
        except Exception:
            pass  # Column not yet migrated — return all signals

        if account_id:
            query = query.filter_by(account_id=account_id)
        if urgency:
            try:
                query = query.filter(QualitativeSignal.effective_urgency == urgency)
            except Exception:
                pass

        query = query.order_by(QualitativeSignal.signal_date.desc())
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)

        signals = []
        for s in paginated.items:
            signals.append({
                'signal_id': s.signal_id,
                'account_id': s.account_id,
                'signal_type': s.signal_type,
                'content': s.content[:200] if s.content else '',
                'sentiment': s.sentiment,
                'signal_date': s.signal_date.isoformat() if s.signal_date else None,
                'source_type': getattr(s, 'source_type', None),
                'intent_signals': getattr(s, 'intent_signals', None),
                'confidence': getattr(s, 'confidence', None),
                'effective_urgency': getattr(s, 'effective_urgency', None),
            })

        return jsonify({
            'review_queue': signals,
            'total': paginated.total,
            'page': paginated.page,
        })

    except Exception as e:
        logger.exception("Review queue query failed: %s", e)
        return jsonify({'error': str(e)}), 500


# ============================================================
# Signal Engine status / health check
# ============================================================

@signal_api.route('/api/signals/status', methods=['GET'])
def signal_engine_status():
    """Check Signal Engine status and feature toggle state."""
    import os
    enabled = os.environ.get('FEATURE_SIGNAL_ENGINE', 'false').lower() in ('true', '1', 'yes')
    llm_available = bool(os.environ.get('ANTHROPIC_API_KEY'))
    return jsonify({
        'signal_engine_enabled': enabled,
        'version': '1.0.0',
        'phase': 'Phase 1 — MVP',
        'capabilities': {
            'ingestion': enabled,
            'structural_urgency': enabled,
            'cg_collision_check': enabled,
            'composite_fusion': enabled,
            'llm_enrichment': llm_available,
            'review_queue': enabled,
            'alert_routing': False,    # Phase 2
            'channels': {
                'manual': enabled,
                'email': enabled,
                'slack': enabled,
                'transcript': enabled,
            },
        },
    })
