#!/usr/bin/env python3
"""
QSIM Signal Engine — Composite Score Fusion.

Fuses quantitative health (KPI-based) with qualitative signals
(QSIM-enriched) to produce a composite health modifier.

Key features:
- Cold-start ramp: qual_weight scales from 0% to target over first 100 signals
- Vertical-specific sub-weights (DC2_S vs SaaS Premium)
- Pillar-level contribution mapping (qual dimensions → specific pillars)

Feature-toggled: Only active when FEATURE_SIGNAL_ENGINE=true.
"""

import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ============================================================
# Default fusion weights
# ============================================================

DEFAULT_QUANT_WEIGHT = 0.70
DEFAULT_QUAL_WEIGHT = 0.30
MIN_SIGNALS_FOR_FULL_WEIGHT = 100  # Cold-start ramp threshold

# Vertical-specific qualitative sub-weights
VERTICAL_QUAL_SUBWEIGHTS = {
    'dc2_s': {
        'relationship_sentiment': 0.30,
        'product_sentiment': 0.40,
        'urgency_inverted': 0.20,
        'escalation_inverted': 0.10,
    },
    'saas_premium': {
        'relationship_sentiment': 0.45,
        'product_sentiment': 0.25,
        'urgency_inverted': 0.20,
        'escalation_inverted': 0.10,
    },
}

# Pillar contribution mapping
PILLAR_CONTRIBUTION = {
    'relationship_sentiment': 'P4',   # Channel & Partner Health
    'product_sentiment': 'P3',        # AI Workload Perf / Customer Sentiment
    'deployment_blocker': 'P1',       # Deployment Velocity (negative modifier)
    'expansion_interest': 'P5',       # Expansion Readiness (positive modifier)
}


@dataclass
class QualScoreComponents:
    """Individual qualitative score components."""
    relationship_sentiment: float = 0.0    # -1.0 to +1.0
    product_sentiment: float = 0.0         # -1.0 to +1.0
    urgency_score: float = 0.5             # 0.0 to 1.0 (inverted for scoring)
    escalation_probability: float = 0.0    # 0.0 to 1.0 (inverted for scoring)


@dataclass
class FusionResult:
    """Result of composite score fusion."""
    quant_health: float
    qual_score: float
    qual_weight: float        # Actual weight after cold-start ramp
    composite_score: float
    score_velocity: Optional[float] = None  # 7-day delta
    cold_start_ramp: float = 1.0  # 0.0 to 1.0 (how far into ramp)
    contributing_signals: int = 0


def get_cold_start_ramp(account_id: int, target_qual_weight: float = DEFAULT_QUAL_WEIGHT) -> Tuple[float, float]:
    """Calculate cold-start ramp for qualitative weight.

    Prevents health score crashes when QSIM is first enabled by
    ramping qual_weight from 0% to target over first 100 signals.

    Returns: (actual_qual_weight, ramp_factor)
    """
    try:
        from models import QualitativeSignal
        from sqlalchemy import func

        signal_count = QualitativeSignal.query.filter(
            QualitativeSignal.account_id == account_id,
            QualitativeSignal.source_type.in_(['slack', 'email', 'transcript']),
        ).count()
    except Exception:
        signal_count = 0

    ramp = min(1.0, signal_count / MIN_SIGNALS_FOR_FULL_WEIGHT)
    actual_weight = target_qual_weight * ramp

    return actual_weight, ramp


def compute_qual_score(
    components: QualScoreComponents,
    vertical: str = 'dc2_s',
) -> float:
    """Compute qualitative health score from enriched signal components.

    Formula:
      qual_score = (relationship_sentiment_norm × w1)
                 + (product_sentiment_norm × w2)
                 + (urgency_inverted_norm × w3)
                 + (escalation_inverted_norm × w4)

    All inputs normalized to 0-100 scale.
    """
    subweights = VERTICAL_QUAL_SUBWEIGHTS.get(vertical, VERTICAL_QUAL_SUBWEIGHTS['dc2_s'])

    # Normalize -1..+1 sentiment to 0..100
    rel_norm = (components.relationship_sentiment + 1.0) * 50.0
    prod_norm = (components.product_sentiment + 1.0) * 50.0

    # Invert urgency and escalation (lower = better health)
    urgency_inv = (1.0 - components.urgency_score) * 100.0
    escalation_inv = (1.0 - components.escalation_probability) * 100.0

    qual_score = (
        rel_norm * subweights['relationship_sentiment']
        + prod_norm * subweights['product_sentiment']
        + urgency_inv * subweights['urgency_inverted']
        + escalation_inv * subweights['escalation_inverted']
    )

    return max(0.0, min(100.0, qual_score))


def fuse_scores(
    quant_health: float,
    components: QualScoreComponents,
    account_id: int,
    vertical: str = 'dc2_s',
    target_qual_weight: float = DEFAULT_QUAL_WEIGHT,
) -> FusionResult:
    """Fuse quantitative health with qualitative signals.

    Composite = (quant_health × quant_weight) + (qual_score × qual_weight)

    With cold-start ramp: qual_weight scales from 0% to target over
    first 100 QSIM signals per account.

    Args:
        quant_health: Current quantitative health score (0-100)
        components: Qualitative score components
        account_id: Account ID (for cold-start ramp lookup)
        vertical: Customer vertical ('dc2_s' or 'saas_premium')
        target_qual_weight: Target qualitative weight (default 0.30)

    Returns:
        FusionResult with composite score and metadata
    """
    qual_score = compute_qual_score(components, vertical)
    qual_weight, ramp = get_cold_start_ramp(account_id, target_qual_weight)
    quant_weight = 1.0 - qual_weight

    composite = (quant_health * quant_weight) + (qual_score * qual_weight)
    composite = max(0.0, min(100.0, composite))

    return FusionResult(
        quant_health=quant_health,
        qual_score=round(qual_score, 2),
        qual_weight=round(qual_weight, 4),
        composite_score=round(composite, 2),
        cold_start_ramp=round(ramp, 2),
    )


def compute_pillar_modifiers(
    components: QualScoreComponents,
    intent_signals: list = None,
) -> Dict[str, float]:
    """Compute per-pillar modifiers from qualitative signals.

    Maps qualitative dimensions to specific pillars as health modifiers.
    Returns dict of {pillar_code: modifier} where modifier is -10 to +10.

    These modifiers are ADDITIVE to the existing pillar score and only
    applied when FEATURE_SIGNAL_ENGINE=true.
    """
    modifiers = {}

    # P4: Channel & Partner Health ← relationship_sentiment
    if components.relationship_sentiment != 0.0:
        modifiers['P4'] = round(components.relationship_sentiment * 10, 2)

    # P3: AI Workload / Customer Sentiment ← product_sentiment
    if components.product_sentiment != 0.0:
        modifiers['P3'] = round(components.product_sentiment * 10, 2)

    # P1: Deployment Velocity ← deployment_blocker intent
    if intent_signals and 'deployment_blocker' in intent_signals:
        modifiers['P1'] = -5.0  # Negative modifier

    # P5: Expansion Readiness ← expansion_interest intent
    if intent_signals and 'expansion_interest' in intent_signals:
        modifiers['P5'] = 5.0  # Positive modifier

    return modifiers
