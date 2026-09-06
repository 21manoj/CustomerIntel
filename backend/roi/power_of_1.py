"""
Power-of-1 — what a 1-point / 1 % move in each pillar and KPI is worth, on the
tenant's own revenue base (design §3).

    power_of_1(customer_id, account_id=None)

Chain, every link labelled:
  revenue base          derived   Account.revenue via get_account_arr (the helper everything else uses)
  pillar weights        derived   the weights actually applied on the latest health row (HealthScore.pillar_weights,
                                  weight_source), else CustomerConfig.pillar_weights, else the catalog — normalised
                                  over the pillars present; never a global base
  KPI weights           derived   weight_l1 normalised over the KPIs that entered the pillar
  1 % of a KPI's value  derived   scored through the catalog curve (score_kpi) at the account's latest measurement
  $ per health point    assumed   config/economics/<vertical>.json retention_sensitivity_per_health_point
  $ at risk by band     assumed   config/economics/<vertical>.json revenue_at_risk_share_by_band
A figure inherits the weakest link (derived × assumed ⇒ assumed).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import utils.health_thresholds as ht
from roi import settings
from roi.basis import money

WEIGHT_SOURCE_HEALTH_ROW = 'health_row'
WEIGHT_SOURCE_CUSTOMER_CONFIG = 'customer_config'
WEIGHT_SOURCE_CATALOG = 'catalog'
BAND_ORDER = ('critical', 'at_risk', 'healthy')


def _normalise(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(float(v) for v in weights.values())
    if total <= 0:
        raise ValueError('pillar weights sum to zero')
    return {k: float(v) / total for k, v in weights.items()}


def _pillar_weights(account_id: int, customer_id: int, pillars: dict) -> dict:
    """(weights normalised, source, latest health row or None)."""
    from models import HealthScore, CustomerConfig
    hs = HealthScore.query.filter_by(account_id=account_id).order_by(HealthScore.measurement_month.desc()).first()
    if hs is not None and hs.pillar_weights:
        return {'weights': _normalise(hs.pillar_weights), 'source': hs.weight_source or WEIGHT_SOURCE_HEALTH_ROW,
                'basis': f'derived: weights applied on the {hs.measurement_month.isoformat()} health row (weight_source={hs.weight_source})', 'row': hs}
    cc = CustomerConfig.query.filter_by(customer_id=customer_id).first()
    if cc is not None and cc.pillar_weights:
        return {'weights': _normalise({p: w for p, w in cc.pillar_weights.items() if p in pillars}), 'source': WEIGHT_SOURCE_CUSTOMER_CONFIG,
                'basis': 'derived: CustomerConfig.pillar_weights (Wizard C / tier), no health row yet', 'row': hs}
    return {'weights': _normalise({p: d['weight_l2'] for p, d in pillars.items()}), 'source': WEIGHT_SOURCE_CATALOG,
            'basis': 'derived: catalog weight_l2, no tenant weights yet', 'row': hs}


def _next_band(band: str) -> Optional[str]:
    i = BAND_ORDER.index(band)
    return BAND_ORDER[i + 1] if i + 1 < len(BAND_ORDER) else None


def _band_boundary(band: str) -> Optional[float]:
    return {'critical': float(ht.at_risk_min()), 'at_risk': float(ht.healthy_min())}.get(band)


def _latest_measurements(account_id: int, codes: List[str]) -> Dict[str, float]:
    from models import KPIMeasurement
    if not codes:
        return {}
    out: Dict[str, float] = {}
    rows = (KPIMeasurement.query.filter(KPIMeasurement.account_id == account_id, KPIMeasurement.kpi_code.in_(codes))
            .order_by(KPIMeasurement.measured_at.desc()).all())
    for r in rows:
        out.setdefault(r.kpi_code, float(r.value))
    return out


def _one_pct_value_move(kdef: dict, value: float) -> dict:
    """Score delta for a 1 % move of the KPI's raw value in its better direction, through the catalog curve."""
    from utils.generic_scorer import score_kpi
    pct = float(settings.get('one_pct'))
    target = kdef.get('target')
    op = target.get('operator', '>') if isinstance(target, dict) else '>'
    higher = kdef.get('higher_is_better', op in ('>', '>='))
    after = value * (1 + pct) if higher else value * (1 - pct)
    now_s, after_s = score_kpi(value, kdef), score_kpi(after, kdef)
    return {'value_now': value, 'value_after': round(after, 4), 'direction': 'up' if higher else 'down',
            'score_now': round(now_s, 2), 'score_after': round(after_s, 2), 'score_delta': round(after_s - now_s, 4)}


