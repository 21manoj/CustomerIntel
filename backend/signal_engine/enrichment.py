"""
LLM extraction — free text in, a LIST of taxonomy-typed signals out.

Every element of the vocabulary the model may use is the tenant's own
(taxonomy base + vertical overlay): role definitions, the closed subtype
set (enforced as a tool-schema enum, not by instruction), and the
vertical's few-shot examples. People are matched against the account's
roster in the prompt, so the model returns a roster role, not a guess.

Structured signals (a declared taxonomy subtype) never come here; the
pipeline maps them by rule. Without ANTHROPIC_API_KEY a keyword stub
answers in the same shape, always flagged requires_review.

Output shape (also stored on QualitativeSignal.extractions):
    {'signals': [{subtype, role, quote, sentiment_score, urgency_score,
                  escalation_probability, people: [{name, title, roster_role}], confidence}],
     'is_duplicate', 'duplicate_reason', 'suggested_action', 'requires_review',
     'llm_model_version',
     # flattened from the first signal / the whole list, for the columns the
     # review queue and the journey already read:
     'sentiment_score', 'urgency_score', 'escalation_probability',
     'intent_signals' (subtypes in order), 'stakeholder_roles', 'confidence'}

Tunables: config/signal_engine.json → llm.*; SIGNAL_ENRICHMENT_MODEL
overrides the model. Every call is metered through
utils.llm_budget_controller.record_usage.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from signal_engine import settings

logger = logging.getLogger(__name__)

TOOL_NAME = 'record_signals'
STUB_MODEL_VERSION = 'stub_keyword_v2'

SYSTEM_PROMPT = """You are the signal extraction engine of a B2B Customer Success platform for the {vertical_name} vertical.
You read one customer communication and record every distinct customer-success signal in it, each typed with a subtype from the closed vocabulary below. You never invent subtypes. A text can contain zero, one, or several signals; record each once, citing the exact words that support it.

Industry context: {vertical_description}
Health pillars: {vertical_pillars}
Vertical terms you will see: {vertical_terms}

SIGNAL ROLES AND THEIR SUBTYPES (subtype → meaning of its role):
{vocabulary_block}

RULES
- Only the customer's own words and actions are signals. Vendor actions are `intervention` roles; a CSM's opinion about risk is a `crm_flag`.
- Sentiment is about the customer's stance toward the vendor and product, -1.0 to +1.0. Urgency is how soon someone must act, 0.0 to 1.0.
- Confidence reflects explicitness: 1.0 stated directly, 0.7 strongly implied, 0.5 weakly implied. Below {confidence_threshold} on any signal, set requires_review.
- People: name anyone the text identifies. If a person matches the ACCOUNT ROSTER, return their roster_role; otherwise leave roster_role unset.
- If the text carries the SAME information as one of the RECENT SIGNALS, set is_duplicate and explain; related-but-new information is not a duplicate.
- Nothing to record (a pleasantry, a scheduling note) → an empty signals list, not a forced signal.

