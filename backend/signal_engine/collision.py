#!/usr/bin/env python3
"""
QSIM Signal Engine — Context Graph Collision Check.

When a QSIM signal arrives, check if a matching context graph node
already exists. If so, enrich the existing node instead of creating
a duplicate alert.

Tier 1 pre-emption: CG nodes with high-impact subtypes (champion_loss,
executive_escalation, churn_risk) fire immediate alerts, bypassing
the composite fusion pipeline entirely.

Feature-toggled: Only active when FEATURE_SIGNAL_ENGINE=true.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


# ============================================================
# Intent-to-CG-Node subtype mapping
# ============================================================

INTENT_TO_CG_SUBTYPES: Dict[str, List[str]] = {
    'executive_escalation': ['champion_loss', 'executive_escalation'],
    'champion_change': ['champion_loss', 'stakeholder_change'],
    'renewal_risk': ['churn_risk', 'renewal_signal'],
    'product_frustration': ['kpi_change', 'ticket'],
    'expansion_interest': ['expansion_signal'],
    'deployment_blocker': ['ticket', 'kpi_change'],
}

# Intents with no CG equivalent — always create new QUALITATIVE_SIGNAL node
INTENTS_WITHOUT_CG = {'competitor_mention', 'pricing_concern', 'feature_request', 'nps_drop_indicator', 'positive_advocacy'}

# Dedup windows per intent category
DEDUP_WINDOWS: Dict[str, int] = {
    'executive_escalation': 72,   # hours
    'champion_change': 72,
    'renewal_risk': 168,          # 7 days
    'expansion_interest': 168,
    'product_frustration': 48,
    'deployment_blocker': 48,
    'competitor_mention': 168,
    'pricing_concern': 168,
    'feature_request': 168,
    'nps_drop_indicator': 168,
    'positive_advocacy': 168,
}


def check_cg_collision(
    account_id: int,
    intent_signals: List[str],
    db_session=None,
) -> Optional[Dict]:
    """Check if any QSIM intent matches an existing context graph node.

    Returns collision info dict if match found, None otherwise.
    When a collision is detected, the QSIM signal should enrich the
    existing CG node rather than creating a new alert.

    Args:
        account_id: The account to check
        intent_signals: List of detected QSIM intent codes
        db_session: SQLAlchemy session (uses default if None)

    Returns:
        {
            'collision': True,
            'cg_node_id': int,
            'cg_node_subtype': str,
            'matched_intent': str,
            'action': 'enrich' | 'suppress',
            'reason': str,
        }
        or None if no collision.
    """
    if not intent_signals:
        return None

    try:
        from models import ContextNode
        if db_session is None:
            from extensions import db
            db_session = db.session

        for intent in intent_signals:
            cg_subtypes = INTENT_TO_CG_SUBTYPES.get(intent)
            if not cg_subtypes:
                continue  # No CG equivalent for this intent

            window_hours = DEDUP_WINDOWS.get(intent, 168)
            cutoff = datetime.utcnow() - timedelta(hours=window_hours)

            # Find matching CG node within dedup window
            node = ContextNode.query.filter(
                ContextNode.account_id == account_id,
                ContextNode.node_subtype.in_(cg_subtypes),
                ContextNode.occurred_at >= cutoff,
            ).order_by(ContextNode.occurred_at.desc()).first()

            if node:
                logger.info(
                    "CG collision: account=%s intent=%s matched node=%s (subtype=%s)",
                    account_id, intent, node.node_id, node.node_subtype,
                )
                return {
                    'collision': True,
                    'cg_node_id': node.node_id,
                    'cg_node_subtype': node.node_subtype,
                    'matched_intent': intent,
                    'action': 'enrich',
                    'reason': f"QSIM intent '{intent}' matches CG node {node.node_id} "
                              f"(subtype={node.node_subtype}, occurred {node.occurred_at})",
                }

    except ImportError:
        logger.debug("ContextNode model not available — skipping CG collision check")
    except Exception as e:
        logger.warning("CG collision check failed: %s", e)

    return None


def should_create_qualitative_node(intent_signals: List[str]) -> bool:
    """Check if any intent requires a new QUALITATIVE_SIGNAL CG node.

    Intents without CG equivalents (competitor_mention, pricing_concern,
    feature_request) should create new context graph nodes of type
    SIGNAL with subtype qualitative_signal.
    """
    return bool(set(intent_signals or []) & INTENTS_WITHOUT_CG)
