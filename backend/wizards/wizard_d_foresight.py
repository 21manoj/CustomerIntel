"""
Wizard D — Foresight (docs/design/wizard-d-foresight.md).

The forward lens over journey v3: per account, at the next decision point
(renewal / refresh / contract end, or the horizon end), how likely retention
and expansion are and what ARR is expected at the horizon end — every number
with an interval and a `basis` that says where it came from:

  prior        the template scorecard in config/wizard_d.json read against the
               journey (health band of kpi_only, the leading label, arc, phase,
               an intervention in flight); interval = a declared template range
  calibrated   the same prior updated on the tenant's own logged terminal
               outcomes (Beta-binomial on a point-in-time stratum); interval =
               a Beta credible interval. Unlocks only when the label counts in
               `calibration` are met — otherwise the block says `prior` and
               shows the counts.

Hindsight (wizard_b_hindsight) is the backward lens over the same journeys;
the two never blend kpi_only with qual (absolute-separation invariant): the
band and the leading label enter as separate factors.

Writes: ForecastRun + AccountForecast (immutable per-run history), and
journey_json['forecast'] on each JourneyData row with the narrative re-rendered
so the cited forecast sentence appears without a rebuild. build_journey embeds
the latest stored block on every later rebuild (latest_forecast_block).
Vertical-agnostic: no vertical name here; `verticals` overrides live in config.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Dict, List, Optional, Tuple

import utils.health_thresholds as ht
from wizards import wizard_d_settings as settings

logger = logging.getLogger(__name__)

# Bump on every change to the forecast block's shape or the math behind it.
#   d1.0  template prior + Beta-binomial calibration, portfolio propagation (2026-09-05)
GENERATOR_VERSION = 'd1.0'
LENS = 'foresight'
WIZARD = 'd'
SEPARATION_NOTE = 'kpi_only (health band) and qual (leading label) are read side by side as separate factors; never blended.'
INTERVENTION_OPEN_STATES = (None, '')   # closed_state is unset until the workflow reports done / failed / cancelled (playbooks.governance)


# ═══════════════════════════════════════════════════════════════════════
# Math — regularized incomplete beta and its inverse (no scipy in this build)
# ═══════════════════════════════════════════════════════════════════════

def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz), standard form."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / (c if abs(c) > tiny else tiny)
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def beta_cdf(a: float, b: float, x: float) -> float:
    """I_x(a, b), the regularized incomplete beta function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_quantile(a: float, b: float, q: float) -> float:
    """Inverse of beta_cdf by bisection — monotone, so exact to the tolerance."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if beta_cdf(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def z_for_level(level: float) -> float:
    """Two-sided normal multiplier for a central interval at `level` (0.9 → 1.645)."""
    return NormalDist().inv_cdf((1.0 + level) / 2.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _d(iso) -> Optional[datetime]:
    if not iso:
        return None
    if isinstance(iso, datetime):
        return iso
    return datetime.fromisoformat(str(iso)[:19])


# ═══════════════════════════════════════════════════════════════════════
# Inputs read from the journey
# ═══════════════════════════════════════════════════════════════════════

def health_band(journey: dict) -> str:
    """The trailing layer's band, from kpi_only-based health; 'none' without a KPI layer."""
    return (journey.get('features') or {}).get('health_band') or 'none'


def leading_label(journey: dict) -> str:
    """The leading layer's latest label as a config key. A live month (`leading_only`)
    is split by the 90-day net polarity, since it has no divergence to read."""
    f = journey.get('features') or {}
    label = f.get('early_warning_now')
    if label == 'leading_only':
        net = f.get('net_polarity_90d') or 0
        return 'leading_only_negative' if net < 0 else 'leading_only_positive' if net > 0 else 'leading_only_neutral'
    return label or 'none'


def arc_key(journey: dict) -> str:
    arc = journey.get('arc') or {}
    return arc.get('arc_type') or arc.get('state') or 'unclassified'


def phase_key(journey: dict) -> str:
    return journey.get('current_phase') or 'none'


