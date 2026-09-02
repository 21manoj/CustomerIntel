"""
Lead-time backtest (Hindsight) — does the behavioral layer warn before
the financial event, and by how much?

Implements the pre-registered protocol in docs/design/wizard-a-assessment.md
§7 against a tenant's journeys (JourneyData v3, written by Wizard A v2):

  H1 (retention): for each negative financial event at T_f (an OUTCOME
      episode whose revenue bucket is in `event_buckets`, default 'lost'),
      the first month before T_f — within `horizon_days` — whose leading
      layer carried an early_warning (or qual < at-risk) gives T_l; the
      first month whose trailing kpi_only < at-risk gives T_t. Lead time
      is T_f − T_l (and T_f − T_t for the comparator), using the month's
      END as the date the composite was available — conservative.
  H2 (growth): same shape with 'expansion' events and the recovery_watch
      label.

  False alarms: warning account-months not followed by an event of that
  kind within `horizon_days`, per 100 account-months.

  Verdict against the pre-registered thresholds (median ≥ 60 days,
  recall ≥ 0.70, false alarms ≤ 5 / 100 account-months):
  'supported' / 'refuted' / 'insufficient_data' (fewer than `min_events`).

  Evidence label: 'measured' ONLY when the tenant's data_origin is NULL
  AND the caller asserts the data is real (`assert_real=True` /
  `--real`). Everything else is 'synthetic_or_unverified — not evidence':
  the load-driver derives signals from story phases, so a lead time on a
  synthetic tenant is the generator's own parameter read back, and
  manifest-mode tenants are not stamped as synthetic yet.

Pure over the journey rows — no scoring, no writes. Point-in-time by
construction: the series is monthly and each month's qual uses only
signals dated on or before that month's end.

    python -m evals.lead_time_backtest --customer-id 415 [--horizon-days 180]
        [--event-buckets lost] [--min-events 10] [--real] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from calendar import monthrange
from datetime import date, datetime
from typing import Dict, List, Optional

PREREGISTERED = {'median_lead_days_min': 60, 'recall_min': 0.70, 'false_alarms_per_100_max': 5.0}
H1 = {'name': 'H1_retention', 'event_buckets': ('lost',), 'warning_label': 'early_warning', 'qual_below_at_risk_counts': True,
      'warning_roles': ()}
# H2's leading indicator is expansion-INTENT behavior in the window (the
# roles), not qual far above kpi — a healthy account's qual can't exceed
# its kpi layer by the divergence threshold, so the label alone never fires.
H2 = {'name': 'H2_growth', 'event_buckets': ('expansion',), 'warning_label': 'recovery_watch', 'qual_below_at_risk_counts': False,
      'warning_roles': ('expansion_intent', 'expansion_realized')}


def _month_end(m: date) -> datetime:
    return datetime(m.year, m.month, monthrange(m.year, m.month)[1], 23, 59, 59)


def _quantiles(vals: List[int]) -> dict:
    if not vals:
        return {'n': 0, 'median': None, 'p25': None, 'p75': None, 'mean': None, 'min': None, 'max': None}
    s = sorted(vals)
    q = statistics.quantiles(s, n=4) if len(s) >= 2 else [s[0], s[0], s[0]]
    return {'n': len(s), 'median': statistics.median(s), 'p25': q[0], 'p75': q[2],
            'mean': round(statistics.mean(s), 1), 'min': s[0], 'max': s[-1]}


def _warning_months(series: List[dict], hyp: dict, at_risk_min: float, healthy_min: float, layer: str) -> List[date]:
    out = []
    prev_kpi = None
    for s in series:
        m = date.fromisoformat(s['month'])
        if layer == 'leading':
            roles = s.get('roles') or {}
            if s.get('qual') is not None and (
                    s.get('early_warning') == hyp['warning_label']
                    or (hyp['qual_below_at_risk_counts'] and s['qual'] < at_risk_min)
                    or any(roles.get(r) for r in hyp.get('warning_roles', ()))):
                out.append(m)
        else:
            # Trailing comparator = the KPI layer's own threshold crossing:
            # H1 below at-risk; H2 crossing UP into healthy from below.
            if hyp['name'] == 'H1_retention' and s['kpi_only'] < at_risk_min:
                out.append(m)
            if (hyp['name'] == 'H2_growth' and prev_kpi is not None
                    and prev_kpi < healthy_min <= s['kpi_only']):
                out.append(m)
        prev_kpi = s['kpi_only']
    return out


LAYERS = ('leading', 'trailing', 'crm')


def _evaluate(journeys: List[dict], hyp: dict, horizon_days: int, at_risk_min: float, healthy_min: float) -> dict:
    events = []
    leads = {layer: [] for layer in LAYERS}
    hits = {layer: 0 for layer in LAYERS}
    fa = {layer: 0 for layer in LAYERS}
    censored = {layer: 0 for layer in LAYERS}
    account_months = 0
    per_event = []

    # Right-censoring: a warning less than `horizon_days` before the end of
    # the data can't be judged yet — the story is still open. Those are
    # reported as `censored`, not counted as false alarms.
    data_end = max(
        (_month_end(date.fromisoformat(s['month'])) for j in journeys for s in j['leading_vs_trailing']['series']),
        default=datetime.utcnow(),
    )

    for j in journeys:
        series = j['leading_vs_trailing']['series']
        account_months += len(series)
        ev = [e for e in j['episodes'] if e['kind'] == 'outcome' and e.get('revenue_bucket') in hyp['event_buckets']]
        ev_dates = [datetime.fromisoformat(e['date']) for e in ev]
        # Each warning = (available_at, window_start). Monthly layers are
        # available at the month's END (conservative) and judged from the
        # month's START; the CRM flag is a dated episode, used as-is.
        warn: Dict[str, List[tuple]] = {}
        for layer in ('leading', 'trailing'):
            warn[layer] = [(_month_end(m), datetime(m.year, m.month, 1))
                           for m in _warning_months(series, hyp, at_risk_min, healthy_min, layer)]
        warn['crm'] = sorted({
            (datetime.fromisoformat(e['date']), datetime.fromisoformat(e['date'])) for e in j['episodes']
            if hyp['name'] == 'H1_retention' and e['kind'] == 'signal' and e.get('role') == 'crm_flag'
        })

        for e, t_f in zip(ev, ev_dates):
            events.append(e)
            rec = {'account': j['account_name'], 'event': e['subtype'], 'event_date': t_f.date().isoformat(),
                   'revenue': e.get('revenue')}
            for layer in LAYERS:
                cands = [avail for avail, _ in warn[layer] if avail <= t_f and (t_f - avail).days <= horizon_days]
                if cands:
                    t_w = min(cands)
                    lead = (t_f - t_w).days
                    leads[layer].append(lead)
                    hits[layer] += 1
                    rec[f'{layer}_warned_at'] = t_w.date().isoformat()
                    rec[f'{layer}_lead_days'] = lead
                else:
                    rec[f'{layer}_warned_at'] = None
                    rec[f'{layer}_lead_days'] = None
            per_event.append(rec)

        # A warning is a false alarm only if no event follows within the
        # horizon of its window start — a crossing in the same month as
        # the event is a late warning (no lead credited), not a false one —
        # and only once enough data exists after it to know.
        for layer in LAYERS:
            for _avail, t_start in warn[layer]:
                if any(t_start <= t_f and (t_f - t_start).days <= horizon_days for t_f in ev_dates):
                    continue
                if (data_end - t_start).days < horizon_days:
                    censored[layer] += 1
                else:
                    fa[layer] += 1

    n = len(events)
    result = {'hypothesis': hyp['name'], 'events': n, 'account_months': account_months,
              'data_end': data_end.date().isoformat(), 'per_event': per_event}
    for layer in LAYERS:
        q = _quantiles(leads[layer])
        result[layer] = {
            **q,
            'recall': round(hits[layer] / n, 3) if n else None,
            'false_alarm_months': fa[layer],
            'censored_warning_months': censored[layer],
            'false_alarms_per_100_account_months': round(100.0 * fa[layer] / account_months, 2) if account_months else None,
        }
    lm, tm = result['leading']['median'], result['trailing']['median']
    result['leading_minus_trailing_median_days'] = (lm - tm) if lm is not None and tm is not None else None
    return result


def _verdict(res: dict, min_events: int, thresholds: dict) -> dict:
    if res['events'] < min_events:
        return {'verdict': 'insufficient_data', 'reason': f"{res['events']} events < min_events {min_events}"}
    L = res['leading']
    checks = {
        'median_lead_days': (L['median'], thresholds['median_lead_days_min'], L['median'] is not None and L['median'] >= thresholds['median_lead_days_min']),
        'recall': (L['recall'], thresholds['recall_min'], L['recall'] is not None and L['recall'] >= thresholds['recall_min']),
        'false_alarms_per_100': (L['false_alarms_per_100_account_months'], thresholds['false_alarms_per_100_max'],
                                 L['false_alarms_per_100_account_months'] is not None
                                 and L['false_alarms_per_100_account_months'] <= thresholds['false_alarms_per_100_max']),
    }
    ok = all(v[2] for v in checks.values())
    return {'verdict': 'supported' if ok else 'refuted',
            'checks': {k: {'value': v[0], 'threshold': v[1], 'pass': v[2]} for k, v in checks.items()}}


def run_backtest(customer_id: int, *, horizon_days: int = 180, min_events: int = 10,
                 event_buckets: Optional[tuple] = None, assert_real: bool = False,
                 thresholds: Optional[dict] = None) -> dict:
    """Run H1 and H2 over a tenant's journeys. Must be called inside an app context."""
    from models import Customer, JourneyData
    import utils.health_thresholds as ht

    thresholds = thresholds or PREREGISTERED
    from extensions import db
    customer = db.session.get(Customer, customer_id)
    journeys = [r.journey_json for r in JourneyData.query.filter_by(customer_id=customer_id).all()]
    if not journeys:
        raise ValueError(f'No journeys for customer {customer_id} — run process_data / Wizard A first.')
    data_origin = getattr(customer, 'data_origin', None)
    measured = assert_real and data_origin is None
    label = 'measured' if measured else 'synthetic_or_unverified — not evidence'
    at_risk_min, healthy_min = ht.at_risk_min(), ht.healthy_min()

    h1 = dict(H1)
    if event_buckets:
        h1['event_buckets'] = tuple(event_buckets)
    hyps = [h1, H2]
    results = {}
    for hyp in hyps:
        r = _evaluate(journeys, hyp, horizon_days, at_risk_min, healthy_min)
        r.update(_verdict(r, min_events, thresholds))
        results[hyp['name']] = r

    return {
        'customer_id': customer_id,
        'customer_name': getattr(customer, 'customer_name', None),
        'vertical': getattr(customer, 'vertical', None),
        'data_origin': data_origin,
        'evidence_label': label,
        'journeys': len(journeys),
        'horizon_days': horizon_days,
        'min_events': min_events,
        'thresholds': thresholds,
        'generated_at': datetime.utcnow().isoformat(),
        'results': results,
    }