def account_power_of_1(account, customer_id: int, vertical: str, pillars: dict, kpis: dict, econ: dict) -> dict:
    from mcp_server.common import get_account_arr
    from models import CustomerConfig
    sens = float(econ['retention_sensitivity_per_health_point']['value'])
    sens_basis = f"assumed: {econ['retention_sensitivity_per_health_point']['basis']}"
    shares = econ['revenue_at_risk_share_by_band']
    revenue = get_account_arr(account)
    pw = _pillar_weights(account.account_id, customer_id, pillars)
    hs = pw['row']
    contributing = (hs.contributing_pillars or {}) if hs is not None else {}
    health_now = float(hs.health_score) if hs is not None and hs.health_score is not None else None
    per_point = revenue * sens
    chain_point = ['derived: Account.revenue', pw['basis'], sens_basis]

    # which KPIs count: the ones that entered the latest score, else the tenant's enabled list, else the catalog
    cc = CustomerConfig.query.filter_by(customer_id=customer_id).first()
    if hs is not None and hs.kpi_weights:
        kpi_scope, kpi_scope_basis = dict(hs.kpi_weights), 'KPIs that entered the latest health row (kpi_weights)'
    elif cc is not None and cc.enabled_kpis:
        kpi_scope, kpi_scope_basis = {c: kpis[c].get('weight_l1') for c in cc.enabled_kpis if c in kpis}, 'CustomerConfig.enabled_kpis'
    else:
        kpi_scope, kpi_scope_basis = {c: d.get('weight_l1') for c, d in kpis.items()}, 'the full catalog'
    measurements = _latest_measurements(account.account_id, list(kpi_scope))

    pillar_rows, kpi_rows = [], []
    for code, w in sorted(pw['weights'].items()):
        pdef = pillars.get(code, {})
        score = contributing.get(code)
        codes_in = [c for c in kpi_scope if kpis.get(c, {}).get('pillar') == code]
        l1_total = sum(float(kpi_scope[c] or kpis[c].get('weight_l1') or 1.0) for c in codes_in) or 0.0
        pillar_rows.append({
            'pillar': code, 'name': pdef.get('name'), 'weight': round(w, 4), 'weight_source': pw['source'],
            'current_score': score,
            'health_points_per_pillar_point': round(w, 4),
            'revenue_per_pillar_point': money(per_point * w, 'assumed', chain_point + [f'derived: × pillar weight {round(w, 4)}']),
            'revenue_per_one_pct_move': money(per_point * w * score * float(settings.get('one_pct')), 'assumed',
                                              chain_point + [f'derived: 1% of current pillar score {score}']) if score is not None
            else money(None, 'assumed', chain_point, note='no pillar score yet (no KPI in this pillar has been scored)'),
            'kpis_in_scope': len(codes_in),
        })
        for c in codes_in:
            kdef = kpis[c]
            l1 = float(kpi_scope[c] or kdef.get('weight_l1') or 1.0)
            l1_norm = l1 / l1_total if l1_total else 0.0
            hp = w * l1_norm
            row = {'kpi': c, 'name': kdef.get('name'), 'pillar': code, 'unit': kdef.get('unit'), 'weight_l1': round(l1_norm, 4),
                   'health_points_per_kpi_point': round(hp, 4),
                   'revenue_per_kpi_score_point': money(per_point * hp, 'assumed', chain_point + [f'derived: × pillar weight {round(w, 4)} × KPI weight {round(l1_norm, 4)}'])}
            v = measurements.get(c)
            if v is not None:
                mv = _one_pct_value_move(kdef, v)
                mv['health_delta'] = round(mv['score_delta'] * hp, 4)
                mv['revenue_delta'] = money(mv['score_delta'] * hp * per_point, 'assumed',
                                            chain_point + [f'derived: catalog curve at value {v}: score {mv["score_now"]}→{mv["score_after"]}'],
                                            note=None if mv['score_delta'] else 'flat on the catalog curve at this value (already at the healthy max or the critical floor)')
                row['one_pct_value_move'] = mv
            else:
                row['one_pct_value_move'] = None
            kpi_rows.append(row)

    band_view = None
    if health_now is not None:
        band = ht.classify(health_now)
        nxt = _next_band(band)
        boundary = _band_boundary(band)
        band_view = {
            'health_now': health_now, 'band': band, 'measurement_month': hs.measurement_month.isoformat(),
            'revenue_at_risk': money(revenue * float(shares[band]), 'assumed', ['derived: Account.revenue', f"assumed: {shares['basis']}"]),
            'next_band': nxt, 'points_to_next_band': round(boundary - health_now, 2) if boundary is not None else None,
            'revenue_protected_if_next_band': money(revenue * (float(shares[band]) - float(shares[nxt])), 'assumed',
                                                    ['derived: Account.revenue', f"assumed: {shares['basis']}"]) if nxt else None,
            'pillar_points_to_next_band': ({p: round((boundary - health_now) / w, 2) for p, w in pw['weights'].items() if w > 0}
                                           if boundary is not None else None),
        }

    return {
        'account_id': account.account_id, 'account_name': account.account_name,
        'revenue': money(revenue, 'derived', ['derived: Account.revenue (get_account_arr)']),
        'health_now': health_now, 'weight_source': pw['source'], 'weights_basis': pw['basis'],
        'revenue_per_health_point': money(per_point, 'assumed', chain_point[:1] + [sens_basis]),
        'pillars': pillar_rows, 'kpis': kpi_rows, 'kpi_scope': kpi_scope_basis, 'measured_kpis': len(measurements),
        'band_view': band_view,
    }