def interventions_in_flight(journey: dict) -> List[dict]:
    """Intervention episodes inside the current phase that the workflow has not closed."""
    phases = journey.get('phases') or []
    since = _d(phases[-1]['entered_at']) if phases else None
    out = []
    for e in journey.get('episodes') or []:
        if e.get('kind') != 'intervention':
            continue
        closed = (e.get('meta') or {}).get('closed_state')
        if closed not in INTERVENTION_OPEN_STATES:
            continue
        if since is None or (_d(e.get('date')) or since) >= since:
            out.append(e)
    return out


def expansion_intent_present(journey: dict) -> bool:
    counts = (journey.get('features') or {}).get('signal_role_counts_90d') or {}
    return bool(counts.get('expansion_intent') or counts.get('expansion_realized'))


def decision_point(account, journey: dict, as_of: datetime, horizon_days: int, window_days: int) -> dict:
    """The next commercial decision inside the horizon, read from the account
    profile (renewal_date / contract_end for subscriptions, refresh_date for
    installed base). Nothing is invented: unknown stays unknown, a passed date
    with no logged terminal outcome is `passed_unrecorded` (the decision is
    still open on the record), a passed date with one is `resolved`."""
    pm = getattr(account, 'profile_metadata', None) or {}
    raw, kind = None, None
    for key, k in (('renewal_date', 'renewal'), ('contract_end', 'contract_end'), ('refresh_date', 'refresh')):
        if pm.get(key):
            raw, kind = pm[key], k
            break
    if not raw:
        return {'at': None, 'kind': None, 'status': 'unknown', 'days_to': None, 'inside_horizon': False}
    try:
        dp = datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return {'at': None, 'kind': kind, 'status': 'unparseable', 'days_to': None, 'inside_horizon': False}
    days_to = (dp - as_of.date()).days
    horizon_end = as_of.date() + timedelta(days=horizon_days)
    if dp < as_of.date():
        pos, neg = settings.get('calibration', 'terminal_positive_buckets'), settings.get('calibration', 'terminal_negative_buckets')
        near = [e for e in (journey.get('episodes') or []) if e.get('kind') == 'outcome'
                and e.get('revenue_bucket') in set(pos) | set(neg)
                and abs(((_d(e.get('date')) or as_of).date() - dp).days) <= window_days]
        if near:
            return {'at': dp.isoformat(), 'kind': kind, 'status': 'resolved', 'days_to': days_to, 'inside_horizon': False,
                    'resolved_by': [e['episode_id'] for e in near]}
        return {'at': dp.isoformat(), 'kind': kind, 'status': 'passed_unrecorded', 'days_to': 0, 'inside_horizon': True}
    if dp <= horizon_end:
        return {'at': dp.isoformat(), 'kind': kind, 'status': 'inside_horizon', 'days_to': days_to, 'inside_horizon': True}
    return {'at': dp.isoformat(), 'kind': kind, 'status': 'beyond_horizon', 'days_to': days_to, 'inside_horizon': False}


def template_block(journey: dict, as_of: datetime) -> Optional[dict]:
    """Where the account sits on its arc's expected path — context, never a probability.
    Loss severity (arr_at_risk_peak / arr_start) is the one number read from it."""
    from utils.story_arc_loader import load_arc, expected_path
    arc = journey.get('arc') or {}
    if not arc.get('arc_type'):
        return None
    manifest = load_arc(arc['arc_type'])
    path = expected_path(arc['arc_type'])
    if not manifest or not path:
        return None
    rn = manifest.get('revenue_narrative') or {}
    at_risk_share = None
    if rn.get('arr_start'):
        at_risk_share = round(_clamp(float(rn.get('arr_at_risk_peak') or 0) / float(rn['arr_start']), 0.0, 1.0), 3)
    support = [e for e in (journey.get('episodes') or []) if e.get('episode_id') in set(arc.get('supporting_episode_ids') or [])]
    start = min((_d(e['date']) for e in support), default=None)
    weeks = round((as_of - start).days / 7.0, 1) if start else None
    total = path.get('total_weeks') or 0
    position = round(_clamp(weeks / total, 0.0, 1.0), 3) if weeks is not None and total else None
    current = None
    if position is not None:
        elapsed = position * total
        for p in path['phases']:
            if p['starts_week'] <= elapsed < p['starts_week'] + p['duration_weeks'] or p is path['phases'][-1]:
                current = {'phase_id': p['phase_id'], 'name': p['name'], 'health_end': p['health_end']}
                break
    last = path['phases'][-1] if path.get('phases') else {}
    return {
        'arc_type': path['arc_type'], 'arc_name': path.get('arc_name'), 'source': 'story_arc_template',
        'note': path.get('note'), 'total_weeks': total, 'weeks_elapsed': weeks, 'position': position,
        'template_phase_now': current, 'expected_health_end': last.get('health_end'), 'at_risk_share_of_arr': at_risk_share,
    }


