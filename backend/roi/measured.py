"""
Measured impact — what the last interventions returned (design §3).

    roi(customer_id)

Reuses playbooks.governance.list_interventions for realized $ vs exposure $
per playbook (two numbers, never summed) and adds:
  by_pillar        the same two numbers per pillar — trigger roles → pillar role
                   (power_of_1.json attribution) → the vertical's pillar via
                   vertical_registry.role(); an intervention citing roles in two
                   pillars is counted under both (do not sum pillars); roles the
                   vertical has no pillar for land in 'unmapped', visibly, with a
                   'reason' block (which roles had nowhere to go, and the catalog's
                   own pillar_roles_notes when it documents the decision — e.g.
                   healthcare_provider's Patient Outcomes/Operational Efficiency/
                   Provider Satisfaction pillars are deliberately unmapped, not
                   a bug) so a bare 'unmapped' status doesn't read as broken
  ledger           every OUTCOME on the journeys by revenue bucket, and the subset
                   linked to interventions — cited by node id
  hindsight        Wizard B's latest run: intervention lift rows, realized NRR,
                   evidence label, run id (no run = says so; it is not computed here)
  sensitivity      measured $ per health point from closed interventions that carry
                   both a health lift (the journey's counterfactual hook) and a
                   revenue outcome — only at or above the configured minimum;
                   below it 'insufficient_data' with the count, never a number.
                   The assumed figure sits beside it under its own key.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from roi import settings
from roi.basis import assumed_link, money

UNMAPPED = 'unmapped'
INSUFFICIENT = 'insufficient_data'


def _pillars_for_roles(vertical: str, roles: List[str]) -> Dict[str, List[str]]:
    """{pillar_code | 'unmapped': [signal roles]} for one intervention's trigger roles."""
    from utils.vertical_registry import role as pillar_for
    amap = settings.get('attribution', 'signal_role_to_pillar_role')
    out: Dict[str, List[str]] = {}
    for r in roles or []:
        pr = amap.get(r)
        code = pillar_for(vertical, pr) if pr else None
        out.setdefault(code or UNMAPPED, []).append(r)
    return out


def _unmapped_reason(vertical: str, roles: List[str]) -> dict:
    """Why these signal roles landed in 'unmapped' for this vertical, so a reader doesn't
    mistake a deliberate catalog design decision (e.g. healthcare_provider's Patient
    Outcomes/Operational Efficiency/Provider Satisfaction pillars, which the catalog's own
    pillar_roles_notes says have "no clean match in the shared vocabulary ... left unmapped
    rather than force-fit") for missing data or a bug. Distinguishes two distinct causes:
      pillar_roles_with_no_pillar          the signal role resolves to a pillar role
                                            (power_of_1.json attribution) that this vertical's
                                            catalog simply has no pillar for
      signal_roles_with_no_attribution_entry  the signal role isn't in the attribution map at all
    and, when the catalog documents its own reasoning (pillar_roles_notes), includes it verbatim
    rather than trying to guess which note applies to which role.
    """
    from utils.vertical_registry import role as pillar_for, get_pillar_roles_notes
    amap = settings.get('attribution', 'signal_role_to_pillar_role')
    no_attribution, no_pillar = set(), set()
    for r in roles:
        pr = amap.get(r)
        if pr is None:
            no_attribution.add(r)
        elif pillar_for(vertical, pr) is None:
            no_pillar.add(pr)
    reason: Dict[str, object] = {}
    if no_pillar:
        reason['pillar_roles_with_no_pillar'] = sorted(no_pillar)
    if no_attribution:
        reason['signal_roles_with_no_attribution_entry'] = sorted(no_attribution)
    notes = get_pillar_roles_notes(vertical)
    if notes:
        reason['catalog_pillar_roles_notes'] = notes
    return reason