def power_of_1(customer_id: int, account_id: Optional[int] = None) -> dict:
    from models import Account
    from utils.vertical_registry import get_vertical_for_customer, get_pillars, get_kpis
    from journeys.read import origin_block
    vertical = get_vertical_for_customer(customer_id)          # raises: no fallback vertical
    pillars, kpis = get_pillars(vertical), get_kpis(vertical)
    econ = settings.economics(vertical)                        # raises: no economics file
    q = Account.query.filter_by(customer_id=int(customer_id))
    if account_id is not None:
        q = q.filter_by(account_id=int(account_id))
    accounts = q.order_by(Account.account_id).all()
    sens = econ['retention_sensitivity_per_health_point']
    out = {'customer_id': int(customer_id), 'vertical': vertical, 'account_id': int(account_id) if account_id is not None else None,
           **origin_block(customer_id),
           'economics': {'file': settings.economics_path(vertical).name, 'basis': econ['basis'], 'horizon_months': econ['horizon_months'],
                         'retention_sensitivity_per_health_point': sens, 'revenue_at_risk_share_by_band': econ['revenue_at_risk_share_by_band']},
           'note': 'derived = the tenant\'s own revenue, weights and catalog; assumed = the economics file, labelled per figure. '
                   'A figure carries the weakest basis in its chain. Nothing here is a forecast.'}
    if not accounts:
        out.update({'status': 'no_accounts', 'accounts': [], 'portfolio': None})
        return out
    rows = [account_power_of_1(a, int(customer_id), vertical, pillars, kpis, econ) for a in accounts]
    total = sum(r['revenue']['value'] for r in rows)
    per_point_total = sum(r['revenue_per_health_point']['value'] for r in rows)

    # pillar aggregate: Σ over accounts (weights may differ per account — lifecycle stages), revenue-weighted score
    agg: Dict[str, dict] = {}
    for r in rows:
        rv = r['revenue']['value']
        for p in r['pillars']:
            a = agg.setdefault(p['pillar'], {'pillar': p['pillar'], 'name': p['name'], 'accounts': 0, 'revenue_per_pillar_point': 0.0,
                                             'score_weighted': 0.0, 'score_revenue': 0.0, 'weight_sources': {}})
            a['accounts'] += 1
            a['revenue_per_pillar_point'] += p['revenue_per_pillar_point']['value']
            a['weight_sources'][p['weight_source']] = a['weight_sources'].get(p['weight_source'], 0) + 1
            if p['current_score'] is not None and rv:
                a['score_weighted'] += p['current_score'] * rv
                a['score_revenue'] += rv
    pillar_rows = []
    for p in sorted(agg.values(), key=lambda x: x['pillar']):
        score = round(p['score_weighted'] / p['score_revenue'], 2) if p['score_revenue'] else None
        pillar_rows.append({
            'pillar': p['pillar'], 'name': p['name'], 'accounts': p['accounts'], 'weight_sources': p['weight_sources'],
            'current_score_revenue_weighted': score,
            'revenue_per_pillar_point': money(p['revenue_per_pillar_point'], 'assumed',
                                              ['derived: Σ over accounts of revenue × pillar weight', f"assumed: {sens['basis']}"]),
            'revenue_per_one_pct_move': money(p['revenue_per_pillar_point'] * score * float(settings.get('one_pct')), 'assumed',
                                              ['derived: Σ revenue × pillar weight × 1% of the revenue-weighted pillar score', f"assumed: {sens['basis']}"])
            if score is not None else money(None, 'assumed', note='no pillar score on any account yet'),
        })
    kagg: Dict[str, dict] = {}
    for r in rows:
        for k in r['kpis']:
            a = kagg.setdefault(k['kpi'], {'kpi': k['kpi'], 'name': k['name'], 'pillar': k['pillar'], 'unit': k['unit'], 'accounts': 0,
                                           'revenue_per_kpi_score_point': 0.0, 'measured_accounts': 0, 'one_pct_value_move_revenue': 0.0})
            a['accounts'] += 1
            a['revenue_per_kpi_score_point'] += k['revenue_per_kpi_score_point']['value']
            if k['one_pct_value_move']:
                a['measured_accounts'] += 1
                a['one_pct_value_move_revenue'] += k['one_pct_value_move']['revenue_delta']['value']
    kpi_rows = [{
        'kpi': a['kpi'], 'name': a['name'], 'pillar': a['pillar'], 'unit': a['unit'], 'accounts': a['accounts'], 'measured_accounts': a['measured_accounts'],
        'revenue_per_kpi_score_point': money(a['revenue_per_kpi_score_point'], 'assumed', ['derived: Σ revenue × pillar weight × KPI weight', f"assumed: {sens['basis']}"]),
        'one_pct_value_move_revenue': money(a['one_pct_value_move_revenue'], 'assumed',
                                            ['derived: Σ over measured accounts of the catalog-curve score delta × weights × revenue', f"assumed: {sens['basis']}"])
        if a['measured_accounts'] else money(None, 'assumed', note='no measurement on any account'),
    } for a in sorted(kagg.values(), key=lambda x: (-x['one_pct_value_move_revenue'], -x['revenue_per_kpi_score_point'], x['kpi']))]

    shares = econ['revenue_at_risk_share_by_band']
    by_band: Dict[str, dict] = {b: {'band': b, 'accounts': 0, 'revenue': 0.0, 'revenue_at_risk': 0.0} for b in BAND_ORDER}
    unscored = 0
    for r in rows:
        bv = r['band_view']
        if not bv:
            unscored += 1
            continue
        b = by_band[bv['band']]
        b['accounts'] += 1
        b['revenue'] += r['revenue']['value']
        b['revenue_at_risk'] += bv['revenue_at_risk']['value']
    band_rows = [{'band': b['band'], 'accounts': b['accounts'], 'share_at_risk': float(shares[b['band']]),
                  'revenue': money(b['revenue'], 'derived', ['derived: Σ Account.revenue in band']),
                  'revenue_at_risk': money(b['revenue_at_risk'], 'assumed', ['derived: Σ Account.revenue in band', f"assumed: {shares['basis']}"])}
                 for b in by_band.values()]

    sens_v = float(sens['value'])
    scenarios = [{
        'cs_investment_share_of_revenue': s,
        'investment': money(total * s, 'assumed', ['derived: revenue base', 'assumed: scenario share (power_of_1.json scenarios)']),
        'break_even_health_points': round(s / sens_v, 2),
        'basis': f'assumed: investment {s:.1%} of revenue pays back when portfolio health rises by {s / sens_v:.1f} points at {sens_v:.2%} of revenue per point ({sens["basis"]})',
    } for s in settings.get('scenarios', 'cs_investment_share_of_revenue')]

    out.update({
        'status': 'ok',
        'portfolio': {
            'accounts': len(rows), 'unscored_accounts': unscored,
            'revenue_base': money(total, 'derived', ['derived: Σ Account.revenue (get_account_arr)']),
            'revenue_per_health_point': money(per_point_total, 'assumed', ['derived: Σ Account.revenue', f"assumed: {sens['basis']}"]),
            'weight_sources': {src: sum(1 for r in rows if r['weight_source'] == src) for src in sorted({r['weight_source'] for r in rows})},
            'pillars': pillar_rows, 'kpis': kpi_rows, 'bands': band_rows, 'scenarios': scenarios,
        },
        'accounts': rows,
    })
    return out
