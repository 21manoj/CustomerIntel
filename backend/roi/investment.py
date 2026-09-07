"""
Investment cost — what it costs to move a health point, on this tenant's real playbook catalog
(config/investment/<vertical>.json; the cost-side companion to config/economics/<vertical>.json,
which prices the point itself — see roi.power_of_1).

    investment_cost(customer_id)

Chain, every link labelled:
  csm_hourly_cost                 assumed   config/investment/<vertical>.json, one figure for the vertical
  estimated_csm_hours             assumed   config/investment/<vertical>.json, per playbook
  estimated_cost_per_execution    assumed   csm_hourly_cost x estimated_csm_hours — both links assumed,
                                             so the product is assumed, never 'derived' just because
                                             arithmetic was involved (roi.basis.money()'s weakest-link rule)
  lift (for cost_per_health_point) measured when roi.measured.playbook_health_lift finds >=5 qualifying
                                             closed instances of that playbook, else the config file's own
                                             estimated_health_point_lift_per_execution (also assumed)
  cost_per_health_point           assumed   estimated_cost_per_execution / lift — computed at QUERY TIME,
                                             never pre-computed or stored (mirrors how power_of_1.py never
                                             stores revenue_per_pillar_point permanently). Cost itself is
                                             never measured (no cost-tracking model exists, by design), so
                                             this ratio's basis stays 'assumed' even when the lift
                                             denominator is measured — the weakest link in the chain wins.
A playbook whose config lift is null (a pure-expansion playbook, or one with no pillar to attribute a
lift to — see each investment file's own notes) and that has no measured lift either gets no
cost_per_health_point: never forced, never guessed.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from roi import settings
from roi.basis import assumed_link, money


def _lift_for_playbook(playbook_id: str, lift_cfg: dict, views: List[dict], hooks: Dict[int, dict]) -> dict:
    """Resolve the lift used for cost-per-point: a measured average (>=5 qualifying closed instances of
    THIS playbook) preferred over the config file's estimated placeholder — the query-time rule documented
    on every investment file's playbooks.<id>.estimated_health_point_lift_per_execution. Never averages
    the two; below the measured minimum, the estimate is used as-is, not blended."""
    from roi.measured import playbook_health_lift
    measured = playbook_health_lift(playbook_id, views, hooks)
    if measured['status'] == 'ok':
        n = measured['qualifying_interventions']
        return {'source': 'measured', 'value': measured['measured_health_point_lift'],
                'chain_link': f"measured: average health-point lift over {n} qualifying closed instances "
                              f"of {playbook_id} ({measured['intervention_ids']})",
                'measured': measured, 'estimated': lift_cfg}
    if lift_cfg.get('value') is not None:
        return {'source': 'estimated', 'value': float(lift_cfg['value']), 'chain_link': assumed_link(lift_cfg['basis']),
                'measured': measured, 'estimated': lift_cfg}
    return {'source': 'not_applicable', 'value': None, 'chain_link': None, 'measured': measured, 'estimated': lift_cfg}


def playbook_investment(playbook_id: str, pb: dict, hourly: dict, views: List[dict], hooks: Dict[int, dict]) -> dict:
    """One playbook's cost figures + the lift resolution + the query-time cost-per-point. `pb` is the
    playbook's own entry from config/investment/<vertical>.json (already validated by roi.settings.investment)."""
    hourly_link = assumed_link(hourly['basis'])
    hours = pb['estimated_csm_hours']
    hours_link = assumed_link(hours['basis'])
    cost_cfg = pb['estimated_cost_per_execution']
    cost = money(cost_cfg['value'], 'assumed', [hourly_link, hours_link], note=cost_cfg.get('note'))

    lift_cfg = pb['estimated_health_point_lift_per_execution']
    lift = _lift_for_playbook(playbook_id, lift_cfg, views, hooks)
    if lift['value'] is not None and lift['value'] > 0:
        cost_per_point = money(cost_cfg['value'] / lift['value'], 'assumed', [hourly_link, hours_link, lift['chain_link']],
                               note=f"{lift['source']} lift ({round(lift['value'], 2)} points per execution)")
    else:
        fallback_note = lift_cfg.get('note') or 'no health-point lift for this playbook'
        cost_per_point = money(None, 'assumed', [hourly_link, hours_link], note=fallback_note)

    return {
        'playbook_id': playbook_id, 'targets_pillars': list(pb.get('targets_pillars') or []), 'targets_kpis': list(pb.get('targets_kpis') or []),
        'estimated_csm_hours': hours, 'estimated_cost_per_execution': cost,
        'lift': {'source': lift['source'], 'value': lift['value'], 'measured': lift['measured'], 'estimated': lift_cfg},
        'cost_per_health_point': cost_per_point,
    }


