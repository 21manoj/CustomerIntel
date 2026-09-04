"""
Urgency — a structural floor from the signal's taxonomy role, combined
with the LLM's perceived urgency.

    structural = classify_structural_urgency(role)          # config: urgency.structural_by_role
    effective  = resolve_effective_urgency(structural, urgency_score, escalation_probability)

Why a floor: a calm note from a CTO saying "we're evaluating alternatives"
reads as low urgency to a sentiment model but is an `escalation` /
`commercial_pressure` role — the role, not the tone, sets the minimum.

v2 (2026-09-04): keyed on taxonomy roles instead of the retired
`champion_loss` / `churn_risk` subtypes, and the account-context rules
(days-to-renewal, ARR, health delta) were removed — every caller passed
zeros, so they never fired. Renewal proximity belongs to the journey.
"""
from __future__ import annotations

import logging
from typing import Optional

from signal_engine import settings

logger = logging.getLogger(__name__)

LEVELS = ('low', 'medium', 'high', 'critical')
_ORDER = {lvl: i for i, lvl in enumerate(LEVELS)}


def classify_structural_urgency(role: Optional[str]) -> Optional[str]:
    """The floor for a taxonomy role, or None when the role carries none
    (routine, advocacy, expansion_intent …) — the perceived level then stands alone."""
    if not role:
        return None
    return settings.get('urgency', 'structural_by_role').get(role)


def perceived_urgency(urgency_score: Optional[float], escalation_probability: Optional[float]) -> str:
    bands = settings.get('urgency', 'perceived_bands')
    score = float(urgency_score or 0.0)
    level = 'low'
    for lvl in ('critical', 'high', 'medium'):
        if score >= bands[lvl]:
            level = lvl
            break
    if float(escalation_probability or 0.0) >= settings.get('urgency', 'escalation_boost_threshold'):
        level = LEVELS[min(len(LEVELS) - 1, _ORDER[level] + 1)]
    return level


def resolve_effective_urgency(structural: Optional[str], urgency_score: Optional[float],
                              escalation_probability: Optional[float]) -> str:
    """max(structural floor, perceived level)."""
    perceived = perceived_urgency(urgency_score, escalation_probability)
    if structural not in _ORDER:
        return perceived
    return LEVELS[max(_ORDER[structural], _ORDER[perceived])]