def stratum_key(band: Optional[str], warning: bool) -> str:
    return f"{band or 'none'}|{'warning' if warning else 'no_warning'}"


def _series_stratum(entry: Optional[dict]) -> str:
    """The point-in-time stratum of one leading-vs-trailing series month: the band of
    kpi_only and the same leading-warning rule the lead-time backtest uses."""
    if not entry:
        return 'unknown'
    k = entry.get('kpi_only')
    band = ht.classify(k) if k is not None else 'none'
    q = entry.get('qual')
    warning = entry.get('early_warning') == 'early_warning' or (q is not None and q < ht.at_risk_min())
    return stratum_key(band, warning)


def current_stratum(journey: dict) -> str:
    series = (journey.get('leading_vs_trailing') or {}).get('series') or []
    return _series_stratum(series[-1] if series else None)


# ═══════════════════════════════════════════════════════════════════════
# Labels — the tenant's own terminal outcomes, grouped into decisions
# ═══════════════════════════════════════════════════════════════════════

def collect_labels(journeys: List[dict]) -> List[dict]:
    """One label per decision: terminal outcome episodes on an account within
    label_window_days of each other. Any `lost` bucket → retained 0, else 1;
    expanded 1 when an `expansion` bucket is in the group. The stratum is the
    series month BEFORE the decision — what was knowable then, not after."""
    pos = set(settings.get('calibration', 'terminal_positive_buckets'))
    neg = set(settings.get('calibration', 'terminal_negative_buckets'))
    window = settings.get('calibration', 'label_window_days')
    labels = []
    for j in journeys:
        series = (j.get('leading_vs_trailing') or {}).get('series') or []
        outs = sorted((e for e in (j.get('episodes') or []) if e.get('kind') == 'outcome' and e.get('revenue_bucket') in pos | neg),
                      key=lambda e: e['date'])
        groups: List[List[dict]] = []
        for e in outs:
            if groups and (_d(e['date']) - _d(groups[-1][0]['date'])).days <= window:
                groups[-1].append(e)
            else:
                groups.append([e])
        for g in groups:
            decided = _d(g[0]['date'])
            month = date(decided.year, decided.month, 1)
            prior_months = [s for s in series if date.fromisoformat(s['month']) < month]
            labels.append({
                'account_id': j.get('account_id'), 'account_name': j.get('account_name'), 'decided_at': decided.date().isoformat(),
                'retained': 0 if any(e.get('revenue_bucket') in neg for e in g) else 1,
                'expanded': 1 if any(e.get('revenue_bucket') == 'expansion' for e in g) else 0,
                'stratum': _series_stratum(prior_months[-1] if prior_months else None),
                'episode_ids': [e['episode_id'] for e in g],
            })
    return labels


def calibration_gate(labels: List[dict]) -> dict:
    """Says whether `calibrated` is earned, with every count the reader needs to check it."""
    need, per_class, per_stratum = (settings.get('calibration', 'min_labels'), settings.get('calibration', 'min_per_class'),
                                    settings.get('calibration', 'min_per_stratum'))
    n = len(labels)
    positive = sum(l['retained'] for l in labels)
    negative = n - positive
    by_stratum: Dict[str, dict] = {}
    for l in labels:
        s = by_stratum.setdefault(l['stratum'], {'n': 0, 'retained': 0, 'not_retained': 0, 'expanded': 0})
        s['n'] += 1
        s['retained'] += l['retained']
        s['not_retained'] += 1 - l['retained']
        s['expanded'] += l['expanded']
    if n < need:
        reason = f'{n} labelled decision(s) < {need} needed'
    elif positive < per_class or negative < per_class:
        reason = f'{positive} retained / {negative} not retained; {per_class} of each class needed'
    else:
        reason = f'{n} labelled decisions, {positive} retained / {negative} not retained'
    return {'eligible': n >= need and positive >= per_class and negative >= per_class,
            'n': n, 'positive': positive, 'negative': negative, 'expanded': sum(l['expanded'] for l in labels),
            'needed': need, 'per_class_needed': per_class, 'per_stratum_needed': per_stratum,
            'reason': reason, 'by_stratum': by_stratum, 'label_semantics': 'terminal outcomes (taxonomy buckets) grouped per account within '
            f"{settings.get('calibration', 'label_window_days')} days; stratum = series month before the decision"}


