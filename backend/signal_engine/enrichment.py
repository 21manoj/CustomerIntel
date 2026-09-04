"""
LLM enrichment — turns free text into structured intelligence the
pipeline can classify: intents (a closed vocabulary, every entry a
taxonomy subtype), sentiment, perceived urgency, people, a suggested
action, and per-field confidence.

Structured signals (a declared taxonomy subtype) never come here; the
pipeline maps them by rule. Without ANTHROPIC_API_KEY a keyword stub
answers, always flagged requires_review.

Prompt context comes from the customer's own KPI catalog (vertical
registry) — there is no per-vertical prose in code — plus the account's
recent signals for duplicate/trajectory awareness.

Tunables: config/signal_engine.json → llm.* (model, limits, thresholds);
SIGNAL_ENRICHMENT_MODEL overrides the model at runtime. Every call is
metered through utils.llm_budget_controller.record_usage.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from signal_engine import settings

logger = logging.getLogger(__name__)

# Intent codes the model may emit. Each must be a signal subtype in
# config/taxonomy_base.json (tests/test_signal_engine_config.py pins this),
# so classification is a lookup, not a second vocabulary.
VALID_INTENTS = [
    'renewal_risk', 'expansion_interest', 'champion_change',
    'product_frustration', 'feature_request', 'executive_escalation',
    'pricing_concern', 'competitor_mention', 'deployment_blocker',
    'nps_drop_indicator', 'positive_advocacy',
]

_NEUTRAL_CONTEXT = {'description': 'No vertical-specific context available',
                    'pillars': '(unavailable)', 'key_terms': '(unavailable)'}


def build_vertical_context(vertical: str) -> Dict:
    """Prompt context derived from the vertical's own catalog: its
    description, pillars and KPI names. Never another vertical's framing;
    if the catalog cannot be read, a neutral stub."""
    try:
        from utils.vertical_registry import get_pillars, get_kpis, get_vertical_description
        pillars = get_pillars(vertical) or {}
        kpis = get_kpis(vertical) or {}
        pillar_str = ', '.join(f"{pid} {pdef.get('name', pid)}" for pid, pdef in sorted(pillars.items())) or '(no pillars registered)'
        kpi_names = [kdef.get('name') for kdef in kpis.values() if kdef.get('name')]
        return {
            'name': vertical.replace('_', ' ').title(),
            'description': get_vertical_description(vertical) or f'{vertical} vertical (from its KPI catalog)',
            'pillars': pillar_str,
            'key_terms': ', '.join(kpi_names[:settings.get('llm', 'key_terms_max')]) or '(no KPIs registered)',
        }
    except Exception as e:
        logger.warning("enrichment: could not derive vertical context for %r: %s", vertical, e)
        return {'name': vertical or 'Unknown Vertical', **_NEUTRAL_CONTEXT}


ENRICHMENT_PROMPT = """You are a B2B Customer Success signal extraction engine for the {vertical_name} vertical.

Industry context: {vertical_description}
Key pillars: {vertical_pillars}
Industry terminology: {vertical_terms}
{similar_signals_block}
Analyze the following customer communication and extract structured intelligence.
Return ONLY valid JSON. No preamble. No markdown fences. No explanation.

Confidence scores reflect explicitness:
  1.0 = stated directly in the text
  0.7 = strongly implied
  0.5 = weakly implied
  0.0 = not determinable from the text

If confidence for any field is below {confidence_threshold}, set requires_review to true.
{dedup_instruction}
VALID INTENT CODES (use ONLY these):
{intent_codes}

TEXT TO ANALYZE:
{raw_text}