EXAMPLES
{examples_block}"""

USER_PROMPT = """ACCOUNT ROSTER (known people on this account):
{roster_block}
{similar_signals_block}
TEXT TO ANALYZE:
{raw_text}"""

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


def vocabulary_block(taxonomy) -> str:
    lines = []
    for role, subs in taxonomy.vocabulary().items():
        d = taxonomy.role_definitions.get(role, {})
        lines.append(f"- {role}: {d.get('is', '')} NOT: {d.get('not', '')}\n    subtypes: {', '.join(subs)}")
    return '\n'.join(lines)


def examples_block(taxonomy) -> str:
    out = []
    for ex in taxonomy.examples:
        out.append(f'Text: "{ex["text"]}"\n  → signals: {", ".join(ex["subtypes"]) or "(none)"}')
    return '\n'.join(out) or '(none)'


def roster_block(roster: List[dict]) -> str:
    if not roster:
        return '(no known people on this account)'
    return '\n'.join(f"- {p['name']} — {p.get('title') or '?'} (roster_role: {p['role']})" for p in roster)


def record_signals_tool(taxonomy, roster: List[dict]) -> dict:
    """The tool schema: the vocabulary is an enum, so an unknown subtype is
    rejected by the API, never parsed into the pipeline."""
    roster_roles = sorted({p['role'] for p in roster if p.get('role')})
    person = {'type': 'object', 'required': ['name'],
              'properties': {'name': {'type': 'string'}, 'title': {'type': 'string'}}}
    if roster_roles:
        person['properties']['roster_role'] = {'type': 'string', 'enum': roster_roles}
    return {
        'name': TOOL_NAME,
        'description': 'Record every customer-success signal found in the text, typed with the closed vocabulary.',
        'input_schema': {
            'type': 'object',
            'required': ['signals', 'requires_review', 'is_duplicate', 'suggested_action'],
            'properties': {
                'signals': {'type': 'array', 'items': {
                    'type': 'object',
                    'required': ['subtype', 'quote', 'sentiment_score', 'urgency_score', 'escalation_probability', 'confidence'],
                    'properties': {
                        'subtype': {'type': 'string', 'enum': taxonomy.all_subtypes()},
                        'quote': {'type': 'string', 'description': 'the exact words that support this signal'},
                        'sentiment_score': {'type': 'number', 'minimum': -1, 'maximum': 1},
                        'urgency_score': {'type': 'number', 'minimum': 0, 'maximum': 1},
                        'escalation_probability': {'type': 'number', 'minimum': 0, 'maximum': 1},
                        'people': {'type': 'array', 'items': person},
                        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                    }}},
                'is_duplicate': {'type': 'boolean'},
                'duplicate_reason': {'type': 'string'},
                'suggested_action': {'type': 'string', 'description': 'one sentence: the recommended CSM action, or "none"'},
                'requires_review': {'type': 'boolean'},
            },
        },
    }


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
    and trajectory awareness."""
    try:
        from models import ContextNode
        recent = (ContextNode.query.filter_by(account_id=account_id, node_type='SIGNAL')
                  .order_by(ContextNode.occurred_at.desc()).limit(settings.get('llm', 'context_signals')).all())
        return [{'title': n.title or '(no title)', 'subtype': n.node_subtype or 'signal',
                 'sentiment': (n.properties or {}).get('sentiment', 'unknown')} for n in recent]
    except Exception:
        return []


def _similar_block(similar: List[dict]) -> str:
    if not similar:
        return ''
    lines = [f"  - [{s['subtype']}] {s['title']} (sentiment: {s['sentiment']})" for s in similar]
    return "\nRECENT SIGNALS FOR THIS ACCOUNT:\n" + "\n".join(lines) + "\n"


# ── normalize the model's output into the shape the pipeline stores ──