def beta_update(p_prior: float, strength: float, successes: int, failures: int, level: float) -> Tuple[float, float, float]:
    a = strength * p_prior + successes
    b = strength * (1.0 - p_prior) + failures
    tail = (1.0 - level) / 2.0
    return a / (a + b), beta_quantile(a, b, tail), beta_quantile(a, b, 1.0 - tail)


# ═══════════════════════════════════════════════════════════════════════
# One account
# ═══════════════════════════════════════════════════════════════════════

def _prior_retention(journey: dict, vertical: str, dp: dict, iv: List[dict], drivers: List[dict]) -> Tuple[float, str]:
    vg = lambda *k: settings.vertical_get(vertical, 'prior', *k)  # noqa: E731
    factors = [
        ('health_band', health_band(journey), vg('health_band_factor')),
        ('leading_label', leading_label(journey), vg('leading_label_factor')),
        ('arc', arc_key(journey), vg('arc_factor')),
        ('phase', phase_key(journey), vg('phase_factor')),
    ]
    product = 1.0
    for name, key, table in factors:
        # a key the config does not know (a new arc, a new label) is a neutral factor, said out loud — never a silent default
        value = table.get(key, 1.0)
        drivers.append({'factor': name, 'key': key, 'value': value} | ({} if key in table else {'note': 'no factor configured for this key'}))
        product *= value
    if iv:
        value = vg('intervention_in_flight_factor')
        drivers.append({'factor': 'intervention_in_flight', 'key': ','.join(e['episode_id'] for e in iv), 'value': value,
                        'note': 'template lift; Hindsight reports the measured one'})
        product *= value
    floor, ceiling = settings.get('p_floor'), settings.get('p_ceiling')
    if dp['inside_horizon']:
        return _clamp(vg('base_retention_at_decision') * product, floor, ceiling), 'decision_in_horizon'
    # no decision inside the horizon: the question is mid-term contraction — the configured hazard, raised by the
    # same factors when they fall below 1 and lowered when above (hazard ÷ factor product)
    hazard = vg('midterm_loss_hazard') / product if product > 0 else 1.0
    return _clamp(1.0 - hazard, floor, ceiling), 'midterm'


def _prior_expansion(journey: dict, vertical: str, drivers: List[dict]) -> float:
    vg = lambda *k: settings.vertical_get(vertical, 'expansion', *k)  # noqa: E731
    arc_f = vg('arc_factor').get(arc_key(journey), 1.0)
    band_f = vg('health_band_factor').get(health_band(journey), 1.0)
    intent = expansion_intent_present(journey)
    intent_f = vg('expansion_intent_role_factor') if intent else 1.0
    drivers.append({'factor': 'expansion_arc', 'key': arc_key(journey), 'value': arc_f})
    drivers.append({'factor': 'expansion_health_band', 'key': health_band(journey), 'value': band_f})
    if intent:
        drivers.append({'factor': 'expansion_intent_present', 'key': 'expansion_intent|expansion_realized in 90d', 'value': intent_f})
    return _clamp(vg('base_p_expand') * arc_f * band_f * intent_f, settings.get('p_floor'), settings.get('p_ceiling'))


def _prior_half_width(journey: dict) -> Tuple[float, List[str]]:
    hw = settings.get('interval', 'half_width_p')
    why = []
    n_eps = len(journey.get('episodes') or [])
    if n_eps < settings.get('interval', 'thin_evidence_below_episodes'):
        hw += settings.get('interval', 'thin_evidence_extra')
        why.append(f'thin evidence ({n_eps} episodes)')
    if (journey.get('data_coverage') or {}).get('kpi_layer') in ('none', 'not_yet'):
        hw += settings.get('interval', 'no_kpi_layer_extra')
        why.append('no KPI layer')
    return hw, why


