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
  3. realized retention      — net and gross, over a trailing window, for
                                the accounts that existed at its start;
                                starting revenue reconstructed from the
                                window's booked losses and expansion;
                                only OUTCOME nodes in the taxonomy's lost /
                                expansion buckets move it; withheld with a
                                reason when the evidence is thin. Settings:
                                config/wizard_b.json, with tenant overrides
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
from datetime import datetime, timedelta
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

def _as_date(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def _first_seen(j: dict) -> Optional[datetime]:
    """The earliest date this account's journey has evidence or a scored month for."""
    dates = [_as_date((j.get('data_coverage') or {}).get('first_evidence_at'))]
    dates += [_as_date(p.get('entered_at')) for p in (j.get('phases') or [])[:1]]
    dates += [_as_date(e.get('date')) for e in j.get('episodes') or []]
    dates = [d for d in dates if d is not None]
    return min(dates) if dates else None


def realized_nrr(customer_id: int, journeys: List[dict], vertical: str, settings: Optional[dict] = None) -> dict:
    """Realized net and gross revenue retention over a trailing window.

    Cohort: accounts that existed at the window start (an account whose new-account outcome
    falls inside the window is left out of both sides). Starting revenue is the cohort's
    revenue at the window start — reconstructed from today's revenue and the window's booked
    losses and expansion, or the revenue column as loaded, per settings. Only outcomes dated
    inside the window in the loss / expansion buckets move the figure. The figure is withheld,
    with the reason, when the cohort, the observed history or the share of outcomes carrying
    an amount is below the configured minimum. Settings: wizards/wizard_b_settings.retention.
    """
    from models import Account
    from wizards import wizard_b_settings
    cfg = settings or wizard_b_settings.retention(customer_id)
    current = {a.account_id: float(a.revenue or 0) for a in Account.query.filter_by(customer_id=customer_id).all()}
    return retention_figures(current, journeys, cfg)


def retention_figures(current: Dict[int, float], journeys: List[dict], cfg: dict) -> dict:
    """realized_nrr's arithmetic on plain values: today's revenue per account, the journeys, the settings."""
    loss_b, exp_b, narr_b = set(cfg['loss_buckets']), set(cfg['expansion_buckets']), set(cfg['narrative_buckets'])
    new_subtypes = set(cfg['new_account_subtypes'])

    as_ofs = [d for d in (_as_date(j.get('as_of')) for j in journeys) if d is not None]
    end = datetime.utcnow() if cfg['window_anchor'] == 'today' or not as_ofs else max(as_ofs)
    start = end - timedelta(days=cfg['window_months'] * cfg['days_per_month'])
    reconstructed = cfg['starting_revenue'] == 'reconstructed'

    acct: Dict[int, dict] = {}
    for j in journeys:
        aid = j['account_id']
        a = acct.setdefault(aid, {'lost': 0.0, 'expansion': 0.0, 'narrative': 0.0, 'new': False, 'with_amount': 0,
                                  'without_amount': 0, 'outside_window': 0, 'first_seen': _first_seen(j)})
        for e in j['episodes']:
            if e['kind'] != 'outcome':
                continue
            when = _as_date(e.get('date'))
            in_window = when is not None and start < when <= end
            is_new = e.get('subtype') in new_subtypes
            bucket = e.get('revenue_bucket')
            if is_new:
                # A new account joined inside the window: not part of the starting cohort.
                a['new'] = a['new'] or in_window
                continue
            if bucket not in loss_b and bucket not in exp_b and bucket not in narr_b:
                continue
            if not in_window:
                a['outside_window'] += 1 if (bucket in loss_b or bucket in exp_b) else 0
                continue
            if bucket in narr_b:
                a['narrative'] += abs(float(e['revenue'])) if e.get('revenue') is not None else 0.0
                continue
            if e.get('revenue') is None:
                a['without_amount'] += 1
                continue
            a['with_amount'] += 1
            a['lost' if bucket in loss_b else 'expansion'] += abs(float(e['revenue']))
        now = current.get(aid, 0.0)
        a['start'] = max(now - a['expansion'] + a['lost'], 0.0) if reconstructed else now
        # A loss cannot exceed what the account was paying at the window start.
        a['lost_counted'] = min(a['lost'], a['start'])

    def _group(js: List[dict]) -> dict:
        ids = [j['account_id'] for j in js]
        cohort = [i for i in ids if not acct[i]['new']]
        starting = sum(acct[i]['start'] for i in cohort)
        lost = sum(acct[i]['lost_counted'] for i in cohort)
        exp = sum(acct[i]['expansion'] for i in cohort)
        with_amt = sum(acct[i]['with_amount'] for i in cohort)
        without_amt = sum(acct[i]['without_amount'] for i in cohort)
        seen = [acct[i]['first_seen'] for i in cohort if acct[i]['first_seen'] is not None]
        observed_from = max(min(seen), start) if seen else None
        observed_months = round((end - observed_from).days / cfg['days_per_month'], 1) if observed_from else 0.0
        amount_coverage = round(with_amt / (with_amt + without_amt), 3) if (with_amt + without_amt) else 1.0
        reasons = []
        if len(cohort) < cfg['min_cohort_accounts']:
            reasons.append(f"{len(cohort)} account(s) in the cohort; at least {cfg['min_cohort_accounts']} needed")
        if observed_months < cfg['min_observed_months']:
            reasons.append(f"{observed_months} month(s) of history in the window; at least {cfg['min_observed_months']} needed")
        if amount_coverage < cfg['min_amount_coverage']:
            reasons.append(f"{without_amt} of {with_amt + without_amt} loss and expansion outcomes carry no amount")
        if starting <= 0:
            reasons.append('the cohort has no starting revenue')
        ok = not reasons
        successes = sum(
            1 for j in js
            if j['summary']['ending_health'] is not None and j['summary']['lowest_health'] is not None
            and j['summary']['ending_health'] > j['summary']['lowest_health'] + INTERVENTION_LIFT_PTS)
        return {
            'n_accounts': len(js), 'cohort_accounts': len(cohort), 'new_accounts_excluded': len(ids) - len(cohort),
            'starting_arr': round(starting, 2),
            'lost': round(lost, 2), 'expansion': round(exp, 2),
            'protected_narrative': round(sum(acct[i]['narrative'] for i in cohort), 2),
            'net_arr': round(starting - lost + exp, 2),
            'nrr': round((starting - lost + exp) / starting, 4) if ok else None,
            'grr': round((starting - lost) / starting, 4) if ok else None,
            'withheld': not ok, 'withheld_reason': '; '.join(reasons) or None,
            'observed_months': observed_months, 'amount_coverage': amount_coverage,
            'outcomes_without_amount': without_amt,
            'outcomes_outside_window': sum(acct[i]['outside_window'] for i in cohort),
            'recovered_from_low_share': round(successes / len(js), 3) if js else None,
        }

    by_arc: Dict[str, List[dict]] = defaultdict(list)
    for j in journeys:
        by_arc[j['arc'].get('arc_type') or j['state']].append(j)
    starting_basis = ("starting revenue reconstructed as today's revenue less expansion plus losses booked in the window"
                      if reconstructed else 'starting revenue is the revenue column as loaded')
    return {
        'basis': f"trailing {cfg['window_months']} months to {end.date().isoformat()}; cohort = accounts that existed at the "
                 f"window start; {starting_basis}; only OUTCOME nodes dated in the window in the taxonomy lost/expansion "
                 'buckets move the figure; protected/at_risk/pipeline are narrative and reported separately',
        'window': {'start': start.date().isoformat(), 'end': end.date().isoformat(), 'months': cfg['window_months'],
                   'anchor': cfg['window_anchor']},
        'settings': {k: cfg[k] for k in ('window_months', 'window_anchor', 'starting_revenue', 'min_cohort_accounts',
                                         'min_observed_months', 'min_amount_coverage')},
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


def get_hindsight(customer_id: int) -> dict:
    """The latest completed Wizard B run, read-shaped for get_hindsight/get_roi.
    {'status': 'no_run', 'hint': ...} when none exists — never a raise, never a guess.

    Canonical home for this lookup: roi.measured's own _hindsight() delegates here
    rather than keeping a second copy, since get_roi's 'hindsight' block and this
    function's caller (the get_hindsight MCP tool) must never be able to drift."""
    from models import WizardRun
    run = (WizardRun.query.filter_by(customer_id=int(customer_id), wizard='b', status='completed')
           .order_by(WizardRun.created_at.desc()).first())
    if run is None or not run.results:
        return {'status': 'no_run', 'hint': "trigger_wizard(customer_id, 'b') — needs at least five journeys"}
    res = run.results
    iv = res.get('interventions') or {}
    return {'status': 'ok', 'run_id': run.run_id, 'generated_at': res.get('generated_at'), 'evidence_label': res.get('evidence_label'),
            'interventions': {k: iv.get(k) for k in ('basis', 'n', 'with_health_lift_share', 'median_lift_pts', 'followed_by_protected_or_expansion_share')},
            'intervention_rows': [{k: r.get(k) for k in ('account', 'date', 'title', 'lift_pts', 'outcomes_after', 'revenue_after_protected')} for r in iv.get('rows') or []],
            'realized_nrr': (res.get('realized_nrr') or {}).get('portfolio'), 'realized_nrr_basis': (res.get('realized_nrr') or {}).get('basis'),
            'realized_nrr_window': (res.get('realized_nrr') or {}).get('window'),
            'journeys': res.get('journeys')}
