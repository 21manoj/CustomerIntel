"""
Evidence-cited arc classification.

Every rule is written against signal ROLES (taxonomy), health features,
and renewal proximity — never literal subtypes — and returns the episode
ids that satisfied it. There is no fallback: an account with no
arc-evidencing episodes is `steady` (healthy, flat, quiet) or
`unclassified` (something moved, nothing matched — with the observed roles
listed so the gap is visible). Confidence values are rule-match constants
and are labelled as such (`confidence_semantics`), not calibrated
probabilities. Rule order is priority order; the first satisfied rule
wins, the rest are reported as alternatives with what they were missing.

The old classifier's `lambda f: True → competitive_displacement @ 0.55`
default is the single largest source of wrong arcs on live data (14.5% of
all accounts, 42% on datacenter tenants) — see docs/design/wizard-a-assessment.md §2.1.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import utils.health_thresholds as ht
from journeys.journey_builder import Episode

# Roles a rule may cite. A rule's `needs` are role sets ANDed together (any
# episode in each set anywhere in the journey); health predicates run on the
# feature vector. `excludes` are roles whose presence blocks the rule.
#
# Rules read the WHOLE journey, not a trailing window. An arc is a story —
# crisis_recovery needs the crisis (months ago) and the recovery (now); an
# account that lost its champion in July and rebuilt by March is still on
# the exec_sponsor_change arc, in its resolution phase. A first version used
# a 120-day window and left 4 of 10 accounts on live tenant 415
# unclassified because only their recovery-phase signals were still in
# view. What IS window-bound is `steady`: healthy, flat, and no negative
# role in the last STEADY_QUIET_DAYS.
STEADY_QUIET_DAYS = 90


def _f(name: str) -> Callable[[dict], bool]:
    return {
        'below_healthy': lambda f: f['health_now'] is not None and f['health_now'] < ht.healthy_min(),
        'below_at_risk': lambda f: f['health_now'] is not None and f['health_now'] < ht.at_risk_min(),
        'healthy': lambda f: f['health_now'] is not None and f['health_now'] >= ht.healthy_min(),
        'very_healthy': lambda f: f['health_now'] is not None and f['health_now'] >= ht.healthy_min() + 10,
        'declining': lambda f: f['health_slope_1mo'] < -1,
        'declining_3mo': lambda f: f['health_slope_3mo'] < 0,
        'recovering': lambda f: f['dipped_below_at_risk'] and f['health_slope_1mo'] > 3,
        'renewal_near': lambda f: f['days_to_renewal'] is not None and f['days_to_renewal'] < 90,
        'adoption_declining': lambda f: f['adoption_delta_3mo'] is not None and f['adoption_delta_3mo'] < -5,
        'flat': lambda f: abs(f['health_slope_1mo']) <= 2,
        # the journey has (or had) a deterioration/intervention phase — the
        # health-side evidence that a negative arc actually played out
        'had_negative_phase': lambda f: bool(f.get('had_negative_phase')),
    }[name]


RULES: List[dict] = [
    {
        'arc': 'exec_sponsor_change', 'confidence': 0.85,
        'needs': [{'champion_change'}],
        'health_any': ['below_healthy', 'declining', 'had_negative_phase'],
        'needs_or_roles': [{'engagement_decline'}],   # health_any OR one of these
        'excludes': set(),
    },
    {
        'arc': 'crisis_recovery', 'confidence': 0.80,
        'needs': [{'infra_incident', 'escalation'}],
        'health_all': ['below_at_risk_or_dipped'],
        'excludes': set(),
    },
    {
        'arc': 'stalled_deployment', 'confidence': 0.75,
        'needs': [{'infra_incident', 'capacity_pressure', 'delivery_stall'}],
        'health_any': ['adoption_declining', 'below_healthy', 'had_negative_phase'],
        'excludes': {'champion_change'},
    },
    {
        'arc': 'competitive_displacement', 'confidence': 0.75,
        'needs': [{'commercial_pressure'}],
        'health_any': ['declining_3mo', 'renewal_near', 'below_healthy', 'had_negative_phase'],
        'excludes': set(),
    },
    {
        'arc': 'silent_churn', 'confidence': 0.70,
        'needs': [{'engagement_decline', 'usage_decline'}],
        'health_any': ['declining', 'had_negative_phase'],
        'health_all': ['below_healthy_or_had_negative_phase'],
        'excludes': {'infra_incident', 'escalation', 'champion_change', 'commercial_pressure'},
    },
    {
        'arc': 'expansion_champion', 'confidence': 0.80,
        'needs': [{'expansion_intent'}, {'advocacy'}],
        'health_all': ['very_healthy'],
        'excludes': set(),
    },
    {
        'arc': 'land_and_expand', 'confidence': 0.75,
        'needs': [{'expansion_intent', 'expansion_realized'}],
        'health_all': ['healthy'],
        'excludes': set(),
    },
    {
        # Only reachable with an explicit periodicity signal; healthy-and-flat
        # is `steady`, not an arc.
        'arc': 'seasonal_surge', 'confidence': 0.60,
        'needs': [{'routine'}], 'subtype_needed': 'seasonal_pattern',
        'health_all': ['healthy'],
        'excludes': set(),
    },
]

_NEGATIVE_FOR_STEADY = {
    'champion_change', 'engagement_decline', 'usage_decline', 'escalation',
    'infra_incident', 'capacity_pressure', 'delivery_stall', 'commercial_pressure',
}


# Evidence equivalents of the health predicates, used when the account has no
# KPI layer (features['evidence_only']). The arc says so: evidence_scope =
# 'evidence_only'. Same rules, a different witness.
def _very_healthy_ev(f: dict) -> bool:
    cfg = ht.arc_evidence_equivalents()
    pos, neg = f['positive_signals_90d'], f['negative_signals_90d']
    return (pos - neg) >= cfg['very_healthy_net_min'] and neg <= pos * cfg['very_healthy_negative_share_max']


_EVIDENCE_EQUIV = {
    'below_healthy': lambda f: f['net_polarity_90d'] < 0 or bool(f.get('had_negative_phase')),
    'below_at_risk': lambda f: bool(f.get('had_negative_phase')) or bool(f.get('negative_then_recovery_90d')),
    'below_at_risk_or_dipped': lambda f: bool(f.get('had_negative_phase')) or bool(f.get('negative_then_recovery_90d')),
    'below_healthy_or_had_negative_phase': lambda f: f['net_polarity_90d'] < 0 or bool(f.get('had_negative_phase')),
    'declining': lambda f: f['net_polarity_90d'] < 0,
    'declining_3mo': lambda f: f['net_polarity_90d'] < 0 or bool(f.get('had_negative_phase')),
    'recovering': lambda f: (bool(f.get('had_negative_phase')) or bool(f.get('negative_then_recovery_90d'))) and f['recovery_signals_90d'] > 0,
    'healthy': lambda f: f['net_polarity_90d'] > 0 and not f.get('had_negative_phase'),
    'very_healthy': _very_healthy_ev,
    'flat': lambda f: f['negative_signals_90d'] == 0,
}


def _health_pred(name: str, f: dict) -> bool:
    if f.get('evidence_only') and name in _EVIDENCE_EQUIV:
        return _EVIDENCE_EQUIV[name](f)
    if name == 'below_at_risk_or_dipped':
        return _f('below_at_risk')(f) or bool(f['dipped_below_at_risk'])
    if name == 'below_healthy_or_had_negative_phase':
        return _f('below_healthy')(f) or bool(f.get('had_negative_phase'))
    return _f(name)(f)


def _evaluate(rule: dict, f: dict, by_role: Dict[str, List[Episode]]) -> Tuple[bool, List[str], List[str]]:
    """Returns (matched, supporting_episode_ids, missing_conditions)."""
    missing: List[str] = []
    support: List[str] = []
    for role_set in rule['needs']:
        hits = [e for r in role_set for e in by_role.get(r, [])]
        if rule.get('subtype_needed'):
            hits = [e for e in hits if e.subtype == rule['subtype_needed']]
        if hits:
            support.extend(e.episode_id for e in hits)
        else:
            missing.append('role:' + '|'.join(sorted(role_set)))
    for r in rule.get('excludes', ()):
        if by_role.get(r):
            missing.append(f'excluded_role_present:{r}')
    ok_all = all(_health_pred(n, f) for n in rule.get('health_all', []))
    if not ok_all:
        missing.append('health:' + '&'.join(rule['health_all']))
    if rule.get('health_any'):
        ok_any = any(_health_pred(n, f) for n in rule['health_any'])
        if not ok_any and rule.get('needs_or_roles'):
            ok_any = any(by_role.get(r) for rs in rule['needs_or_roles'] for r in rs)
        if not ok_any:
            missing.append('health:' + '|'.join(rule['health_any']))
    return (not missing), support, missing


def classify(features: dict, episodes: List[Episode], taxonomy, as_of: datetime) -> dict:
    by_role: Dict[str, List[Episode]] = {}
    recent_negative: List[str] = []
    quiet_start = as_of - timedelta(days=STEADY_QUIET_DAYS)
    for e in episodes:
        if e.kind == 'signal' and e.role and e.date <= as_of:
            by_role.setdefault(e.role, []).append(e)
            if e.role in _NEGATIVE_FOR_STEADY and e.date > quiet_start:
                recent_negative.append(e.role)
    observed_roles = sorted(by_role)

    matched = None
    alternatives = []
    for rule in RULES:
        ok, support, missing = _evaluate(rule, features, by_role)
        if ok and matched is None:
            matched = {'rule': rule, 'support': sorted(set(support))}
        elif support:
            alternatives.append({'arc_type': rule['arc'], 'present': sorted(set(support)), 'missing': missing})

    if matched:
        r = matched['rule']
        contradicting = []
        neg = [x for x in observed_roles if x in _NEGATIVE_FOR_STEADY]
        pos = [x for x in observed_roles if x in ('expansion_intent', 'advocacy', 'recovery', 'expansion_realized')]
        if r['arc'] in ('expansion_champion', 'land_and_expand') and neg:
            contradicting = [f'negative role present: {x}' for x in neg]
        if r['arc'] in ('exec_sponsor_change', 'crisis_recovery', 'stalled_deployment',
                        'competitive_displacement', 'silent_churn') and pos:
            contradicting = [f'positive role present: {x}' for x in pos]
        return {
            'state': 'classified',
            'arc_type': r['arc'],
            'confidence': r['confidence'],
            'confidence_semantics': 'rule_match_constant',
            'matched_rule': r['arc'],
            'supporting_episode_ids': matched['support'],
            'contradicting_evidence': contradicting,
            'alternatives': alternatives,
            'observed_roles': observed_roles,
            'evidence_scope': 'evidence_only' if features.get('evidence_only') else 'whole_journey',
        }

    healthy = features['health_now'] is not None and features['health_now'] >= ht.healthy_min()
    quiet = not recent_negative and not features.get('had_negative_phase')
    if healthy and _f('flat')(features) and quiet:
        state, reason = 'steady', f'healthy, flat, no negative-role signals in the last {STEADY_QUIET_DAYS} days, no negative phase'
    elif features['health_now'] is None and quiet:
        state, reason = 'steady', f'no KPI layer; no negative-role signals in the last {STEADY_QUIET_DAYS} days, no negative phase (evidence only)'
    elif features['health_now'] is None:
        state, reason = 'unclassified', f"no KPI layer; observed roles {observed_roles or 'none'} — no rule satisfied on the evidence alone"
    else:
        state, reason = 'unclassified', (
            f"observed roles {observed_roles or 'none'} with health "
            f"{features['health_band']} slope_1mo {features['health_slope_1mo']} — no rule satisfied"
        )
    return {
        'state': state,
        'arc_type': None,
        'confidence': None,
        'confidence_semantics': None,
        'matched_rule': None,
        'supporting_episode_ids': [],
        'contradicting_evidence': [],
        'alternatives': alternatives,
        'observed_roles': observed_roles,
        'reason': reason,
        'evidence_scope': 'evidence_only' if features.get('evidence_only') else 'whole_journey',
    }
