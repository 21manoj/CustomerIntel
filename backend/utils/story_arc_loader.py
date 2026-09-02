"""
Story-arc manifests (config/story_arcs/) as EXPECTED-PATH overlays.

Ported 2026-09-02 (Tier 2A-5) from the old repo's 374-line loader/validator,
trimmed to what the new build uses. The manifests keep their value as
"what typically happens in this arc" — phase durations, health ranges,
the decisions that usually get made, the revenue narrative — and are
attached to a journey as `expected_path` so a real account can be shown
against its typical trajectory and its deviation from it measured.

Nothing from a manifest is written to context_nodes / context_edges any
more. The old repo's arc_decision_generator / arc_edge_generator turned
these templates into synthetic DECISION nodes and ordinal-bound causal
edges in the shared graph; not ported — see docs/design/wizard-a-assessment.md §2.3.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ARCS_DIR = Path(__file__).resolve().parent.parent / 'config' / 'story_arcs'

CANONICAL_ARCS = (
    'exec_sponsor_change', 'crisis_recovery', 'stalled_deployment',
    'competitive_displacement', 'silent_churn', 'land_and_expand',
    'expansion_champion', 'seasonal_surge',
)

# Older labels (load-driver / legacy classifier) → canonical manifest id.
ARC_ALIASES = {
    'champion_loss': 'exec_sponsor_change',
    'competitor_evaluation': 'competitive_displacement',
    'ignored_churn': 'silent_churn',
    'proactive_growth': 'expansion_champion',
    'infrastructure_decay': 'stalled_deployment',
    'engagement_decline': 'silent_churn',
    'budget_pressure': 'competitive_displacement',
    'steady_performer': 'seasonal_surge',
    'crisis': 'crisis_recovery',
}

_cache: Dict[str, Optional[dict]] = {}


def canonical_arc(arc_type: Optional[str]) -> Optional[str]:
    if not arc_type:
        return None
    a = arc_type.strip().lower()
    if a.startswith('arc_'):
        a = a[4:]
    a = ARC_ALIASES.get(a, a)
    return a if a in CANONICAL_ARCS else None


def load_arc(arc_type: str) -> Optional[Dict[str, Any]]:
    """Load a manifest by canonical arc type (aliases accepted). None if unknown."""
    canon = canonical_arc(arc_type)
    if not canon:
        return None
    if canon in _cache:
        return _cache[canon]
    path = ARCS_DIR / f'arc_{canon}.json'
    data = None
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.error('Invalid story arc JSON %s: %s', path, e)
    _cache[canon] = data
    return data


def load_all_arcs() -> Dict[str, Dict[str, Any]]:
    return {a: m for a in CANONICAL_ARCS if (m := load_arc(a))}


def expected_path(arc_type: str) -> Optional[Dict[str, Any]]:
    """The overlay attached to a journey: typical phases, decisions, and the
    revenue narrative — read-only template content, labelled as such."""
    arc = load_arc(arc_type)
    if not arc:
        return None
    phases: List[dict] = []
    week = 0
    for p in arc.get('phases', []):
        dur = int(p.get('duration_weeks') or 0)
        hr = p.get('health_range') or {}
        phases.append({
            'phase_id': p.get('phase_id'),
            'name': p.get('name'),
            'starts_week': week,
            'duration_weeks': dur,
            'health_start': hr.get('start'),
            'health_end': hr.get('end'),
            'description': p.get('description'),
        })
        week += dur
    return {
        'arc_type': canonical_arc(arc_type),
        'arc_name': arc.get('arc_name'),
        'source': 'story_arc_template',
        'note': 'Typical path for this arc from the reference narrative — a prior, not this account\'s data.',
        'total_weeks': week,
        'phases': phases,
        'typical_decisions': [
            {
                'phase_id': d.get('phase_id'),
                'week': d.get('week'),
                'title': d.get('title'),
                'decision_maker_role': d.get('decision_maker_role'),
            }
            for d in arc.get('decisions', [])
        ],
        'revenue_narrative': arc.get('revenue_narrative'),
    }


def reset_cache() -> None:
    _cache.clear()