def by_pillar(vertical: str, views: List[dict], pillars: dict) -> List[dict]:
    rows: Dict[str, dict] = {}
    for v in views:
        for code, roles in _pillars_for_roles(vertical, v['trigger'].get('roles') or []).items():
            s = rows.setdefault(code, {'pillar': code, 'name': (pillars.get(code) or {}).get('name') if code != UNMAPPED else 'no pillar for these roles in this vertical',
                                      'interventions': 0, 'closed_done': 0, 'outcomes_reported': 0, 'realized_revenue': 0.0, 'exposure_revenue': 0.0,
                                      'roles': set(), 'intervention_ids': [], 'outcome_node_ids': []})
            s['interventions'] += 1
            s['roles'].update(roles)
            s['intervention_ids'].append(v['intervention_id'])
            if v['state'] == 'closed' and v['closed_state'] == 'done':
                s['closed_done'] += 1
            if v.get('exposure_revenue') is not None:
                s['exposure_revenue'] += float(v['exposure_revenue'])
            oc = v.get('outcome') or {}
            if oc.get('node_id'):
                s['outcomes_reported'] += 1
                s['outcome_node_ids'].append(oc['node_id'])
                if oc.get('revenue') is not None:
                    s['realized_revenue'] += float(oc['revenue'])
    out = []
    for s in sorted(rows.values(), key=lambda x: (x['pillar'] == UNMAPPED, x['pillar'])):
        row = {**{k: v for k, v in s.items() if k not in ('realized_revenue', 'exposure_revenue', 'roles')},
               'roles': sorted(s['roles']),
               'realized_revenue': money(s['realized_revenue'], 'measured', [f"measured: outcome nodes {s['outcome_node_ids']}"]) if s['outcome_node_ids']
               else money(None, 'measured', note='no outcome reported yet'),
               'exposure_revenue': money(s['exposure_revenue'], 'derived', ['derived: account revenue on the intervention rows']),
               'note': 'an intervention citing roles in two pillars is counted under both; do not sum pillars'}
        if s['pillar'] == UNMAPPED:
            # A bare 'unmapped' status reads as broken; attach why, so a catalog's deliberate
            # design decision (e.g. a vertical with no clean shared-vocabulary match for a
            # pillar) isn't mistaken for missing data.
            row['reason'] = _unmapped_reason(vertical, sorted(s['roles']))
        out.append(row)
    return out


def _ledger(customer_id: int, linked_outcome_ids: set) -> dict:
    from models import JourneyData
    buckets = settings.get('measured', 'ledger_buckets')
    totals: Dict[str, dict] = {b: {'bucket': b, 'outcomes': 0, 'with_revenue': 0, 'revenue': 0.0, 'node_ids': [],
                                   'linked_to_interventions': 0, 'linked_revenue': 0.0} for b in buckets}
    other = {'outcomes': 0, 'subtypes': set()}
    for jd in JourneyData.query.filter_by(customer_id=int(customer_id)).all():
        for e in (jd.journey_json or {}).get('episodes', []):
            if e.get('kind') != 'outcome':
                continue
            b = e.get('revenue_bucket')
            if b not in totals:
                other['outcomes'] += 1
                other['subtypes'].add(e.get('subtype'))
                continue
            t = totals[b]
            t['outcomes'] += 1
            nid = (e.get('evidence_node_ids') or [None])[0]
            t['node_ids'].append(nid)
            linked = nid in linked_outcome_ids
            t['linked_to_interventions'] += int(linked)
            if e.get('revenue') is not None:
                t['with_revenue'] += 1
                t['revenue'] += float(e['revenue'])
                if linked:
                    t['linked_revenue'] += float(e['revenue'])
    rows = []
    for t in totals.values():
        rows.append({**{k: v for k, v in t.items() if k not in ('revenue', 'linked_revenue')},
                     'revenue': money(t['revenue'], 'measured', [f"measured: OUTCOME nodes {t['node_ids']}"]) if t['with_revenue']
                     else money(None, 'measured', note='no revenue figure on these outcomes' if t['outcomes'] else 'no outcomes in this bucket'),
                     'linked_revenue': money(t['linked_revenue'], 'measured', ['measured: the subset reported through report_intervention']) if t['linked_to_interventions']
                     else money(None, 'measured', note='none linked to an intervention')})
    return {'by_bucket': rows, 'outside_buckets': {'outcomes': other['outcomes'], 'subtypes': sorted(s for s in other['subtypes'] if s)},
            'note': 'signed as stored: lost negative, expansion/protected positive. Buckets are reported side by side, not netted, '
                    'except realized_nrr in the hindsight block (lost and expansion only, Wizard B\'s rule).'}


def _hindsight(customer_id: int) -> dict:
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
            'journeys': res.get('journeys')}