def _dollars(arr: float, p_ret: float, p_exp: float, size: float, severity: float) -> float:
    return arr * (p_ret * (1.0 + p_exp * size) + (1.0 - p_ret) * (1.0 - severity))


def _cites(journey: dict, iv: List[dict]) -> List[str]:
    ids = list((journey.get('arc') or {}).get('supporting_episode_ids') or [])
    series = (journey.get('leading_vs_trailing') or {}).get('series') or []
    if series:
        ids.extend(series[-1].get('contributing_episode_ids') or [])
    transitions = [e['episode_id'] for e in (journey.get('episodes') or []) if e.get('kind') == 'health_transition']
    if transitions:
        ids.append(transitions[-1])
    ids.extend(e['episode_id'] for e in iv)
    ids.extend(e['episode_id'] for e in (journey.get('episodes') or []) if e.get('kind') == 'renewal')
    known = {e['episode_id'] for e in (journey.get('episodes') or [])}
    return [i for i in dict.fromkeys(ids) if i in known]


def forecast_account(journey: dict, account, vertical: str, gate: dict, run_id: str, horizon_days: int) -> dict:
    as_of = _d(journey.get('as_of')) or datetime.utcnow()
    level = settings.get('interval_level')
    window = settings.get('calibration', 'label_window_days')
    dp = decision_point(account, journey, as_of, horizon_days, window)
    iv = interventions_in_flight(journey)
    drivers: List[dict] = []
    p_ret_prior, mode = _prior_retention(journey, vertical, dp, iv, drivers)
    p_exp_prior = _prior_expansion(journey, vertical, drivers)
    hw, widen_why = _prior_half_width(journey)

    stratum = current_stratum(journey)
    labels_block = {'n': gate['n'], 'positive': gate['positive'], 'negative': gate['negative'], 'needed': gate['needed'],
                    'per_class_needed': gate['per_class_needed'], 'stratum': stratum, 'stratum_n': (gate['by_stratum'].get(stratum) or {}).get('n', 0),
                    'stratum_used': None, 'gate': gate['reason']}
    if gate['eligible']:
        basis = 'calibrated'
        s = gate['by_stratum'].get(stratum) or {}
        if s.get('n', 0) >= gate['per_stratum_needed']:
            counts, labels_block['stratum_used'] = s, 'own'
        else:
            counts = {'n': gate['n'], 'retained': gate['positive'], 'not_retained': gate['negative'], 'expanded': gate['expanded']}
            labels_block['stratum_used'] = 'pooled'
        strength = settings.get('calibration', 'prior_strength')
        p_ret, ret_lo, ret_hi = beta_update(p_ret_prior, strength, counts['retained'], counts['not_retained'], level)
        p_exp, exp_lo, exp_hi = beta_update(p_exp_prior, strength, counts['expanded'], counts['n'] - counts['expanded'], level)
        semantics = 'beta_credible'
        basis_note = (f"prior updated on {counts['n']} labelled decision(s) ({labels_block['stratum_used']} stratum {stratum}); "
                      f"prior_strength {strength} pseudo-labels")
    else:
        basis = 'prior'
        p_ret, ret_lo, ret_hi = p_ret_prior, _clamp(p_ret_prior - hw, 0.0, 1.0), _clamp(p_ret_prior + hw, 0.0, 1.0)
        p_exp, exp_lo, exp_hi = p_exp_prior, _clamp(p_exp_prior - hw, 0.0, 1.0), _clamp(p_exp_prior + hw, 0.0, 1.0)
        semantics = 'template_range'
        basis_note = f"template prior — not calibrated: {gate['reason']}" + (f"; range widened for {', '.join(widen_why)}" if widen_why else '')

    tmpl = template_block(journey, as_of)
    if tmpl and tmpl.get('at_risk_share_of_arr') is not None:
        severity, severity_basis = tmpl['at_risk_share_of_arr'], 'story_arc_template'
    else:
        severity, severity_basis = settings.vertical_get(vertical, 'loss', 'default_severity'), 'config_default'
    size = settings.vertical_get(vertical, 'expansion', 'size_share_of_arr')
    arr = float(getattr(account, 'revenue', 0) or 0)
    expected = _dollars(arr, p_ret, p_exp, size, severity)
    low, high = _dollars(arr, ret_lo, exp_lo, size, severity), _dollars(arr, ret_hi, exp_hi, size, severity)

    return {
        'generator': f'{LENS}_{GENERATOR_VERSION}', 'lens': LENS, 'run_id': run_id, 'status': 'forecast',
        'as_of': as_of.isoformat(), 'horizon_days': horizon_days, 'horizon_end': (as_of + timedelta(days=horizon_days)).date().isoformat(),
        'basis': basis, 'basis_note': basis_note, 'labels': labels_block,
        'decision_point': dp,
        'retention': {'p': round(p_ret, 4), 'low': round(ret_lo, 4), 'high': round(ret_hi, 4), 'prior_p': round(p_ret_prior, 4),
                      'interval_level': level, 'interval_semantics': semantics, 'mode': mode},
        'expansion': {'p': round(p_exp, 4), 'low': round(exp_lo, 4), 'high': round(exp_hi, 4), 'prior_p': round(p_exp_prior, 4),
                      'interval_level': level, 'interval_semantics': semantics, 'size_share_of_arr': size},
        'revenue': {'arr': round(arr, 2), 'arr_known': arr > 0, 'expected_arr_end': round(expected, 2), 'low': round(low, 2), 'high': round(high, 2),
                    'nrr_contribution': round(expected - arr, 2), 'nrr_contribution_low': round(low - arr, 2), 'nrr_contribution_high': round(high - arr, 2),
                    'loss_severity': severity, 'loss_severity_basis': severity_basis,
                    'formula': 'arr × [p_retain·(1 + p_expand·size_share) + (1 − p_retain)·(1 − loss_severity)]; bounds = the formula at the interval ends'},
        'drivers': drivers, 'template': tmpl,
        'inputs': {'health_band': health_band(journey), 'leading_label': leading_label(journey), 'arc': arc_key(journey), 'phase': phase_key(journey),
                   'interventions_in_flight': [e['episode_id'] for e in iv], 'expansion_intent_present': expansion_intent_present(journey),
                   'episodes': len(journey.get('episodes') or []), 'kpi_layer': (journey.get('data_coverage') or {}).get('kpi_layer'),
                   'stratum': stratum},
        'cites': _cites(journey, iv), 'stale': False, 'separation': SEPARATION_NOTE,
    }


