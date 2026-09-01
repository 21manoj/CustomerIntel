#!/usr/bin/env python3
"""
QSIM Signal Engine — Structural Urgency Classifier.

Deterministic business-logic classifier that pre-empts LLM urgency scoring.
Uses account context (ARR, days-to-renewal, health delta) to assign
urgency that LLM-perceived sentiment cannot override.

Example: A calm email from a CTO saying "we're evaluating alternatives"
has low LLM-perceived urgency (polite tone) but critical structural
urgency (competitor eval + CTO involvement + high ARR).

Feature-toggled: Only active when FEATURE_SIGNAL_ENGINE=true.
"""

import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ============================================================
# Configuration thresholds (overridable via CustomerConfig)
# ============================================================

# ARR threshold for high-value account urgency boost
ARR_HIGH_THRESHOLD = 2_000_000  # $2M

# Tier 1 subtypes that always get 'critical' urgency
TIER_1_SUBTYPES = {'champion_loss', 'executive_escalation', 'churn_risk'}

# Intent codes that map to structural urgency
HIGH_URGENCY_INTENTS = {'executive_escalation', 'champion_change', 'renewal_risk'}


@dataclass
class AccountContext:
    """Account-level context for urgency classification."""
    account_id: int
    arr: float = 0.0
    days_to_renewal: int = 365
    health_score: float = 70.0
    health_delta_7d: float = 0.0  # 7-day health score change


@dataclass
class SignalContext:
    """Signal-level context for urgency classification."""
    node_subtype: Optional[str] = None
    intent_signals: list = None
    health_delta: float = 0.0
    llm_urgency_score: float = 0.5
    escalation_probability: float = 0.0

    def __post_init__(self):
        if self.intent_signals is None:
            self.intent_signals = []


def classify_structural_urgency(
    signal: SignalContext,
    account: AccountContext,
) -> Optional[str]:
    """Classify structural urgency from business rules.

    Returns urgency level ('critical', 'high', 'medium', 'low') or None
    if no structural rule applies (fall through to LLM urgency).

    Rules evaluated in priority order:
    1. Tier 1 CG subtypes → always critical
    2. Near-renewal + health declining → critical
    3. High-ARR account + health declining → high
    4. Large health drop (>15 points) → high
    5. High-urgency intent codes → medium (structural floor)
    """
    # Rule 1: Tier 1 subtypes are always critical
    if signal.node_subtype in TIER_1_SUBTYPES:
        logger.info("Structural urgency: CRITICAL (Tier 1 subtype: %s)", signal.node_subtype)
        return 'critical'

    # Rule 2: Near-renewal + declining health
    if account.days_to_renewal <= 30 and signal.health_delta < -10:
        logger.info(
            "Structural urgency: CRITICAL (renewal in %d days, health delta %.1f)",
            account.days_to_renewal, signal.health_delta,
        )
        return 'critical'

    # Rule 3: High-ARR account + declining health
    if account.arr > ARR_HIGH_THRESHOLD and signal.health_delta < -5:
        logger.info(
            "Structural urgency: HIGH (ARR $%.0f > threshold, health delta %.1f)",
            account.arr, signal.health_delta,
        )
        return 'high'

    # Rule 4: Large health drop
    if signal.health_delta < -15:
        logger.info("Structural urgency: HIGH (health delta %.1f < -15)", signal.health_delta)
        return 'high'

    # Rule 5: High-urgency intent codes provide a structural floor
    if signal.intent_signals:
        matching = set(signal.intent_signals) & HIGH_URGENCY_INTENTS
        if matching:
            logger.info("Structural urgency: MEDIUM (intent floor: %s)", matching)
            return 'medium'

    # No structural rule → fall through to LLM urgency
    return None


def resolve_effective_urgency(
    structural: Optional[str],
    llm_urgency_score: float,
    escalation_probability: float,
) -> str:
    """Resolve effective urgency = max(structural, perceived).

    LLM urgency is mapped to levels:
      >= 0.8 → critical
      >= 0.6 → high
      >= 0.4 → medium
      <  0.4 → low

    Escalation probability >= 0.7 boosts by one level.

    Returns: 'critical', 'high', 'medium', or 'low'
    """
    URGENCY_ORDER = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}
    ORDER_TO_URGENCY = {v: k for k, v in URGENCY_ORDER.items()}

    # Map LLM score to level
    if llm_urgency_score >= 0.8:
        perceived = 'critical'
    elif llm_urgency_score >= 0.6:
        perceived = 'high'
    elif llm_urgency_score >= 0.4:
        perceived = 'medium'
    else:
        perceived = 'low'

    # Escalation boost
    if escalation_probability >= 0.7:
        perceived_idx = min(3, URGENCY_ORDER[perceived] + 1)
        perceived = ORDER_TO_URGENCY[perceived_idx]

    # Resolve: max(structural, perceived)
    structural_val = URGENCY_ORDER.get(structural, -1)
    perceived_val = URGENCY_ORDER[perceived]

    effective_val = max(structural_val, perceived_val)
    return ORDER_TO_URGENCY[effective_val]