def _sensitivity(customer_id: int, views: List[dict], econ: dict) -> dict:
    """Measured $ per health point: closed-done interventions with a revenue outcome in a positive bucket and a
    positive health lift on the journey's counterfactual hook. Gated on the configured minimum."""
    from models import JourneyData
    cfg = settings.get('measured')
    positive = set(cfg['revenue_buckets_positive'])
    need = int(cfg['min_interventions_for_sensitivity'])
    hooks: Dict[int, dict] = {}
    for jd in JourneyData.query.filter_by(customer_id=int(customer_id)).all():
        for h in (jd.journey_json or {}).get('counterfactual_hooks', []):
            if str(h.get('episode_id', '')).startswith('int:'):
                hooks[int(h['episode_id'][4:])] = h
    pairs = []
    for v in views:
        oc = v.get('outcome') or {}
        if not (v['state'] == 'closed' and v['closed_state'] == 'done' and v.get('node_id') and oc.get('revenue') is not None):
            continue
        h = hooks.get(v['node_id'])
        if not h:
            continue
        before, after = (h.get('health_before') or {}).get('last'), (h.get('health_after') or {}).get('last')
        if before is None or after is None:
            continue
        lift = float(after) - float(before)
        bucket = next((o.get('bucket') for o in h.get('outcomes_after') or [] if o.get('episode_id') == f"out:{oc['node_id']}"), None)
        if lift <= 0 or bucket not in positive:
            continue
        pairs.append({'intervention_id': v['intervention_id'], 'outcome_node_id': oc['node_id'], 'lift_pts': round(lift, 2),
                      'revenue': float(oc['revenue']), 'bucket': bucket})
    assumed = econ['retention_sensitivity_per_health_point']
    out = {'minimum_interventions': need, 'qualifying_interventions': len(pairs), 'pairs': pairs,
           'assumed_revenue_share_per_health_point': {'value': float(assumed['value']), 'basis': assumed_link(assumed['basis'])},
           'note': 'measured and assumed sit side by side; the assumed figure is never scaled by, blended with, or replaced by a '
                   'measured one below the minimum'}
    if len(pairs) < need:
        out.update({'status': INSUFFICIENT, 'measured_revenue_per_health_point': money(None, 'measured',
                    note=f'{len(pairs)} of {need} closed interventions carry both a positive health lift and a positive revenue outcome')})
        return out
    lift_total = sum(p['lift_pts'] for p in pairs)
    rev_total = sum(p['revenue'] for p in pairs)
    out.update({'status': 'ok', 'measured_revenue_per_health_point': money(rev_total / lift_total, 'measured',
                [f"measured: Σ revenue of outcomes {[p['outcome_node_id'] for p in pairs]} / Σ health lift on interventions {[p['intervention_id'] for p in pairs]}"])})
    return out


def roi(customer_id: int) -> dict:
    from models import Account
    from mcp_server.common import get_account_arr
    from playbooks.governance import list_interventions
    from utils.vertical_registry import get_vertical_for_customer, get_pillars
    from journeys.read import origin_block
    vertical = get_vertical_for_customer(customer_id)          # raises: no fallback vertical
    pillars = get_pillars(vertical)
    econ = settings.economics(vertical)
    li = list_interventions(int(customer_id))
    views = li['interventions']
    revenue_base = sum(get_account_arr(a) for a in Account.query.filter_by(customer_id=int(customer_id)).all())
    playbook_rows = []
    for s in li['by_playbook']:
        ids = [v['intervention_id'] for v in views if v['playbook_id'] == s['playbook_id']]
        onodes = [v['outcome']['node_id'] for v in views if v['playbook_id'] == s['playbook_id'] and v.get('outcome')]
        playbook_rows.append({
            **{k: v for k, v in s.items() if k not in ('realized_revenue', 'exposure_revenue', 'note')},
            'intervention_ids': ids, 'outcome_node_ids': onodes,
            'realized_revenue': money(s['realized_revenue'], 'measured', [f'measured: outcome nodes {onodes}']) if onodes
            else money(None, 'measured', note='no outcome reported yet'),
            'exposure_revenue': money(s['exposure_revenue'], 'derived', ['derived: account revenue on the intervention rows']),
            'note': s['note'],
        })
    linked_ids = {v['outcome']['node_id'] for v in views if v.get('outcome')}
    return {
        'customer_id': int(customer_id), 'vertical': vertical, **origin_block(customer_id),
        'revenue_base': money(revenue_base, 'derived', ['derived: Σ Account.revenue (get_account_arr)']),
        'interventions': {'count': li['count'], 'stuck': li['stuck'], 'source': 'list_interventions'},
        'by_playbook': playbook_rows,
        'by_pillar': by_pillar(vertical, views, pillars),
        'ledger': _ledger(customer_id, linked_ids),
        'hindsight': _hindsight(customer_id),
        'sensitivity': _sensitivity(customer_id, views, econ),
        'note': 'realized = linked outcomes (measured, cited); exposure = account revenue on the rows (derived). Two numbers, never summed. '
                'Investment scenarios and $ per point live in get_power_of_1.',
    }