# ═══════════════════════════════════════════════════════════════════════
# Portfolio — revenue-weighted, interval propagated
# ═══════════════════════════════════════════════════════════════════════

def portfolio_rollup(blocks: List[dict]) -> dict:
    level = settings.get('interval_level')
    z = z_for_level(level)
    arr = sum(b['revenue']['arr'] for b in blocks)
    expected = sum(b['revenue']['expected_arr_end'] for b in blocks)
    sigmas = [(b['revenue']['high'] - b['revenue']['low']) / (2.0 * z) for b in blocks]
    sigma_ind = math.sqrt(sum(s * s for s in sigmas))
    independent = {'low': round(expected - z * sigma_ind, 2), 'high': round(expected + z * sigma_ind, 2),
                   'assumption': 'independent accounts: σ = half-width / z per account, sqrt(Σσ²) propagated'}
    correlated = {'low': round(sum(b['revenue']['low'] for b in blocks), 2), 'high': round(sum(b['revenue']['high'] for b in blocks), 2),
                  'assumption': 'perfectly correlated accounts: the bounds summed (worst case)'}
    headline = settings.get('portfolio', 'headline_assumption')
    ranges = {'independent': independent, 'correlated': correlated}
    head = ranges[headline]
    basis_counts: Dict[str, int] = {}
    for b in blocks:
        basis_counts[b['basis']] = basis_counts.get(b['basis'], 0) + 1
    nrr = (lambda v: round(v / arr, 4) if arr else None)  # noqa: E731
    return {
        'accounts': len(blocks), 'arr': round(arr, 2), 'expected_arr_end': round(expected, 2),
        'low': head['low'], 'high': head['high'], 'headline_assumption': headline, 'ranges': ranges, 'interval_level': level,
        'nrr': nrr(expected), 'nrr_low': nrr(head['low']), 'nrr_high': nrr(head['high']),
        'expected_retained_arr': round(sum(b['revenue']['arr'] * b['retention']['p'] for b in blocks), 2),
        'basis_counts': basis_counts,
        'basis': 'calibrated' if basis_counts.get('calibrated') == len(blocks) and blocks else 'prior' if basis_counts.get('prior') == len(blocks) else 'mixed',
        'note': 'a revenue-weighted roll-up of per-account blocks; the range is propagated from their intervals, never a sum of point estimates. '
                'Prior-basis half-widths are declared template ranges read as ±z·σ.',
    }


