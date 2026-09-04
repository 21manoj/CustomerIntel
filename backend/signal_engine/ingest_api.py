"""
Signal ingest — framework-agnostic request handling.

Each function takes plain data and returns (status_code, body). The
HTTP surface lives in signal_engine.http (Starlette routes on the
CustomerIntelV1 server); the MCP tools call signal_engine.pipeline
directly. v2 (2026-09-03): replaced the Flask blueprint the old build
never mounted on the new server.

Consent: transcript ingestion requires consent_verified=true.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def engine_enabled() -> bool:
    return os.environ.get('FEATURE_SIGNAL_ENGINE', 'true').lower() in ('true', '1', 'yes')


def customer_engine_enabled(customer_id: int) -> bool:
    """Per-customer toggle (FeatureToggle row 'signal_engine'). Webhook
    sources require it — they are only signature-authenticated."""
    try:
        from models import FeatureToggle
        t = FeatureToggle.query.filter_by(customer_id=int(customer_id), feature_name='signal_engine').first()
        return bool(t and t.enabled)
    except Exception:
        return False


def ingest_from_payload(source_type: str, data: dict, *, require_customer_toggle: bool = False) -> Tuple[int, dict]:
    """JSON ingest for manual / slack / email / transcript / ticket / crm_activity.

    Body: account_id, customer_id, raw_text; optional timestamp | occurred_at,
    participant_list [{name, role}], signal_type (a taxonomy subtype — the
    structured path, no LLM), source_ref, consent_verified.
    """
    if not engine_enabled():
        return 403, {'error': 'Signal Engine is disabled', 'hint': 'Set FEATURE_SIGNAL_ENGINE=true'}
    data = data or {}
    account_id, customer_id = data.get('account_id'), data.get('customer_id')
    raw_text = (data.get('raw_text') or data.get('text') or '').strip()
    if not account_id or not customer_id:
        return 400, {'error': 'account_id and customer_id are required'}
    if not raw_text:
        return 400, {'error': 'raw_text is required'}
    if require_customer_toggle and not customer_engine_enabled(customer_id):
        return 403, {'error': f'Signal Engine not enabled for customer {customer_id}',
                     'hint': 'configure_signal_engine(customer_id, enabled=true)'}
    try:
        from signal_engine.pipeline import ingest
        res = ingest(int(customer_id), int(account_id), source_type, raw_text,
                     occurred_at=data.get('occurred_at') or data.get('timestamp'),
                     participants=data.get('participant_list') or data.get('participants'),
                     signal_type=data.get('signal_type'), source_ref=data.get('source_ref') or data.get('thread_or_channel_id'),
                     consent_verified=data.get('consent_verified'))
    except ValueError as e:
        return 400, {'error': str(e)}
    except Exception as e:  # pragma: no cover
        logger.exception('signal ingestion failed: %s', e)
        return 500, {'error': f'Ingestion failed: {e}'}
    code = 202 if res['status'] == 'queued' else 200
    res['message'] = ('Signal accepted; it becomes evidence on the next processing pass.'
                      if res['status'] == 'queued' else 'Duplicate of an existing signal within the dedup window.')
    return code, res


# ── transcripts ──────────────────────────────────────────────────────────

def _parse_vtt(content: str) -> str:
    """Parse WebVTT (.vtt), stripping timestamps, headers and cue ids."""
    out = []
    for line in content.split('\n'):
        s = line.strip()
        if s.startswith('WEBVTT') or s.startswith('NOTE') or re.match(r'\d{2}:\d{2}:\d{2}', s) or s.isdigit():
            continue
        if s:
            out.append(s)
    return ' '.join(out)


def _parse_srt(content: str) -> str:
    """Parse SubRip (.srt), stripping sequence numbers and timestamps."""
    out = []
    for line in content.split('\n'):
        s = line.strip()
        if s.isdigit() or re.match(r'\d{2}:\d{2}:\d{2},\d{3}', s):
            continue
        if s:
            out.append(s)
    return ' '.join(out)


def ingest_transcript_file(filename: str, content: str, account_id, customer_id, consent: str,
                           occurred_at=None) -> Tuple[int, dict]:
    if not engine_enabled():
        return 403, {'error': 'Signal Engine is disabled'}
    if not account_id or not customer_id:
        return 400, {'error': 'account_id and customer_id are required'}
    if str(consent).lower() != 'true':
        return 400, {'error': 'consent_verified must be "true"', 'hint': 'Verify participant consent before submitting transcript data'}
    ext = (filename or 'transcript.txt').rsplit('.', 1)[-1].lower()
    text = _parse_vtt(content) if ext == 'vtt' else _parse_srt(content) if ext == 'srt' else (content or '').strip()
    if len(text) < 20:
        return 400, {'error': 'Parsed transcript too short (< 20 chars)'}
    code, body = ingest_from_payload('transcript', {
        'account_id': account_id, 'customer_id': customer_id, 'raw_text': text[:10000],
        'consent_verified': True, 'occurred_at': occurred_at})
    body.update({'filename': filename, 'format': ext, 'text_length': len(text[:10000])})
    return code, body


# ── review queue + status ───────────────────────────────────────────────

def review_queue(customer_id: int, account_id: Optional[int] = None, urgency: Optional[str] = None,
                 page: int = 1, per_page: int = 25) -> Tuple[int, dict]:
    if not customer_id:
        return 400, {'error': 'customer_id is required'}
    from models import QualitativeSignal
    q = QualitativeSignal.query.filter(QualitativeSignal.customer_id == int(customer_id),
                                       QualitativeSignal.requires_review.is_(True))
    if account_id:
        q = q.filter_by(account_id=int(account_id))
    if urgency:
        q = q.filter(QualitativeSignal.effective_urgency == urgency)
    total = q.count()
    items = q.order_by(QualitativeSignal.signal_date.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return 200, {'review_queue': [{
        'signal_id': s.signal_id, 'account_id': s.account_id, 'signal_type': s.signal_type,
        'content': (s.content or '')[:200], 'sentiment': s.sentiment,
        'signal_date': s.signal_date.isoformat() if s.signal_date else None, 'source_type': s.source_type,
        'intent_signals': s.intent_signals, 'confidence': s.confidence, 'effective_urgency': s.effective_urgency,
        'node_id': s.cg_node_id} for s in items], 'total': total, 'page': page}


def status_payload() -> dict:
    enabled = engine_enabled()
    llm_available = bool(os.environ.get('ANTHROPIC_API_KEY'))
    return {
        'signal_engine_enabled': enabled,
        'version': '2.0.0',
        'phase': 'v2 — evidence pipeline (roles, provenance, people)',
        'capabilities': {
            'ingestion': enabled,
            'structural_urgency': enabled,
            'composite_fusion': False,      # retired — the journey's leading composite is the only leading score
            'llm_enrichment': llm_available,
            'structured_rule_map': enabled,
            'review_queue': enabled,
            'alert_routing': False,
            'channels': {
                'manual': enabled,
                'email': enabled,
                'slack': enabled,
                'transcript': enabled,
                'ticket': enabled,
                'crm_activity': enabled,
            },
        },
    }
