"""
Wizard B — Hindsight (Tier 2B, 2026-09-02).

Backward-looking lens over a tenant's journeys (JourneyData v3, written by
Wizard A v2). Its job is to PROVE, on the tenant's own history:

  1. arc pattern profiles     — what each arc looks like here (health
                                start/end/lowest, months, phase mix,
                                evidence depth), keyed by the journey's
                                arc — ONE vocabulary
  2. phase transition matrix  — how often each phase leads to the next,
                                how long it takes, and what TRIGGERED it
                                (role histogram from the journey's
                                transition-trigger episodes)
  3. realized NRR per arc     — (ARR − lost + expansion) / ARR from
                                OUTCOME nodes in the taxonomy's lost /
                                expansion buckets only; new_logo excluded
                                from the denominator (old repo's rule,
                                kept)
  4. interventions            — before/after windows from the journeys'
                                counterfactual hooks, and actual ending
                                health vs the arc's expected path. A
                                comparison, labelled as such — not a
                                causal estimate
  5. lead-time backtest       — evals.lead_time_backtest, embedded, with
                                its evidence label
  6. early-warning rules      — DERIVED: which behavioral roles preceded
                                which events on this tenant, with
                                observed frequency and median lead. The
                                old repo's fixed EW001/EW002 filtered on
                                a vocabulary the journeys never produced
                                and never fired for any customer.

Not here: the forward portfolio NRR forecast (Foresight — Wizard D, 2D).
"""
from __future__ import annotations

import logging
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MIN_JOURNEYS = 5
INTERVENTION_LIFT_PTS = 5.0


