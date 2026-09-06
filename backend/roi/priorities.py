"""
Investment priorities — where the next hour / dollar goes, now (design §3).

    investment_priorities(customer_id, account_id=None)   ranked rows + portfolio totals
    compact_for_rows(customer_id, pairs)                  the same score, compact, for list_journeys rows

Per account: risk_factor = weighted sum of phase, leading layer, the highest
effective urgency on the latest month's cited evidence, renewal proximity;
opportunity_factor from positive roles / recovery_watch / an open
expansion-class intervention. revenue_weighted = revenue × max(risk,
opportunity); lens = protect | grow. Every row cites the episodes it rests
on and lists the open interventions (a proposed row is a decision waiting).
One scoring function serves the tool, the route and the portfolio row.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import utils.health_thresholds as ht
from roi import settings
from roi.basis import money

NONE = 'none'
UNKNOWN = 'unknown'
LENS_PROTECT, LENS_GROW = 'protect', 'grow'
OPEN_STATES = ('proposed', 'approved', 'sent')


# ── inputs shared across accounts (batched) ───────────────────────────

def _urgency_rank(level: Optional[str]) -> int:
    from signal_engine.urgency import LEVELS
    return LEVELS.index(level) if level in LEVELS else -1


def _node_urgencies(node_ids: List[int]) -> Dict[int, Optional[str]]:
    from models import ContextNode
    if not node_ids:
        return {}
    return {n.node_id: (n.properties or {}).get('effective_urgency')
            for n in ContextNode.query.filter(ContextNode.node_id.in_(sorted(set(node_ids)))).all()}


def _open_interventions(customer_id: int, account_ids: List[int]) -> Dict[int, List[dict]]:
    from models import Intervention
    if not account_ids:
        return {}
    out: Dict[int, List[dict]] = {}
    rows = (Intervention.query.filter(Intervention.customer_id == int(customer_id), Intervention.account_id.in_(account_ids),
                                      Intervention.state.in_(OPEN_STATES)).order_by(Intervention.id).all())
    for r in rows:
        out.setdefault(r.account_id, []).append({
            'intervention_id': r.id, 'playbook_id': r.playbook_id, 'state': r.state, 'action_class': r.action_class,
            'urgency': r.urgency, 'pending_approval': r.state == 'proposed',
            'expected_outcome_types': list(r.expected_outcome_types or []),
            'trigger_episode_ids': list(r.trigger_episode_ids or []), 'trigger_node_ids': list(r.trigger_node_ids or []),
        })
    return out


def _latest_month(journey: dict) -> dict:
    series = (journey.get('leading_vs_trailing') or {}).get('series') or []
    return series[-1] if series else {}


def _latest_evidence_nodes(journey: dict) -> Tuple[List[str], List[int]]:
    latest = _latest_month(journey)
    by_id = {e['episode_id']: e for e in journey.get('episodes', [])}
    eids = [i for i in (latest.get('contributing_episode_ids') or []) if i in by_id]
    nids = [nid for i in eids for nid in (by_id[i].get('evidence_node_ids') or [])]
    return eids, nids


# ── the score ─────────────────────────────────────────────────────────

def _band_factor(score: Optional[float], cfg: dict) -> Tuple[str, float]:
    band = ht.classify(float(score)) if score is not None else UNKNOWN
    return band, float(cfg['band_factor'][band])


def _leading(journey: dict, cfg: dict) -> dict:
    latest = _latest_month(journey)
    label = latest.get('early_warning')
    fv = journey.get('features') or {}
    if label in cfg['leading_label_factor']:
        return {'label': label, 'basis': f'early-warning label {label}', 'factor': float(cfg['leading_label_factor'][label]),
                'month': latest.get('month'), 'qual': latest.get('qual'), 'kpi_only': latest.get('kpi_only'), 'divergence': latest.get('divergence')}
    if label == 'aligned':
        band, f = _band_factor(latest.get('kpi_only'), cfg)
        basis = f'aligned: kpi_only band {band}'
    elif label == 'leading_only':
        band, f = _band_factor(latest.get('qual'), cfg)
        basis = f'leading_only: qual band {band}'
    else:
        band, f = _band_factor(fv.get('health_now'), cfg)
        basis = f'no leading label: health band {band}' if fv.get('health_now') is not None else 'nothing scored, nothing signalled'
    return {'label': label, 'basis': basis, 'factor': f, 'month': latest.get('month'), 'qual': latest.get('qual'),
            'kpi_only': latest.get('kpi_only'), 'divergence': latest.get('divergence')}


def _urgency(journey: dict, urgencies: Dict[int, Optional[str]], cfg: dict) -> dict:
    from signal_engine.urgency import classify_structural_urgency
    by_id = {e['episode_id']: e for e in journey.get('episodes', [])}
    eids, _ = _latest_evidence_nodes(journey)
    levels = []
    for eid in eids:
        e = by_id[eid]
        for nid in e.get('evidence_node_ids') or []:
            levels.append(urgencies.get(nid) or classify_structural_urgency(e.get('role')) or 'low')
    top = max(levels, key=_urgency_rank) if levels else NONE
    return {'level': top, 'factor': float(cfg['urgency_factor'][top]), 'evidence_nodes': len(levels),
            'basis': 'highest effective_urgency on the latest month\'s cited evidence' if levels else 'no cited evidence in the latest month'}


def _renewal(journey: dict, cfg: dict) -> dict:
    from journeys.features import days_to_renewal_band
    days = (journey.get('features') or {}).get('days_to_renewal')
    band = days_to_renewal_band(days)
    return {'days': days, 'band': band, 'factor': float(cfg['renewal_factor_by_band'][band])}


def _phase(journey: dict, cfg: dict) -> dict:
    phase = journey.get('current_phase') or NONE
    known = phase in cfg['phase_factor']
    return {'phase': phase, 'factor': float(cfg['phase_factor'][phase if known else NONE]),
            'basis': journey.get('phases_basis'), **({} if known else {'note': f'phase {phase!r} is not in power_of_1.json; scored as none'})}


def _opportunity(journey: dict, open_rows: List[dict], taxonomy, cfg: dict) -> dict:
    oc = cfg['opportunity']
    latest = _latest_month(journey)
    roles = {r: n for r, n in (latest.get('roles') or {}).items() if r in oc['role_factor']}
    f = max((float(oc['role_factor'][r]) for r in roles), default=0.0)
    parts = [f'roles {sorted(roles)}'] if roles else []
    if latest.get('early_warning') == 'recovery_watch':
        f = max(f, float(oc['recovery_watch_factor']))
        parts.append('recovery_watch')
    expansion_open = [r['intervention_id'] for r in open_rows
                      if any(taxonomy.revenue_bucket(t) in ('expansion', 'pipeline') for t in r['expected_outcome_types'])]
    if expansion_open:
        f = max(f, float(oc['open_expansion_intervention_factor']))
        parts.append(f'open expansion intervention {expansion_open}')
    return {'factor': round(f, 4), 'roles': roles, 'open_expansion_interventions': expansion_open,
            'basis': '; '.join(parts) or 'no positive roles in the latest month'}


def score_account(journey: dict, account, urgencies: Dict[int, Optional[str]], open_rows: List[dict], taxonomy) -> dict:
    """The one scoring function. `journey` is the account's journey_json; `urgencies` the effective_urgency
    of its evidence nodes; `open_rows` its open interventions."""
    from mcp_server.common import get_account_arr
    cfg = settings.get('priority')
    w = cfg['weights']
    phase, leading, urgency, renewal = _phase(journey, cfg), _leading(journey, cfg), _urgency(journey, urgencies, cfg), _renewal(journey, cfg)
    risk = round(float(w['phase']) * phase['factor'] + float(w['leading']) * leading['factor']
                 + float(w['urgency']) * urgency['factor'] + float(w['renewal']) * renewal['factor'], 4)
    opp = _opportunity(journey, open_rows, taxonomy, cfg)
    lens = LENS_GROW if opp['factor'] > risk else LENS_PROTECT
    top = max(risk, opp['factor'])
    revenue = get_account_arr(account)
    eids, nids = _latest_evidence_nodes(journey)
    arc = journey.get('arc') or {}
    cited_eids = list(dict.fromkeys(eids + list(arc.get('supporting_episode_ids') or []) + [i for r in open_rows for i in r['trigger_episode_ids']]))
    by_id = {e['episode_id']: e for e in journey.get('episodes', [])}
    cited_nids = list(dict.fromkeys(nids + [n for eid in cited_eids for n in ((by_id.get(eid) or {}).get('evidence_node_ids') or [])]))
    first = by_id.get(eids[0]) if eids else None
    return {
        'account_id': account.account_id, 'account_name': account.account_name,
        'lens': lens, 'risk_factor': risk, 'opportunity_factor': opp['factor'], 'priority_factor': round(top, 4),
        'revenue': money(revenue, 'derived', ['derived: Account.revenue (get_account_arr)']),
        'revenue_weighted': money(revenue * top, 'derived',
                                  ['derived: Account.revenue', f'derived: {lens} factor {round(top, 4)} from the journey '
                                   '(phase, leading layer, cited urgency, renewal proximity | positive roles) with weights from power_of_1.json']),
        'factors': {'phase': phase, 'leading': leading, 'urgency': urgency, 'renewal': renewal, 'weights': dict(w)},
        'opportunity': opp,
        'arc_type': arc.get('arc_type'), 'state': journey.get('state'), 'as_of': journey.get('as_of'),
        'open_interventions': [{k: r[k] for k in ('intervention_id', 'playbook_id', 'state', 'action_class', 'urgency', 'pending_approval')} for r in open_rows],
        'pending_approvals': sum(1 for r in open_rows if r['pending_approval']),
        'cites': {'episode_ids': cited_eids, 'node_ids': cited_nids,
                  'quote': ((first or {}).get('meta') or {}).get('quote') or (first or {}).get('title')},
    }


def compact(row: dict) -> dict:
    return {'lens': row['lens'], 'risk_factor': row['risk_factor'], 'opportunity_factor': row['opportunity_factor'],
            'revenue_weighted': row['revenue_weighted']['value'], 'basis': row['revenue_weighted']['basis'],
            'pending_approvals': row['pending_approvals'], 'cited_episodes': len(row['cites']['episode_ids'])}


# ── batched scoring ───────────────────────────────────────────────────

def score_pairs(customer_id: int, vertical: str, pairs: List[tuple]) -> List[dict]:
    """pairs: [(journey_json, Account)] → scored rows, with one urgency query and one interventions query."""
    from utils.taxonomy_loader import get_taxonomy
    taxonomy = get_taxonomy(vertical)
    node_ids = [n for j, _ in pairs for n in _latest_evidence_nodes(j)[1]]
    urgencies = _node_urgencies(node_ids)
    open_rows = _open_interventions(customer_id, [a.account_id for _, a in pairs])
    return [score_account(j, a, urgencies, open_rows.get(a.account_id, []), taxonomy) for j, a in pairs]


def compact_for_rows(customer_id: int, vertical: str, pairs: List[tuple]) -> Dict[int, dict]:
    return {r['account_id']: compact(r) for r in score_pairs(customer_id, vertical, pairs)}


def investment_priorities(customer_id: int, account_id: Optional[int] = None) -> dict:
    from models import Account, JourneyData
    from utils.vertical_registry import get_vertical_for_customer, get_pillars
    from journeys.read import origin_block
    vertical = get_vertical_for_customer(customer_id)          # raises: no fallback vertical
    # raises for a vertical with no catalog — before any work: a tenant without journeys would otherwise return
    # 'no_journeys' for a vertical that does not exist, and get_taxonomy() silently serves the base taxonomy
    get_pillars(vertical)
    q = (JourneyData.query.filter_by(customer_id=int(customer_id))
         .join(Account, Account.account_id == JourneyData.account_id).add_entity(Account))
    if account_id is not None:
        q = q.filter(JourneyData.account_id == int(account_id))
    pairs = [(jd.journey_json or {}, a) for jd, a in q.order_by(Account.account_id).all()]
    cfg = settings.get('priority')
    out = {'customer_id': int(customer_id), 'vertical': vertical, 'account_id': int(account_id) if account_id is not None else None,
           **origin_block(customer_id), 'weights': dict(cfg['weights']), 'list_floor': float(cfg['list_floor']),
           'note': 'revenue_weighted = revenue × max(risk_factor, opportunity_factor); both factors are journey-derived with '
                   'weights from power_of_1.json. Rank, do not sum: the column is exposure-weighted revenue, not a forecast.'}
    if not pairs:
        out.update({'status': 'no_journeys', 'rows': [], 'listed': [], 'portfolio': None,
                    'hint': 'run process_data / trigger_wizard a first'})
        return out
    rows = sorted(score_pairs(customer_id, vertical, pairs), key=lambda r: (-r['revenue_weighted']['value'], r['account_id']))
    listed = [r for r in rows if r['priority_factor'] >= float(cfg['list_floor'])][:int(cfg['top_n'])]
    total = sum(r['revenue']['value'] for r in rows)
    protect = [r for r in rows if r['lens'] == LENS_PROTECT and r['priority_factor'] >= float(cfg['list_floor'])]
    grow = [r for r in rows if r['lens'] == LENS_GROW and r['priority_factor'] >= float(cfg['list_floor'])]
    out.update({
        'status': 'ok', 'rows': rows, 'listed': listed,
        'portfolio': {
            'accounts': len(rows), 'listed': len(listed),
            'revenue_total': money(total, 'derived', ['derived: sum of Account.revenue']),
            'revenue_in_protect_lens': money(sum(r['revenue']['value'] for r in protect), 'derived', ['derived: revenue of listed protect accounts']),
            'revenue_in_grow_lens': money(sum(r['revenue']['value'] for r in grow), 'derived', ['derived: revenue of listed grow accounts']),
            'exposure_weighted': money(sum(r['revenue_weighted']['value'] for r in protect), 'derived', ['derived: Σ revenue × risk_factor over listed protect accounts']),
            'opportunity_weighted': money(sum(r['revenue_weighted']['value'] for r in grow), 'derived', ['derived: Σ revenue × opportunity_factor over listed grow accounts']),
            'by_lens': {LENS_PROTECT: len(protect), LENS_GROW: len(grow)},
            'pending_approvals': sum(r['pending_approvals'] for r in rows),
        },
    })
    return out