def format_report(rep: dict) -> str:
    lines = [
        f"Lead-time backtest — customer {rep['customer_id']} {rep['customer_name'] or ''} ({rep['vertical']})",
        f"evidence: {rep['evidence_label']}  (data_origin={rep['data_origin']!r})  journeys={rep['journeys']}  horizon={rep['horizon_days']}d",
        '',
    ]
    for name, r in rep['results'].items():
        lines.append(f"{name}: events={r['events']}  account_months={r['account_months']}  verdict={r['verdict']}"
                     + (f"  ({r.get('reason')})" if r.get('reason') else ''))
        for layer in LAYERS:
            L = r[layer]
            if layer == 'crm' and L['n'] == 0 and L['false_alarm_months'] == 0:
                continue   # no CRM flags in this tenant's data
            lines.append(f"  {layer:8s} n={L['n']:<3} median={L['median']!s:<6} p25={L['p25']!s:<6} p75={L['p75']!s:<6} "
                         f"recall={L['recall']!s:<6} FA/100mo={L['false_alarms_per_100_account_months']!s:<6} "
                         f"open={L['censored_warning_months']}")
        if r['leading_minus_trailing_median_days'] is not None:
            lines.append(f"  behavioral layer bought {r['leading_minus_trailing_median_days']} days over trailing (median)")
        if r['crm']['median'] is not None and r['leading']['median'] is not None:
            lines.append(f"  behavioral layer bought {r['leading']['median'] - r['crm']['median']} days over the CSM's own flag (median)")
        if r.get('checks'):
            for k, c in r['checks'].items():
                lines.append(f"  check {k}: {c['value']} vs {c['threshold']} → {'pass' if c['pass'] else 'FAIL'}")
        for e in r['per_event'][:12]:
            lines.append(f"    {e['account']:22s} {e['event']:20s} {e['event_date']}  lead={e['leading_lead_days']!s:<5} "
                         f"trail={e['trailing_lead_days']!s:<5} crm={e['crm_lead_days']!s}")
        lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--customer-id', type=int, required=True)
    ap.add_argument('--horizon-days', type=int, default=180)
    ap.add_argument('--min-events', type=int, default=10)
    ap.add_argument('--event-buckets', default='lost', help='comma-separated revenue buckets counted as H1 events')
    ap.add_argument('--real', action='store_true', help='assert the tenant holds real customer history')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from flask import Flask
    from extensions import db
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
    db.init_app(app)
    with app.app_context():
        rep = run_backtest(args.customer_id, horizon_days=args.horizon_days, min_events=args.min_events,
                           event_buckets=tuple(b.strip() for b in args.event_buckets.split(',')), assert_real=args.real)
    print(json.dumps(rep, indent=1, default=str) if args.json else format_report(rep))


if __name__ == '__main__':
    main()