# ═══════════════════════════════════════════════════════════════════════
# Runner, embed, read
# ═══════════════════════════════════════════════════════════════════════

def run_wizard_d(customer_id: int, persist: bool = True, created_by: str = 'wizard_d_foresight') -> dict:
    from extensions import db
    from models import Account, JourneyData, ForecastRun, AccountForecast
    from utils.vertical_registry import get_vertical_for_customer

    vertical = get_vertical_for_customer(customer_id)
    rows = (JourneyData.query.filter_by(customer_id=int(customer_id))
            .join(Account, Account.account_id == JourneyData.account_id).add_entity(Account)
            .order_by(Account.account_id).all())
    if not rows:
        return {'status': 'skipped', 'wizard': WIZARD, 'lens': LENS, 'reason': 'no journeys — run process_data or trigger_wizard(customer_id, "a")',
                'accounts': 0}
    horizon = settings.get('horizon_days')
    run_id = f"wizard_d_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    journeys = [jd.journey_json for jd, _ in rows]
    gate = calibration_gate(collect_labels(journeys))
    blocks = []
    for jd, acct in rows:
        blocks.append((jd, acct, forecast_account(jd.journey_json, acct, vertical, gate, run_id, horizon)))
    portfolio = portfolio_rollup([b for _, _, b in blocks])
    as_of = max(_d(b['as_of']) for _, _, b in blocks)
    results = {
        'status': 'completed', 'wizard': WIZARD, 'lens': LENS, 'run_id': run_id, 'generator_version': GENERATOR_VERSION,
        'vertical': vertical, 'horizon_days': horizon, 'as_of': as_of.isoformat(), 'accounts': len(blocks),
        'basis_counts': portfolio['basis_counts'], 'labels': gate,
        'portfolio': portfolio,
        'account_forecasts': [{'account_id': acct.account_id, 'account_name': acct.account_name, 'basis': b['basis'],
                               'p_retain': b['retention']['p'], 'low': b['retention']['low'], 'high': b['retention']['high'],
                               'p_expand': b['expansion']['p'], 'expected_arr_end': b['revenue']['expected_arr_end'],
                               'decision_point': b['decision_point']['at'], 'decision_status': b['decision_point']['status']}
                              for _, acct, b in blocks],
        'generated_at': datetime.utcnow().isoformat(),
    }
    if not persist:
        return results

    snapshot = {k: v for k, v in settings.load().items() if not k.startswith('_')}
    db.session.add(ForecastRun(
        run_id=run_id, customer_id=int(customer_id), vertical=vertical, generator_version=GENERATOR_VERSION, horizon_days=horizon,
        as_of=as_of, basis_counts=portfolio['basis_counts'], labels=results['labels'], portfolio=portfolio,
        config_snapshot=snapshot, accounts=len(blocks), created_by=created_by,
    ))
    db.session.flush()          # the run row first: account_forecasts.run_id is a plain FK, not an ORM relationship
    for jd, acct, b in blocks:
        db.session.add(AccountForecast(
            run_id=run_id, customer_id=int(customer_id), account_id=acct.account_id, as_of=_d(b['as_of']), basis=b['basis'],
            p_retain=b['retention']['p'], p_retain_low=b['retention']['low'], p_retain_high=b['retention']['high'],
            p_expand=b['expansion']['p'], p_expand_low=b['expansion']['low'], p_expand_high=b['expansion']['high'],
            arr=b['revenue']['arr'], expected_arr_end=b['revenue']['expected_arr_end'], expected_arr_low=b['revenue']['low'],
            expected_arr_high=b['revenue']['high'],
            decision_point_at=date.fromisoformat(b['decision_point']['at']) if b['decision_point'].get('at') else None,
            stratum=b['inputs']['stratum'], n_labels=gate['n'], forecast_json=b,
        ))
        jd.journey_json = embed_forecast(dict(jd.journey_json), b, acct)
        jd.updated_at = datetime.utcnow()
    db.session.commit()
    logger.info('Wizard D: customer=%s run=%s accounts=%d basis=%s labels=%d/%d', customer_id, run_id, len(blocks),
                portfolio['basis_counts'], gate['n'], gate['needed'])
    return results