RESPOND WITH THIS EXACT JSON STRUCTURE:
{{
  "sentiment_score": <float -1.0 to +1.0>,
  "relationship_sentiment": <float -1.0 to +1.0>,
  "product_sentiment": <float -1.0 to +1.0>,
  "urgency_score": <float 0.0 to 1.0>,
  "escalation_probability": <float 0.0 to 1.0>,
  "intent_signals": ["<intent_code>", ...],
  "stakeholder_roles": [{{"role": "<title>", "name": "<name or null>"}}],
  "suggested_action": "<one sentence recommended CSM action>",
  "is_duplicate": <boolean>,
  "duplicate_reason": "<null or explanation if duplicate>",
  "confidence": {{
    "sentiment_score": <float>,
    "intent_signals": <float>,
    "urgency_score": <float>,
    "escalation_probability": <float>
  }},
  "requires_review": <boolean>
}}"""


# ── daily call budget (process-local; llm_budget_controller keeps the durable ledger) ──

_call_counts: Dict[str, Dict[str, int]] = {}   # {date: {customer_X: n, account_Y: n}}


def _today_counts() -> Dict[str, int]:
    today = datetime.utcnow().strftime('%Y-%m-%d')
    if today not in _call_counts:
        _call_counts.clear()
        _call_counts[today] = {}
    return _call_counts[today]


def _check_rate_limit(customer_id: int, account_id: int) -> Optional[str]:
    counts = _today_counts()
    per_customer = settings.get('llm', 'max_calls_per_customer_per_day')
    per_account = settings.get('llm', 'max_calls_per_account_per_day')
    if counts.get(f'customer_{customer_id}', 0) >= per_customer:
        return f'Customer {customer_id} exceeded daily limit ({per_customer} calls/day)'
    if counts.get(f'account_{account_id}', 0) >= per_account:
        return f'Account {account_id} exceeded daily limit ({per_account} calls/day)'
    return None


def _record_call(customer_id: int, account_id: int) -> None:
    counts = _today_counts()
    counts[f'customer_{customer_id}'] = counts.get(f'customer_{customer_id}', 0) + 1
    counts[f'account_{account_id}'] = counts.get(f'account_{account_id}', 0) + 1


# ── account context for the prompt ──

def _recent_account_signals(account_id: int) -> List[dict]:
    """The account's latest SIGNAL nodes — what the model sees for duplicate
    and trajectory awareness. (The old build tried a Qdrant vector store
    first; this build has none, so the SQL path is the path.)"""
    try:
        from models import ContextNode
        recent = (ContextNode.query.filter_by(account_id=account_id, node_type='SIGNAL')
                  .order_by(ContextNode.occurred_at.desc()).limit(settings.get('llm', 'context_signals')).all())
        return [{'title': n.title or '(no title)', 'subtype': n.node_subtype or 'signal',
                 'sentiment': (n.properties or {}).get('sentiment', 'unknown')} for n in recent]
    except Exception:
        return []


def _context_blocks(similar: List[dict]) -> tuple:
    if not similar:
        return '', ''
    lines = [f"  - [{s['subtype']}] {s['title']} (sentiment: {s['sentiment']})" for s in similar]
    block = ("\nRECENT SIGNALS FOR THIS ACCOUNT:\n" + "\n".join(lines) +
             "\n\nUse these to:\n  1. Detect if the new signal is a DUPLICATE of an existing one\n"
             "  2. Understand the account's recent trajectory and context\n  3. Correlate this signal with existing patterns\n")
    dedup = ("\nDUPLICATE CHECK: if this new signal conveys the SAME information as one above, set is_duplicate=true "
             "and explain in duplicate_reason. Do NOT set is_duplicate for signals that are related but contain NEW information.\n")
    return block, dedup


# ── enrichment ──

def enrich_signal(signal_id: str, raw_text: str, account_id: int, customer_id: int, vertical: str) -> Dict:
    """Enrich one free-text signal. Returns a dict matching QualitativeSignal
    columns; on any failure a partial result with requires_review=True."""
    limit_error = _check_rate_limit(customer_id, account_id)
    if limit_error:
        logger.warning('enrichment rate limit: %s', limit_error)
        return {'requires_review': True, 'confidence': {'rate_limited': True},
                'suggested_action': f'Rate limited: {limit_error}'}

    similar = _recent_account_signals(account_id)
    similar_block, dedup_instruction = _context_blocks(similar)
    v_ctx = build_vertical_context(vertical)
    prompt = ENRICHMENT_PROMPT.format(
        vertical_name=v_ctx['name'], vertical_description=v_ctx['description'],
        vertical_pillars=v_ctx['pillars'], vertical_terms=v_ctx['key_terms'],
        similar_signals_block=similar_block, dedup_instruction=dedup_instruction,
        intent_codes=', '.join(VALID_INTENTS), raw_text=raw_text[:settings.get('llm', 'prompt_text_chars')],
        confidence_threshold=settings.get('llm', 'confidence_threshold'),
    )

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        logger.warning('enrichment: ANTHROPIC_API_KEY not set — keyword stub')
        return _stub_enrichment(raw_text)

    model = settings.llm_model()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(model=model, max_tokens=settings.get('llm', 'max_tokens'),
                                          messages=[{'role': 'user', 'content': prompt}])
        try:
            from utils.llm_budget_controller import record_usage
            record_usage(customer_id=customer_id, module='signal_engine_enrichment',
                         tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens,
                         model=model, success=True)
        except Exception as cost_err:
            logger.debug('signal_engine_enrichment: cost tracking failed: %s', cost_err)
        _record_call(customer_id, account_id)

        enrichment = _validate_enrichment(json.loads(response.content[0].text.strip()))
        enrichment['llm_model_version'] = model
        enrichment['_similar_signal_count'] = len(similar)
        logger.info('enrichment complete: signal=%s intents=%s urgency=%.2f review=%s context=%d duplicate=%s',
                    signal_id, enrichment.get('intent_signals', []), enrichment.get('urgency_score', 0),
                    enrichment.get('requires_review', False), len(similar), enrichment.get('is_duplicate', False))
        return enrichment
    except json.JSONDecodeError as e:
        logger.warning('enrichment: invalid JSON from LLM for signal %s: %s', signal_id, e)
        return {'requires_review': True, 'confidence': {'parse_error': True},
                'suggested_action': 'LLM returned invalid JSON — manual review required', 'llm_model_version': model}
    except Exception as e:
        logger.exception('enrichment failed for signal %s: %s', signal_id, e)
        return {'requires_review': True, 'confidence': {'error': str(e)},
                'suggested_action': f'Enrichment error: {str(e)[:100]}'}


def _validate_enrichment(data: Dict) -> Dict:
    """Validate and sanitize LLM enrichment output."""
    # Clamp numeric values
    for field in ('sentiment_score', 'relationship_sentiment', 'product_sentiment'):
        if field in data:
            data[field] = max(-1.0, min(1.0, float(data[field])))

    for field in ('urgency_score', 'escalation_probability'):
        if field in data:
            data[field] = max(0.0, min(1.0, float(data[field])))

    # Validate intent codes
    if 'intent_signals' in data:
        data['intent_signals'] = [i for i in data['intent_signals'] if i in VALID_INTENTS]

    # Validate duplicate detection fields
    if 'is_duplicate' not in data:
        data['is_duplicate'] = False
    if 'duplicate_reason' not in data:
        data['duplicate_reason'] = None

    # Check confidence threshold
    confidence = data.get('confidence', {})
    low_confidence = any(
        v < settings.get('llm', 'confidence_threshold')
        for k, v in confidence.items()
        if isinstance(v, (int, float))
    )
    if low_confidence:
        data['requires_review'] = True

    # Duplicates always require review (human confirmation before discard)
    if data.get('is_duplicate'):
        data['requires_review'] = True

    return data


def _stub_enrichment(raw_text: str) -> Dict:
    """Stub enrichment when no API key available.

    Uses simple keyword matching for basic intent detection.
    Always sets requires_review=True (not LLM-verified).
    """
    text_lower = raw_text.lower()

    # Simple keyword-based intent detection
    intents = []
    if any(w in text_lower for w in ('competitor', 'alternative', 'evaluating', 'switch')):
        intents.append('competitor_mention')
    if any(w in text_lower for w in ('expand', 'capacity', 'growth', 'additional', 'upgrade')):
        intents.append('expansion_interest')
    if any(w in text_lower for w in ('escalat', 'board', 'cto', 'vp', 'urgent')):
        intents.append('executive_escalation')
    if any(w in text_lower for w in ('renew', 'contract', 'subscription')):
        intents.append('renewal_risk')
    if any(w in text_lower for w in ('frustrat', 'issue', 'problem', 'broken', 'fail')):
        intents.append('product_frustration')
    if any(w in text_lower for w in ('feature', 'request', 'wishlist', 'need')):
        intents.append('feature_request')
    if any(w in text_lower for w in ('champion', 'leaving', 'departed', 'new role')):
        intents.append('champion_change')
    if any(w in text_lower for w in ('price', 'cost', 'budget', 'expensive')):
        intents.append('pricing_concern')

    # Simple sentiment from keywords
    positive_words = {'positive', 'great', 'excellent', 'happy', 'confirmed', 'approved', 'love'}
    negative_words = {'negative', 'issue', 'problem', 'frustrated', 'escalate', 'concerned', 'risk'}
    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)
    sentiment = (pos_count - neg_count) / max(pos_count + neg_count, 1)

    urgency = 0.7 if 'urgent' in text_lower or 'escalat' in text_lower else 0.3

    return {
        'sentiment_score': round(sentiment, 2),
        'relationship_sentiment': round(sentiment * 0.8, 2),
        'product_sentiment': round(sentiment * 0.6, 2),
        'urgency_score': urgency,
        'escalation_probability': 0.6 if 'escalat' in text_lower else 0.1,
        'intent_signals': intents,
        'stakeholder_roles': [],
        'suggested_action': 'Review signal — stub enrichment (no API key)',
        'confidence': {
            'sentiment_score': 0.3,
            'intent_signals': 0.4,
            'urgency_score': 0.3,
            'escalation_probability': 0.3,
        },
        'requires_review': True,
        'llm_model_version': 'stub_keyword_v1',
    }
