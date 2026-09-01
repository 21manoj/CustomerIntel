#!/usr/bin/env python3
"""
QSIM Signal Engine — LLM Enrichment Service.

Enriches raw signals with sentiment, intent, urgency, stakeholder roles
using Claude Sonnet via Anthropic SDK. Vertical-aware prompts ensure
accurate extraction for DC2_S vs SaaS Premium contexts.

Feature-toggled: Only active when FEATURE_SIGNAL_ENGINE=true.

Cost guardrails:
  - Max 200 LLM calls per customer per day
  - Max 50 LLM calls per account per day
  - Cache TTL: 1 hour for near-identical signals
  - ~$0.003 per signal (Claude Sonnet)
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

# ============================================================
# Configuration
# ============================================================

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_CALLS_PER_CUSTOMER_PER_DAY = 200
MAX_CALLS_PER_ACCOUNT_PER_DAY = 50
CONFIDENCE_THRESHOLD = 0.6  # Below this → requires_review=True

# Intent codes recognized by the system
VALID_INTENTS = [
    'renewal_risk', 'expansion_interest', 'champion_change',
    'product_frustration', 'feature_request', 'executive_escalation',
    'pricing_concern', 'competitor_mention', 'deployment_blocker',
    'nps_drop_indicator', 'positive_advocacy',
]

# ============================================================
# Vertical-aware prompt templates
# ============================================================

VERTICAL_CONTEXT = {
    'dc2_s': {
        'name': 'Data Center Hardware Infrastructure',
        'description': 'GPU/AI compute, rack deployment, thermal management, power capacity',
        'pillars': 'P1 Deployment Velocity, P2 Operational Stability, P3 AI Workload Performance, P4 Channel & Partner Health, P5 Expansion Readiness',
        'key_terms': 'racks, GPUs, thermal throttling, power draw, deployment, firmware, DCIM, colocation',
    },
    'saas_premium': {
        'name': 'SaaS Premium',
        'description': 'Product-led SaaS with subscription renewals, feature adoption, support tickets',
        'pillars': 'P1 Product Adoption & Usage, P2 Customer Engagement, P3 Customer Sentiment & Support, P4 Partner & Ecosystem Health, P5 Revenue & Growth',
        'key_terms': 'DAU, feature adoption, onboarding, NPS, CSAT, churn, expansion, upsell, seats',
    },
    # Aug 21 2026 vertical-coupling audit (Bug 2): sourced from
    # config/datacenter_v1_kpi_catalog.json's own 'description'/pillars/KPIs —
    # not hand-guessed — so a datacenter_v1 tenant's signals stop being
    # enriched against dc2_s's rack/thermal/colocation framing.
    'datacenter_v1': {
        'name': 'GPU-Rental Neocloud',
        'description': 'GPU-rental neocloud — rent NVIDIA GPUs hourly / reserved clusters; '
                        'health optimized for realized revenue x utilization x goodput x sellable power',
        'pillars': 'P1 Revenue & Unit Economics, P2 Fleet Utilization & Goodput, '
                   'P3 Reliability & SLA Delivery, P4 Power & Facility, '
                   'P5 Commercial & Expansion, P6 Provisioning Velocity',
        'key_terms': 'GPU-hour, utilization, goodput, reserved cluster, SLA attainment, '
                     'fabric error rate, stranded power, PUE, time-to-first-job, silicon refresh',
    },
    # Sourced from config/healthcare_provider_kpi_catalog.json's own pillars/KPIs.
    'healthcare_provider': {
        'name': 'Healthcare Provider',
        'description': 'Patient care delivery, clinical operations, provider satisfaction, '
                        'regulatory compliance',
        'pillars': 'P1 Patient Outcomes, P2 Operational Efficiency, '
                   'P3 Provider Satisfaction, P4 Compliance & Risk',
        'key_terms': 'patient satisfaction, readmission rate, treatment adherence, '
                     'bed utilization, wait time, staff-to-patient ratio, provider NPS, '
                     'EHR uptime, HIPAA compliance, incident response',
    },
}


def _build_generic_vertical_context(vertical: str) -> Dict:
    """Fail-closed fallback for a registered vertical with no curated
    VERTICAL_CONTEXT entry yet — derives context from the vertical's own
    KPI catalog (via vertical_registry) instead of silently borrowing
    dc2_s's rack/GPU-hardware framing (Aug 21 2026 vertical-coupling audit,
    Bug 2). If the catalog itself can't be loaded, returns a neutral,
    vertical-agnostic stub — never dc2_s's context.
    """
    try:
        from utils.vertical_registry import get_pillars, get_kpis
        pillars = get_pillars(vertical) or {}
        kpis = get_kpis(vertical) or {}
        pillar_str = ', '.join(
            f"{pid} {pdef.get('name', pid)}" for pid, pdef in sorted(pillars.items())
        ) or '(no pillars registered)'
        kpi_names = [kdef.get('name') for kdef in kpis.values() if kdef.get('name')]
        terms_str = ', '.join(kpi_names[:12]) or '(no KPIs registered)'
        return {
            'name': vertical.replace('_', ' ').title(),
            'description': f'{vertical} vertical (auto-derived from its KPI catalog)',
            'pillars': pillar_str,
            'key_terms': terms_str,
        }
    except Exception as e:
        logger.warning("QSIM enrichment: could not derive vertical context for %r: %s", vertical, e)
        return {
            'name': vertical or 'Unknown Vertical',
            'description': 'No vertical-specific context available',
            'pillars': '(unavailable)',
            'key_terms': '(unavailable)',
        }

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

If confidence for any field is below 0.6, set requires_review to true.
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


# ============================================================
# Cost tracking (in-memory for Phase 2, Redis/DB in Phase 3)
# ============================================================

_call_counts: Dict[str, Dict[str, int]] = {}  # {date_key: {customer_X: count, account_Y: count}}


def _check_rate_limit(customer_id: int, account_id: int) -> Optional[str]:
    """Check if customer/account has exceeded daily LLM call limits.

    Returns error message if limit exceeded, None if OK.
    """
    today = datetime.utcnow().strftime('%Y-%m-%d')
    if today not in _call_counts:
        _call_counts.clear()  # Reset on new day
        _call_counts[today] = {}

    counts = _call_counts[today]
    cust_key = f"customer_{customer_id}"
    acct_key = f"account_{account_id}"

    if counts.get(cust_key, 0) >= MAX_CALLS_PER_CUSTOMER_PER_DAY:
        return f"Customer {customer_id} exceeded daily limit ({MAX_CALLS_PER_CUSTOMER_PER_DAY} calls/day)"
    if counts.get(acct_key, 0) >= MAX_CALLS_PER_ACCOUNT_PER_DAY:
        return f"Account {account_id} exceeded daily limit ({MAX_CALLS_PER_ACCOUNT_PER_DAY} calls/day)"

    return None


def _record_call(customer_id: int, account_id: int):
    """Record an LLM call for rate limiting."""
    today = datetime.utcnow().strftime('%Y-%m-%d')
    if today not in _call_counts:
        _call_counts.clear()
        _call_counts[today] = {}

    counts = _call_counts[today]
    cust_key = f"customer_{customer_id}"
    acct_key = f"account_{account_id}"
    counts[cust_key] = counts.get(cust_key, 0) + 1
    counts[acct_key] = counts.get(acct_key, 0) + 1


# ============================================================
# Qdrant Context Retrieval (RAG for enrichment)
# ============================================================

def _retrieve_similar_signals(
    customer_id: int,
    account_id: int,
    raw_text: str,
    top_k: int = 5,
) -> tuple:
    """Search Qdrant for similar past signals to provide context.

    Returns:
        (similar_signals_block, dedup_instruction, similar_signals_list)
        - similar_signals_block: formatted string for prompt injection
        - dedup_instruction: instruction if potential duplicates found
        - similar_signals_list: raw list of matches for post-processing
    """
    similar_signals: List[dict] = []

    try:
        from utils.qdrant_signal_search import SignalVectorStore
        store = SignalVectorStore(customer_id)
        similar_signals = store.search(
            query=raw_text[:500],  # Use signal text as semantic query
            account_id=account_id,
            top_k=top_k,
            threshold=0.4,  # Lower threshold to catch more context
        )
        if similar_signals:
            logger.debug(
                "QSIM enrichment: Qdrant returned %d similar signals for account %d",
                len(similar_signals), account_id,
            )
    except Exception as e:
        logger.debug("QSIM enrichment: Qdrant unavailable, enriching without context: %s", e)

    # Fall back to SQL if Qdrant unavailable
    if not similar_signals:
        try:
            from models import ContextNode
            recent = (
                ContextNode.query
                .filter_by(account_id=account_id, node_type='SIGNAL')
                .order_by(ContextNode.occurred_at.desc())
                .limit(top_k)
                .all()
            )
            similar_signals = [
                {
                    'title': n.title or '(no title)',
                    'subtype': n.node_subtype or 'signal',
                    'score': 0.5,  # No similarity score from SQL
                    'sentiment': (n.properties or {}).get('sentiment', 'unknown'),
                }
                for n in recent
            ]
        except Exception:
            pass  # Proceed without context

    # Build prompt block
    if similar_signals:
        lines = []
        for s in similar_signals[:5]:
            score = s.get('score', 0)
            lines.append(
                f"  - [{s.get('subtype', 'signal')}] {s.get('title', '?')} "
                f"(similarity: {score:.0%}, sentiment: {s.get('sentiment', '?')})"
            )
        similar_block = (
            "\nRECENT SIMILAR SIGNALS FOR THIS ACCOUNT (from semantic search):\n"
            + "\n".join(lines)
            + "\n\nUse these to:\n"
            "  1. Detect if the new signal is a DUPLICATE of an existing one\n"
            "  2. Understand the account's recent trajectory and context\n"
            "  3. Correlate this signal with existing patterns\n"
        )

        # Check for high-similarity matches that might be duplicates
        high_sim = [s for s in similar_signals if s.get('score', 0) >= 0.85]
        if high_sim:
            dedup = (
                "\nDUPLICATE CHECK: One or more existing signals have >85% similarity. "
                "If this new signal conveys the SAME information, set is_duplicate=true "
                "and explain in duplicate_reason. Do NOT set is_duplicate for signals "
                "that are related but contain NEW information.\n"
            )
        else:
            dedup = ""
    else:
        similar_block = ""
        dedup = ""

    return similar_block, dedup, similar_signals


# ============================================================
# LLM Enrichment
# ============================================================

def enrich_signal(
    signal_id: str,
    raw_text: str,
    account_id: int,
    customer_id: int,
    vertical: str = 'dc2_s',
) -> Dict:
    """Enrich a raw signal using Qdrant context retrieval + Claude Sonnet.

    Flow: Qdrant semantic search → context stuffing → Claude LLM → structured output.
    Falls back to Claude-only if Qdrant unavailable.

    Returns enrichment dict with sentiment, intent, urgency, etc.
    On failure, returns partial result with requires_review=True.

    Args:
        signal_id: The signal UUID
        raw_text: Original communication text
        account_id: Account ID
        customer_id: Customer ID
        vertical: Customer vertical ('dc2_s' or 'saas_premium')

    Returns:
        Dict with enrichment fields matching QualitativeSignal columns
    """
    # Rate limit check
    limit_error = _check_rate_limit(customer_id, account_id)
    if limit_error:
        logger.warning("QSIM rate limit: %s", limit_error)
        return {
            'requires_review': True,
            'confidence': {'rate_limited': True},
            'suggested_action': f'Rate limited: {limit_error}',
        }

    # Step 1: Retrieve similar signals from Qdrant for context
    similar_block, dedup_instruction, similar_signals = _retrieve_similar_signals(
        customer_id, account_id, raw_text,
    )

    # Step 2: Build vertical-aware prompt WITH Qdrant context
    # Aug 21 2026 vertical-coupling audit (Bug 2): used to fall back to
    # dc2_s's context for ANY unrecognized vertical — now derives a real
    # context from that vertical's own KPI catalog instead.
    v_ctx = VERTICAL_CONTEXT.get(vertical) or _build_generic_vertical_context(vertical)
    prompt = ENRICHMENT_PROMPT.format(
        vertical_name=v_ctx['name'],
        vertical_description=v_ctx['description'],
        vertical_pillars=v_ctx['pillars'],
        vertical_terms=v_ctx['key_terms'],
        similar_signals_block=similar_block,
        dedup_instruction=dedup_instruction,
        intent_codes=', '.join(VALID_INTENTS),
        raw_text=raw_text[:3000],  # Truncate for cost control
    )

    # Call Claude
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        logger.warning("QSIM enrichment: ANTHROPIC_API_KEY not set — returning stub enrichment")
        return _stub_enrichment(raw_text)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        # Cost tracking — canonical path via llm_budget_controller.
        try:
            from utils.llm_budget_controller import record_usage as _record_usage
            _record_usage(
                customer_id=customer_id,
                module='signal_engine_enrichment',
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                model=DEFAULT_MODEL,
                success=True,
            )
        except Exception as _cost_err:
            logger.debug('signal_engine_enrichment: cost tracking failed: %s', _cost_err)

        _record_call(customer_id, account_id)

        # Parse response
        content = response.content[0].text.strip()
        enrichment = json.loads(content)

        # Validate and sanitize
        enrichment = _validate_enrichment(enrichment)
        enrichment['llm_model_version'] = DEFAULT_MODEL

        # Track context source for observability
        enrichment['_context_source'] = 'qdrant' if similar_signals else 'sql_fallback'
        enrichment['_similar_signal_count'] = len(similar_signals)

        logger.info(
            "QSIM enrichment complete: signal=%s intents=%s urgency=%.2f review=%s "
            "context=%s(%d) duplicate=%s",
            signal_id, enrichment.get('intent_signals', []),
            enrichment.get('urgency_score', 0), enrichment.get('requires_review', False),
            enrichment.get('_context_source', 'none'), len(similar_signals),
            enrichment.get('is_duplicate', False),
        )

        return enrichment

    except json.JSONDecodeError as e:
        logger.warning("QSIM enrichment: invalid JSON from LLM for signal %s: %s", signal_id, e)
        return {
            'requires_review': True,
            'confidence': {'parse_error': True},
            'suggested_action': 'LLM returned invalid JSON — manual review required',
            'llm_model_version': DEFAULT_MODEL,
        }

    except Exception as e:
        logger.exception("QSIM enrichment failed for signal %s: %s", signal_id, e)
        return {
            'requires_review': True,
            'confidence': {'error': str(e)},
            'suggested_action': f'Enrichment error: {str(e)[:100]}',
        }


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
        v < CONFIDENCE_THRESHOLD
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


# ============================================================
# Batch enrichment (for processing queue)
# ============================================================

def enrich_pending_signals(customer_id: int, limit: int = 20) -> Dict:
    """Enrich all un-enriched signals for a customer.

    Processes signals that have source_type set (QSIM-ingested)
    but no intent_signals (not yet enriched).

    Args:
        customer_id: Customer to process
        limit: Max signals to enrich in this batch

    Returns:
        Summary dict with counts
    """
    try:
        from extensions import db
        from models import QualitativeSignal, Account
        from utils.vertical_registry import get_vertical_for_customer

        vertical = get_vertical_for_customer(customer_id)

        # Get account IDs for this customer
        account_ids = [a.account_id for a in
                       Account.query.filter_by(customer_id=customer_id)
                       .with_entities(Account.account_id).all()]

        if not account_ids:
            return {'status': 'no_accounts', 'enriched': 0}

        # Find un-enriched QSIM signals
        signals = QualitativeSignal.query.filter(
            QualitativeSignal.account_id.in_(account_ids),
            QualitativeSignal.source_type.isnot(None),  # QSIM-ingested
            QualitativeSignal.intent_signals.is_(None),  # Not yet enriched
        ).limit(limit).all()

        if not signals:
            return {'status': 'nothing_to_enrich', 'enriched': 0}

        enriched_count = 0
        errors = 0

        for sig in signals:
            raw = sig.raw_text or sig.content or ''
            if not raw:
                continue

            result = enrich_signal(
                signal_id=sig.signal_id,
                raw_text=raw,
                account_id=sig.account_id,
                customer_id=customer_id,
                vertical=vertical,
            )

            # Apply enrichment to signal
            for field in ('sentiment_score', 'relationship_sentiment', 'product_sentiment',
                          'urgency_score', 'escalation_probability', 'intent_signals',
                          'stakeholder_roles', 'suggested_action', 'confidence',
                          'requires_review', 'llm_model_version'):
                if field in result:
                    try:
                        setattr(sig, field, result[field])
                    except Exception:
                        pass

            # Run structural urgency classifier
            try:
                from signal_engine.urgency import (
                    classify_structural_urgency, resolve_effective_urgency,
                    AccountContext, SignalContext,
                )
                acct_ctx = AccountContext(account_id=sig.account_id)
                sig_ctx = SignalContext(
                    intent_signals=result.get('intent_signals', []),
                    health_delta=0,  # Would need actual health data
                    llm_urgency_score=result.get('urgency_score', 0.5),
                    escalation_probability=result.get('escalation_probability', 0),
                )
                structural = classify_structural_urgency(sig_ctx, acct_ctx)
                effective = resolve_effective_urgency(
                    structural,
                    result.get('urgency_score', 0.5),
                    result.get('escalation_probability', 0),
                )
                sig.structural_urgency = structural
                sig.effective_urgency = effective
            except Exception as e:
                logger.debug("Urgency classification skipped: %s", e)

            # Map sentiment to existing sentiment column
            score = result.get('sentiment_score', 0)
            if score > 0.2:
                sig.sentiment = 'positive'
            elif score < -0.2:
                sig.sentiment = 'negative'
            else:
                sig.sentiment = 'neutral'

            enriched_count += 1

        db.session.commit()

        return {
            'status': 'completed',
            'enriched': enriched_count,
            'errors': errors,
            'customer_id': customer_id,
            'vertical': vertical,
        }

    except Exception as e:
        logger.exception("Batch enrichment failed: %s", e)
        return {'status': 'error', 'error': str(e), 'enriched': 0}
