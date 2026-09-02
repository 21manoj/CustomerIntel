"""
Shared feature vector — the one definition Wizards B and D both read.

Everything here is computed relative to `as_of` (the last scored month's
end). Role counts come from the taxonomy's signal roles, so a datacenter
tenant's `reliability_sla_breach` counts toward `infra_incident` exactly
as a SaaS tenant's `system_outage` does.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Dict, List

import utils.health_thresholds as ht
from journeys.journey_builder import Episode, slope_pts_per_month, trajectory_label


def days_to_renewal_band(days) -> str:
    if days is None:
        return 'unknown'
    if days <= 30:
        return '0-30'
    if days <= 90:
        return '31-90'
    if days <= 180:
        return '91-180'
    if days <= 365:
        return '181-365'
    return '>365'


def compute(account, vertical: str, taxonomy, points: List[tuple], episodes: List[Episode],
            phases: List[dict], lvt: dict, as_of: datetime) -> dict:
    from models import ContextNode
    from utils.vertical_registry import role as pillar_role

    scores = [s for _, s in points]
    health_now = scores[-1] if scores else None
    slope_1 = slope_pts_per_month(points, 1)
    slope_3 = slope_pts_per_month(points, 3)
    deltas = [b - a for a, b in zip(scores[-4:-1], scores[-3:])] if len(scores) >= 3 else []
    volatility = round(statistics.pstdev(deltas), 2) if len(deltas) >= 2 else 0.0

    def _in_window(e: Episode, days: int) -> bool:
        return as_of - timedelta(days=days) < e.date <= as_of

    role_counts_90: Dict[str, int] = {}
    role_counts_total: Dict[str, int] = {}
    unmapped_90 = 0
    last_role_at: Dict[str, str] = {}
    for e in episodes:
        if e.kind != 'signal':
            continue
        key = e.role or 'unmapped'
        role_counts_total[key] = role_counts_total.get(key, 0) + 1
        if _in_window(e, 90):
            role_counts_90[key] = role_counts_90.get(key, 0) + 1
            if not e.role:
                unmapped_90 += 1
        if e.role and e.date <= as_of:
            last_role_at[e.role] = e.date.date().isoformat()

    renewal = None
    for e in episodes:
        if e.kind == 'renewal':
            renewal = (e.date - as_of).days
    if renewal is None:
        renewal = None
    else:
        renewal = max(renewal, 0)

    stakeholder_roles = sorted({
        (n.node_subtype or '').lower() for n in ContextNode.query.filter_by(
            account_id=account.account_id, node_type='STAKEHOLDER').all()
    })

    current = phases[-1] if phases else None
    time_in_phase_days = None
    if current:
        entered = datetime.fromisoformat(current['entered_at'])
        time_in_phase_days = (as_of - entered).days

    adoption_pillar = pillar_role(vertical, 'adoption')
    adoption_delta_3mo = None
    if adoption_pillar:
        from models import HealthScore
        rows = HealthScore.query.filter_by(account_id=account.account_id).order_by(
            HealthScore.measurement_month).all()
        vals = [float((r.contributing_pillars or {}).get(adoption_pillar)) for r in rows
                if (r.contributing_pillars or {}).get(adoption_pillar) is not None]
        if len(vals) >= 2:
            adoption_delta_3mo = round(vals[-1] - vals[-min(4, len(vals))], 2)

    last_outcome = next((e for e in reversed(episodes) if e.kind == 'outcome'), None)
    latest = lvt['series'][-1] if lvt.get('series') else None
    traj, traj_conf = trajectory_label(scores)

    return {
        'as_of': as_of.isoformat(),
        'health_now': health_now,
        'health_band': ht.classify(health_now) if health_now is not None else None,
        'health_slope_1mo': round(slope_1, 2),
        'health_slope_3mo': round(slope_3, 2),
        'volatility_3mo': volatility,
        'months_scored': len(points),
        'min_health': min(scores) if scores else None,
        'dipped_below_at_risk': any(s < ht.at_risk_min() for s in scores),
        'current_phase': current['name'] if current else None,
        'phases_seen': [p['name'] for p in phases],
        'had_negative_phase': any(p['name'] in ('deterioration', 'intervention') for p in phases),
        'time_in_phase_days': time_in_phase_days,
        'days_to_renewal': renewal,
        'days_to_renewal_band': days_to_renewal_band(renewal),
        'signal_role_counts_90d': role_counts_90,
        'signal_role_counts_total': role_counts_total,
        'unmapped_signals_90d': unmapped_90,
        'last_role_at': last_role_at,
        'stakeholder_roles_present': stakeholder_roles,
        'adoption_pillar': adoption_pillar,
        'adoption_delta_3mo': adoption_delta_3mo,
        'qual_now': latest['qual'] if latest else None,
        'divergence_now': latest['divergence'] if latest else None,
        'early_warning_now': latest['early_warning'] if latest else None,
        'first_leading_warning_at': lvt.get('first_leading_warning_at'),
        'first_trailing_warning_at': lvt.get('first_trailing_warning_at'),
        'lead_days': lvt.get('lead_days'),
        'latest_outcome_bucket': last_outcome.revenue_bucket if last_outcome else None,
        'trajectory': traj,
        'trajectory_confidence': round(traj_conf, 2),
    }