def not_run_block() -> dict:
    return {'generator': f'{LENS}_{GENERATOR_VERSION}', 'lens': LENS, 'status': 'not_run', 'basis': None,
            'note': "no Foresight run for this account yet — trigger_wizard(customer_id, 'd') or process_data"}


def with_staleness(block: dict, journey: dict) -> dict:
    """Mark a stored block stale when the journey has moved past the run it was read from."""
    if block.get('status') != 'forecast':
        return block
    run_as_of = _d(block.get('as_of'))
    j_as_of = _d(journey.get('as_of'))
    last_ev = _d(journey.get('last_evidence_at'))
    stale_reason = None
    if run_as_of and j_as_of and j_as_of > run_as_of:
        stale_reason = f"journey as_of {j_as_of.date().isoformat()} is after the forecast's {run_as_of.date().isoformat()}"
    elif run_as_of and last_ev and last_ev > run_as_of:
        stale_reason = f"evidence dated {last_ev.date().isoformat()} arrived after the forecast's as_of {run_as_of.date().isoformat()}"
    block = dict(block)
    block['stale'] = stale_reason is not None
    block['stale_reason'] = stale_reason
    return block


def latest_forecast_block(account_id: int, journey: dict) -> dict:
    """The block build_journey embeds: the newest stored forecast for the account, staleness marked."""
    from models import AccountForecast
    row = AccountForecast.query.filter_by(account_id=int(account_id)).order_by(AccountForecast.id.desc()).first()
    if not row:
        return not_run_block()
    return with_staleness(row.forecast_json, journey)


def embed_forecast(journey: dict, block: dict, account) -> dict:
    """Set journey['forecast'] and re-render the narrative (pure over the journey JSON) so the cited
    forecast sentence appears without a full rebuild."""
    from journeys.narrative import build_narrative
    from journeys.journey_builder import _rejected_evidence
    journey['forecast'] = with_staleness(block, journey)
    journey['narrative'] = build_narrative(journey, rejected=_rejected_evidence(account))
    return journey


def get_forecast(customer_id: int, account_id: Optional[int] = None) -> Optional[dict]:
    """Latest run: the portfolio block with one compact row per account, or one account's full block."""
    from models import ForecastRun, AccountForecast, Account
    run = ForecastRun.query.filter_by(customer_id=int(customer_id)).order_by(ForecastRun.id.desc()).first()
    if not run:
        return None
    head = {'run_id': run.run_id, 'generator_version': run.generator_version, 'lens': LENS, 'vertical': run.vertical,
            'horizon_days': run.horizon_days, 'as_of': run.as_of.isoformat(), 'created_at': run.created_at.isoformat(),
            'basis_counts': run.basis_counts, 'labels': run.labels}
    if account_id is not None:
        row = AccountForecast.query.filter_by(run_id=run.run_id, account_id=int(account_id)).first()
        if not row:
            return None
        return head | {'account_id': int(account_id), 'forecast': row.forecast_json}
    rows = (AccountForecast.query.filter_by(run_id=run.run_id).join(Account, Account.account_id == AccountForecast.account_id)
            .add_entity(Account).order_by(Account.account_name).all())
    return head | {'portfolio': run.portfolio, 'accounts': [
        {'account_id': r.account_id, 'account_name': a.account_name, 'basis': r.basis, 'p_retain': float(r.p_retain),
         'p_retain_low': float(r.p_retain_low), 'p_retain_high': float(r.p_retain_high), 'p_expand': float(r.p_expand),
         'arr': float(r.arr), 'expected_arr_end': float(r.expected_arr_end), 'expected_arr_low': float(r.expected_arr_low),
         'expected_arr_high': float(r.expected_arr_high), 'decision_point_at': r.decision_point_at.isoformat() if r.decision_point_at else None,
         'decision_status': (r.forecast_json.get('decision_point') or {}).get('status'), 'stratum': r.stratum}
        for r, a in rows]}