def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _median(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


# ═══════════════════════════════════════════════════════════════════════
# 1. Pattern profiles
# ═══════════════════════════════════════════════════════════════════════

def pattern_profiles(journeys: List[dict]) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for j in journeys:
        groups[j['arc'].get('arc_type') or j['state']].append(j)
    out = {}
    for key, js in groups.items():
        phase_months: Counter = Counter()
        for j in js:
            for p in j['phases']:
                phase_months[p['name']] += p['months']
        total = sum(phase_months.values()) or 1
        out[key] = {
            'n_accounts': len(js),
            'kind': 'arc' if js[0]['arc'].get('arc_type') else 'state',
            'avg_starting_health': _mean([j['summary']['starting_health'] for j in js]),
            'avg_ending_health': _mean([j['summary']['ending_health'] for j in js]),
            'avg_lowest_health': _mean([j['summary']['lowest_health'] for j in js]),
            'avg_highest_health': _mean([j['summary']['highest_health'] for j in js]),
            'avg_health_change': _mean([
                j['summary']['ending_health'] - j['summary']['starting_health'] for j in js
                if j['summary']['ending_health'] is not None and j['summary']['starting_health'] is not None]),
            'avg_months': _mean([j['summary']['months_scored'] for j in js]),
            'phase_distribution_pct': {p: round(100.0 * m / total, 1) for p, m in phase_months.items()},
            'current_phase_mix': dict(Counter(j['current_phase'] for j in js)),
            'avg_supporting_episodes': _mean([len(j['arc'].get('supporting_episode_ids', [])) for j in js]),
            'observed_roles': dict(Counter(r for j in js for r in j['arc'].get('observed_roles', []))),
            'lead_days': {
                'n': sum(1 for j in js if j['leading_vs_trailing'].get('lead_days') is not None),
                'median': _median([j['leading_vs_trailing'].get('lead_days') for j in js]),
            },
            'accounts': [j['account_name'] for j in js],
        }
    return out


# ═══════════════════════════════════════════════════════════════════════
# 2. Transitions
# ═══════════════════════════════════════════════════════════════════════

def transition_matrix(journeys: List[dict]) -> Dict[str, dict]:
    counts: Dict[str, dict] = defaultdict(lambda: {'count': 0, 'months_in_from': [], 'triggers': Counter()})
    from_segments: Counter = Counter()
    for j in journeys:
        eps = {e['episode_id']: e for e in j['episodes']}
        phases = j['phases']
        for i, p in enumerate(phases):
            from_segments[p['name']] += 1
            if i + 1 < len(phases):
                nxt = phases[i + 1]
                key = f"{p['name']}→{nxt['name']}"
                counts[key]['count'] += 1
                counts[key]['months_in_from'].append(p['months'])
                trig = eps.get(nxt.get('trigger_episode_id') or '')
                if trig:
                    counts[key]['triggers'][trig.get('role') or trig['kind']] += 1
    out = {}
    for key, d in counts.items():
        frm = key.split('→')[0]
        out[key] = {
            'from_phase': frm, 'to_phase': key.split('→')[1],
            'count': d['count'],
            'probability': round(d['count'] / from_segments[frm], 3) if from_segments[frm] else None,
            'avg_months_before_transition': _mean(d['months_in_from']),
            'triggers': dict(d['triggers'].most_common()),
        }
    return out


# ═══════════════════════════════════════════════════════════════════════
# 3. Realized NRR
# ═══════════════════════════════════════════════════════════════════════

def realized_nrr(customer_id: int, journeys: List[dict], vertical: str) -> dict:
    from models import Account
    from utils.taxonomy_loader import get_taxonomy
    tax = get_taxonomy(vertical)
    arr = {a.account_id: float(a.revenue or 0) for a in Account.query.filter_by(customer_id=customer_id).all()}

    per_account: Dict[int, dict] = defaultdict(lambda: {'lost': 0.0, 'expansion': 0.0, 'protected': 0.0, 'at_risk': 0.0, 'new_logo': False})
    for j in journeys:
        aid = j['account_id']
        for e in j['episodes']:
            if e['kind'] != 'outcome' or e.get('revenue') is None:
                continue
            amt = abs(float(e['revenue']))
            if e.get('subtype') == 'new_logo':
                per_account[aid]['expansion'] += amt
                per_account[aid]['new_logo'] = True
                continue
            bucket = e.get('revenue_bucket')
            if bucket in ('lost', 'expansion', 'protected', 'at_risk'):
                per_account[aid][bucket] += amt

    def _group(js: List[dict]) -> dict:
        total_arr = sum(arr.get(j['account_id'], 0) for j in js if not per_account[j['account_id']]['new_logo'])
        lost = sum(per_account[j['account_id']]['lost'] for j in js)
        exp = sum(per_account[j['account_id']]['expansion'] for j in js)
        prot = sum(per_account[j['account_id']]['protected'] for j in js)
        successes = sum(
            1 for j in js
            if j['summary']['ending_health'] is not None and j['summary']['lowest_health'] is not None
            and j['summary']['ending_health'] > j['summary']['lowest_health'] + INTERVENTION_LIFT_PTS)
        return {
            'n_accounts': len(js),
            'starting_arr': round(total_arr, 2),
            'lost': round(lost, 2), 'expansion': round(exp, 2), 'protected_narrative': round(prot, 2),
            'net_arr': round(total_arr - lost + exp, 2),
            'nrr': round((total_arr - lost + exp) / total_arr, 4) if total_arr > 0 else None,
            'recovered_from_low_share': round(successes / len(js), 3) if js else None,
        }

    by_arc: Dict[str, List[dict]] = defaultdict(list)
    for j in journeys:
        by_arc[j['arc'].get('arc_type') or j['state']].append(j)
    return {
        'basis': 'Account.revenue as starting ARR; only OUTCOME nodes in the taxonomy lost/expansion buckets move NRR; '
                 'protected/at_risk/pipeline are narrative and reported separately',
        'portfolio': _group(journeys),
        'by_arc': {k: _group(v) for k, v in by_arc.items()},
    }


# ═══════════════════════════════════════════════════════════════════════
# 4. Interventions (counterfactual hooks + expected path)
# ═══════════════════════════════════════════════════════════════════════

def interventions(journeys: List[dict]) -> dict:
    rows = []
    for j in journeys:
        exp = j.get('expected_path') or {}
        expected_end = exp['phases'][-1].get('health_end') if exp.get('phases') else None
        for h in j.get('counterfactual_hooks', []):
            before, after = h['health_before'], h['health_after']
            lift = (after['last'] - before['last']) if (after.get('last') is not None and before.get('last') is not None) else None
            rows.append({
                'account': j['account_name'], 'arc': j['arc'].get('arc_type'), 'date': h['date'], 'title': h['title'],
                'health_before_last': before.get('last'), 'health_after_last': after.get('last'), 'lift_pts': round(lift, 2) if lift is not None else None,
                'outcomes_after': [o['bucket'] for o in h['outcomes_after']],
                'revenue_after_protected': round(sum(o['revenue'] or 0 for o in h['outcomes_after'] if o['bucket'] in ('protected', 'expansion')), 2),
                'actual_ending_health': j['summary']['ending_health'],
                'expected_path_end_health': expected_end,
            })
    lifts = [r['lift_pts'] for r in rows if r['lift_pts'] is not None]
    return {
        'basis': 'before/after windows around observed decisions and CSM interventions, and actual ending health vs the '
                 'arc template\'s expected path — a comparison on this tenant\'s data, not a causal estimate',
        'n': len(rows),
        'with_health_lift_share': round(sum(1 for l in lifts if l >= INTERVENTION_LIFT_PTS) / len(lifts), 3) if lifts else None,
        'median_lift_pts': _median(lifts),
        'followed_by_protected_or_expansion_share': round(
            sum(1 for r in rows if any(b in ('protected', 'expansion') for b in r['outcomes_after'])) / len(rows), 3) if rows else None,
        'rows': rows,
    }


# ═══════════════════════════════════════════════════════════════════════
# 6. Derived early-warning rules
# ═══════════════════════════════════════════════════════════════════════

def derived_rules(journeys: List[dict], backtest: dict) -> List[dict]:
    """Which behavioral roles were in the warning month that preceded each
    event, vs how often those roles appear in months that led nowhere."""
    by_name = {j['account_name']: j for j in journeys}
    rules = []
    for hyp_name, r in backtest['results'].items():
        preceded: Counter = Counter()
        leads: Dict[str, List[int]] = defaultdict(list)
        n_events = r['events']
        for ev in r['per_event']:
            if not ev.get('leading_warned_at'):
                continue
            j = by_name.get(ev['account'])
            if not j:
                continue
            month = ev['leading_warned_at'][:7] + '-01'
            s = next((s for s in j['leading_vs_trailing']['series'] if s['month'] == month), None)
            for role in (s or {}).get('roles', {}):
                preceded[role] += 1
                leads[role].append(ev['leading_lead_days'])
        # background rate: share of all account-months whose window carried the role
        role_months: Counter = Counter()
        total_months = 0
        for j in journeys:
            for s in j['leading_vs_trailing']['series']:
                total_months += 1
                for role in s.get('roles', {}):
                    role_months[role] += 1
        for role, k in preceded.most_common():
            rules.append({
                'hypothesis': hyp_name, 'role': role,
                'preceded_events': k, 'of_events': n_events,
                'frequency': round(k / n_events, 3) if n_events else None,
                'median_lead_days': _median(leads[role]),
                'background_rate_per_100_months': round(100.0 * role_months[role] / total_months, 2) if total_months else None,
                'rule_semantics': 'observed frequency on this tenant — not calibrated, not a probability',
            })
    return rules


# ═══════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════

def run_wizard_b(customer_id: int, *, horizon_days: int = 180, min_events: int = 10,
                 assert_real: bool = False, persist: bool = True) -> dict:
    from models import JourneyData, WizardRun
    from extensions import db
    from utils.vertical_registry import get_vertical_for_customer
    from evals.lead_time_backtest import run_backtest

    vertical = get_vertical_for_customer(customer_id)
    journeys = [r.journey_json for r in JourneyData.query.filter_by(customer_id=customer_id).all()]
    if len(journeys) < MIN_JOURNEYS:
        return {'status': 'skipped', 'reason': f'{len(journeys)} journeys < {MIN_JOURNEYS} — run Wizard A / add accounts',
                'journeys': len(journeys)}

    backtest = run_backtest(customer_id, horizon_days=horizon_days, min_events=min_events, assert_real=assert_real)
    results = {
        'status': 'completed',
        'wizard': 'b',
        'lens': 'hindsight',
        'vertical': vertical,
        'journeys': len(journeys),
        'coverage': dict(Counter(j['state'] for j in journeys)),
        'evidence_label': backtest['evidence_label'],
        'pattern_profiles': pattern_profiles(journeys),
        'transitions': transition_matrix(journeys),
        'realized_nrr': realized_nrr(customer_id, journeys, vertical),
        'interventions': interventions(journeys),
        'backtest': {k: v for k, v in backtest.items() if k != 'results'} | {
            'results': {name: {kk: vv for kk, vv in r.items() if kk != 'per_event'} | {'per_event': r['per_event']}
                        for name, r in backtest['results'].items()}},
        'early_warning_rules': derived_rules(journeys, backtest),
        'generated_at': datetime.utcnow().isoformat(),
    }
    if persist:
        run = WizardRun(
            run_id=f"wizard_b_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
            customer_id=customer_id, wizard='b', status='completed',
            config={'horizon_days': horizon_days, 'min_events': min_events, 'assert_real': assert_real},
            results=results, completed_at=datetime.utcnow(), created_by='wizard_b_hindsight',
        )
        db.session.add(run)
        db.session.commit()
        results['run_id'] = run.run_id
    return results