def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _coerce_payload(data) -> dict:
    """Models sometimes return the tool payload, or its `signals` field, as a
    JSON *string* instead of an object. Parse and unwrap before validating."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {'signals': [], 'requires_review': True}
    if not isinstance(data, dict):
        return {'signals': [], 'requires_review': True}
    sigs = data.get('signals')
    if isinstance(sigs, str):
        try:
            parsed = json.loads(sigs)
        except ValueError:
            parsed = []
        if isinstance(parsed, dict) and 'signals' in parsed:      # whole payload nested under 'signals'
            data = {**data, **parsed}
        else:
            data = {**data, 'signals': parsed if isinstance(parsed, list) else []}
    return data


def normalize_extraction(data: dict, taxonomy) -> dict:
    """Validate + flatten a record_signals payload. Unknown subtypes are
    dropped (the API enum should have prevented them) and counted."""
    data = _coerce_payload(data)
    threshold = settings.get('llm', 'confidence_threshold')
    signals, dropped = [], 0
    for item in (data.get('signals') or []):
        if not isinstance(item, dict):
            continue
        subtype = (item.get('subtype') or '').strip().lower()
        role = taxonomy.signal_role(subtype)
        if not role:
            dropped += 1
            continue
        people = [{'name': p.get('name'), 'title': p.get('title'), 'roster_role': p.get('roster_role')}
                  for p in (item.get('people') or []) if isinstance(p, dict) and p.get('name')]
        signals.append({
            'subtype': subtype, 'role': role, 'quote': (item.get('quote') or '')[:500],
            'sentiment_score': _clamp(item.get('sentiment_score'), -1, 1, 0.0),
            'urgency_score': _clamp(item.get('urgency_score'), 0, 1, 0.0),
            'escalation_probability': _clamp(item.get('escalation_probability'), 0, 1, 0.0),
            'people': people, 'confidence': _clamp(item.get('confidence'), 0, 1, 0.0),
        })
    low = any(s['confidence'] < threshold for s in signals)
    requires_review = bool(data.get('requires_review')) or low or bool(data.get('is_duplicate')) or dropped > 0
    first = signals[0] if signals else None
    all_people = []
    for s in signals:
        for p in s['people']:
            if not any(q['name'].lower() == p['name'].lower() for q in all_people):
                all_people.append(p)
    return {
        'signals': signals,
        'is_duplicate': bool(data.get('is_duplicate')), 'duplicate_reason': data.get('duplicate_reason') or None,
        'suggested_action': (data.get('suggested_action') or '')[:500] or None,
        'requires_review': requires_review, 'dropped_unknown_subtypes': dropped,
        # flattened, column-compatible view
        'sentiment_score': (sum(s['sentiment_score'] for s in signals) / len(signals)) if signals else 0.0,
        'urgency_score': max((s['urgency_score'] for s in signals), default=0.0),
        'escalation_probability': max((s['escalation_probability'] for s in signals), default=0.0),
        'intent_signals': [s['subtype'] for s in signals],
        'stakeholder_roles': [{'name': p['name'], 'role': p.get('title'), 'roster_role': p.get('roster_role')} for p in all_people] or None,
        'confidence': {s['subtype']: s['confidence'] for s in signals} | ({'first': first['confidence']} if first else {}),
    }


# ── extraction ──

def enrich_signal(signal_id: str, raw_text: str, account_id: int, customer_id: int, vertical: str,
                  taxonomy=None, roster: Optional[List[dict]] = None) -> Dict:
    """Extract every signal in one free-text communication. Returns the
    normalized shape above; on any failure a partial result with
    requires_review=True and no signals."""
    from utils.taxonomy_loader import get_taxonomy
    taxonomy = taxonomy or get_taxonomy(vertical)
    if roster is None:
        from signal_engine.pipeline import account_roster
        roster = account_roster(customer_id, account_id)

    limit_error = _check_rate_limit(customer_id, account_id)
    if limit_error:
        logger.warning('enrichment rate limit: %s', limit_error)
        return {**normalize_extraction({'signals': [], 'requires_review': True}, taxonomy),
                'error': limit_error, 'suggested_action': f'Rate limited: {limit_error}', 'confidence': {'rate_limited': True}}

    v_ctx = build_vertical_context(vertical)
    system = SYSTEM_PROMPT.format(
        vertical_name=v_ctx['name'], vertical_description=v_ctx['description'],
        vertical_pillars=v_ctx['pillars'], vertical_terms=v_ctx['key_terms'],
        vocabulary_block=vocabulary_block(taxonomy), examples_block=examples_block(taxonomy),
        confidence_threshold=settings.get('llm', 'confidence_threshold'))
    similar = _recent_account_signals(account_id)
    user = USER_PROMPT.format(roster_block=roster_block(roster), similar_signals_block=_similar_block(similar),
                              raw_text=raw_text[:settings.get('llm', 'prompt_text_chars')])

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        logger.warning('enrichment: ANTHROPIC_API_KEY not set — keyword stub')
        return _stub_enrichment(raw_text, taxonomy)

    model = settings.llm_model()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        tool = record_signals_tool(taxonomy, roster)
        response = client.messages.create(
            model=model, max_tokens=settings.get('llm', 'max_tokens'), system=system,
            messages=[{'role': 'user', 'content': user}],
            tools=[tool], tool_choice={'type': 'tool', 'name': TOOL_NAME})
        try:
            from utils.llm_budget_controller import record_usage
            record_usage(customer_id=customer_id, module='signal_engine_enrichment',
                         tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens,
                         model=model, success=True)
        except Exception as cost_err:
            logger.debug('signal_engine_enrichment: cost tracking failed: %s', cost_err)
        _record_call(customer_id, account_id)

        payload = next((b.input for b in response.content if getattr(b, 'type', '') == 'tool_use'), None)
        if payload is None:
            raise ValueError('model returned no tool_use block')
        result = normalize_extraction(payload, taxonomy)
        result['llm_model_version'] = model
        result['_similar_signal_count'] = len(similar)
        logger.info('extraction complete: signal=%s subtypes=%s review=%s duplicate=%s',
                    signal_id, result['intent_signals'], result['requires_review'], result['is_duplicate'])
        return result
    except Exception as e:
        # A failed extraction is not evidence: the pipeline leaves the signal
        # queued (no node, intent_signals stays NULL) and the worker retries.
        logger.exception('extraction failed for signal %s: %s', signal_id, e)
        return {**normalize_extraction({'signals': [], 'requires_review': True}, taxonomy),
                'error': str(e)[:200], 'suggested_action': f'Extraction error: {str(e)[:100]}',
                'confidence': {'error': str(e)[:200]}, 'llm_model_version': model}


# ── stub (no API key) ──

# keyword → base subtype; every subtype here must exist in taxonomy_base
STUB_KEYWORDS = (
    (('competitor', 'alternative', 'evaluating', 'switch'), 'competitor_mention'),
    (('expand', 'capacity', 'growth', 'additional', 'upgrade'), 'expansion_interest'),
    (('escalat', 'board', 'cto', 'urgent'), 'executive_escalation'),
    (('renew', 'contract', 'subscription'), 'renewal_risk'),
    (('frustrat', 'issue', 'problem', 'broken', 'fail'), 'product_frustration'),
    (('feature', 'request', 'wishlist', 'need'), 'feature_request'),
    (('champion', 'leaving', 'departed', 'new role'), 'champion_change'),
    (('price', 'cost', 'budget', 'expensive'), 'pricing_concern'),
    (('outage', 'downtime', 'down since', 'incident'), 'incident'),
    (('usage dropped', 'stopped using', 'idle', 'utilization'), 'usage_decline'),
)
_POSITIVE_WORDS = {'positive', 'great', 'excellent', 'happy', 'confirmed', 'approved', 'love'}
_NEGATIVE_WORDS = {'negative', 'issue', 'problem', 'frustrated', 'escalate', 'concerned', 'risk'}


def _stub_enrichment(raw_text: str, taxonomy=None) -> Dict:
    """Keyword stub when no API key: same shape as the model path, always
    requires_review (not LLM-verified)."""
    if taxonomy is None:
        from utils.taxonomy_loader import get_taxonomy
        taxonomy = get_taxonomy('dc2_s')   # base vocabulary is what the stub emits; any vertical carries it
    text_lower = (raw_text or '').lower()
    pos = sum(1 for w in _POSITIVE_WORDS if w in text_lower)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in text_lower)
    sentiment = round((pos - neg) / max(pos + neg, 1), 2)
    urgency = 0.7 if ('urgent' in text_lower or 'escalat' in text_lower) else 0.3
    signals = []
    for words, subtype in STUB_KEYWORDS:
        if any(w in text_lower for w in words):
            signals.append({'subtype': subtype, 'quote': raw_text[:120], 'sentiment_score': sentiment,
                            'urgency_score': urgency, 'escalation_probability': 0.6 if 'escalat' in text_lower else 0.1,
                            'people': [], 'confidence': 0.4})
    out = normalize_extraction({'signals': signals, 'requires_review': True, 'is_duplicate': False,
                                'suggested_action': 'Review signal — keyword stub (no API key)'}, taxonomy)
    out['llm_model_version'] = STUB_MODEL_VERSION
    return out
