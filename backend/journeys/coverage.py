"""
Data coverage — derived from what the account actually has, never declared.

    data_coverage(account, points, episodes, as_of) -> dict

kpi_layer: present | stale | not_yet | none — the one distinction the rest of the
journey branches on (phases, arc predicates, backtest comparator, wording), and
every branch says which way it went. Thresholds live in health_thresholds.json.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import utils.health_thresholds as ht


def contract_shape(profile: dict) -> str:
    cfg = ht.data_coverage_config()
    ctype = str((profile or {}).get('contract_type') or '').strip().lower()
    if ctype in cfg['installed_base_contract_types'] or (profile or {}).get('refresh_date') or (profile or {}).get('purchase_date'):
        return 'installed_base'
    if (profile or {}).get('renewal_date') or (profile or {}).get('contract_end') or ctype == 'subscription':
        return 'subscription'
    return 'unknown'


def data_coverage(account, points: List[tuple], episodes, as_of: datetime) -> dict:
    cfg = ht.data_coverage_config()
    profile = getattr(account, 'profile_metadata', None) or {}
    signals = [e for e in episodes if e.kind == 'signal']
    outcomes = [e for e in episodes if e.kind == 'outcome']
    first_ev = min((e.date for e in signals), default=None)
    last_ev = max((e.date for e in signals), default=None)
    span_days = (last_ev - first_ev).days if first_ev and last_ev else 0
    age_days = (as_of - first_ev).days if first_ev else 0
    last_scored = points[-1][0] if points else None
    days_since_kpi = (as_of.date() - last_scored).days if last_scored else None
    shape = contract_shape(profile)

    if points:
        kpi_layer = 'present' if days_since_kpi is not None and days_since_kpi <= cfg['kpi_stale_after_days'] else 'stale'
        basis = (f'{len(points)} scored month(s); last {last_scored.isoformat()} ({days_since_kpi} days before as_of)')
    elif shape == 'installed_base' or (first_ev and age_days >= cfg["no_kpi_layer_after_days"]):
        kpi_layer = 'none'
        basis = ('installed-base contract, no usage telemetry expected' if shape == 'installed_base'
                 else f'no KPI in {age_days} days of evidence (threshold {cfg["no_kpi_layer_after_days"]})')
    else:
        kpi_layer = 'not_yet'
        basis = (f'no KPI yet; evidence is {age_days} days old (threshold {cfg["no_kpi_layer_after_days"]})' if first_ev
                 else 'no KPI and no evidence yet')
    return {
        'kpi_layer': kpi_layer, 'basis': basis,
        'months_scored': len(points), 'last_scored_month': last_scored.isoformat() if last_scored else None,
        'days_since_last_kpi': days_since_kpi,
        'evidence_count': len(signals), 'first_evidence_at': first_ev.isoformat() if first_ev else None,
        'last_evidence_at': last_ev.isoformat() if last_ev else None, 'evidence_span_days': span_days,
        'months_with_evidence': len({e.date.strftime('%Y-%m') for e in signals}),
        'outcome_types_seen': sorted({e.subtype for e in outcomes if e.subtype}),
        'contract_shape': shape,
        'thresholds': {k: cfg[k] for k in ('no_kpi_layer_after_days', 'kpi_stale_after_days')},
    }