def _rollup(codes: dict, by_id: Dict[str, dict], is_pillar: bool) -> List[dict]:
    """pillars.<code> or kpis.<code> from the investment file, with each covering playbook's cost/lift
    figures attached — a rollup, not a second cost estimate: the same playbook's numbers, referenced."""
    rows = []
    for code, cdef in sorted(codes.items()):
        covering = [by_id[pid] for pid in (cdef.get('playbooks') or []) if pid in by_id]
        row = {
            ('pillar' if is_pillar else 'kpi'): code, 'name': cdef.get('name'),
            'status': cdef.get('status') or ('covered' if covering else 'not_covered'),
            'playbooks': [{'playbook_id': r['playbook_id'], 'estimated_cost_per_execution': r['estimated_cost_per_execution'],
                          'cost_per_health_point': r['cost_per_health_point']} for r in covering],
            'note': cdef.get('note'),
        }
        rows.append(row)
    return rows


def investment_cost(customer_id: int) -> dict:
    """What it costs to run this tenant's real playbooks, and the $ per health point that buys — every
    playbook in config/investment/<vertical>.json, rolled up by pillar and by KPI exactly as that file
    defines coverage (not_covered where no playbook targets it — never guessed). Every dollar figure
    carries basis + basis_chain; cost is always 'assumed' (no cost-tracking model exists, by design)."""
    from playbooks.governance import list_interventions
    from roi.measured import intervention_hooks
    from utils.vertical_registry import get_vertical_for_customer
    from journeys.read import origin_block
    vertical = get_vertical_for_customer(customer_id)          # raises: no fallback vertical
    inv = settings.investment(vertical)                        # raises: no investment file for this vertical
    li = list_interventions(int(customer_id))
    views = li['interventions']
    hooks = intervention_hooks(customer_id)
    hourly = inv['csm_hourly_cost']

    playbook_rows = [playbook_investment(pid, pb, hourly, views, hooks) for pid, pb in sorted(inv['playbooks'].items())]
    by_id = {r['playbook_id']: r for r in playbook_rows}

    return {
        'customer_id': int(customer_id), 'vertical': vertical, **origin_block(customer_id),
        'basis': inv['basis'], 'csm_hourly_cost': hourly,
        'playbooks': playbook_rows,
        'pillars': _rollup(inv['pillars'], by_id, is_pillar=True),
        'kpis': _rollup(inv['kpis'], by_id, is_pillar=False),
        'note': 'cost is always assumed (no cost-tracking model exists by design, a deliberate product decision — '
                'see Intervention/playbook config, which carries no cost fields). cost_per_health_point is computed '
                'at query time (estimated_cost_per_execution / lift), never stored, mirroring revenue_per_pillar_point. '
                'The lift prefers a measured average from this playbook\'s own closed interventions (>=5 qualifying) '
                'over the config file\'s estimated placeholder, but the ratio\'s basis stays assumed either way, since '
                'cost itself is never measured (roi.basis weakest-link rule): a computed value from an assumed and a '
                'measured input is itself only as strong as its weakest link.',
    }
