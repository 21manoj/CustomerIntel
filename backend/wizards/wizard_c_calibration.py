"""
Wizard C — weight calibration from logged outcomes (docs/design/wizard-c-calibration.md).

    propose(customer_id)                       outcomes → labels; KPI scores before them → effects → a proposal row
    get_calibration(customer_id, proposal_id)  the read: one proposal in full (evidence, impact) + the list
    approve(customer_id, proposal_id, note)    human approval → CustomerConfig (weights_origin='wizard_c') → recompute
    reject(customer_id, proposal_id, note)     human rejection

Rules kept: labels come from OUTCOME nodes' revenue buckets, never from
HealthScore (that is a rollup of the same KPIs — circular); every effect
carries its counts and a confidence tier, and a gate that cannot open on
the tenant's data says so (`insufficient_outcomes` with the counts, no
proposal); the before/after is computed and stored, never written to
health rows until a person approves; every transition is a tool_audit_log
row with the key; nothing here fires from process_data; nothing here
carries its own number (config/wizard_c.json).
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

AUDIT_SURFACE = 'wizard_c'
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config', 'wizard_c.json')
_REQUIRED_KEYS = ('version', 'method_version', 'label_buckets', 'feature_window_days', 'gate', 'confidence', 'adjustment',
                  'recompute_mode', 'states', 'impact_band_decimals')
_TIER_ORDER = ('none', 'low', 'medium', 'high')


class WizardCConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def config() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    missing = [k for k in _REQUIRED_KEYS if k not in cfg]
    if missing:
        raise WizardCConfigError(f'config/wizard_c.json is missing {missing}')
    for section, keys in (('gate', ('min_outcomes_total', 'min_outcomes_per_class', 'min_accounts_with_outcomes')),
                          ('confidence', ('min_samples_per_side', 'tiers', 'flat_effect_pts')),
                          ('adjustment', ('adjust_from_confidence', 'gain', 'd_cap', 'min_pillar_weight',
                                          'max_kpi_weight_within_pillar', 'kpi_cap_min_kpis', 'weight_decimals')),
                          ('label_buckets', ('negative', 'positive'))):
        missing = [k for k in keys if k not in cfg[section]]
        if missing:
            raise WizardCConfigError(f'config/wizard_c.json: {section} is missing {missing}')
    if cfg['adjustment']['adjust_from_confidence'] not in _TIER_ORDER[1:]:
        raise WizardCConfigError(f"config/wizard_c.json: adjust_from_confidence must be one of {_TIER_ORDER[1:]}")
    if set(cfg['confidence']['tiers']) != set(_TIER_ORDER[1:]):
        raise WizardCConfigError(f'config/wizard_c.json: confidence.tiers must define exactly {_TIER_ORDER[1:]}')
    if cfg['recompute_mode'] not in ('auto', 'full_recalc'):
        raise WizardCConfigError("config/wizard_c.json: recompute_mode must be 'auto' or 'full_recalc'")
    return cfg


def reset_cache() -> None:
    config.cache_clear()


# ── who / audit (the governance layer's pattern) ──────────────────────

def current_actor() -> dict:
    from playbooks.governance import current_actor as _actor
    return _actor()


def _audit(customer_id: int, transition: str, actor: dict, detail: str) -> None:
    from mcp_server import audit
    audit.record(AUDIT_SURFACE, f'calibration.{transition}', customer_id, key_kind=actor['key_kind'],
                 key_record=actor.get('key_record'), outcome='allowed', detail=detail)


def _note(row, actor: dict, transition: str, note: Optional[str]) -> None:
    notes = list(row.notes or [])
    notes.append({'at': datetime.utcnow().isoformat(), 'by': actor['label'], 'transition': transition,
                  'note': (note or '').strip() or None})
    row.notes = notes


# ── current weights ───────────────────────────────────────────────────

def _round_to_one(weights: Dict[str, float], decimals: int) -> Dict[str, float]:
    """Normalise to 1.0, round, and let the last key absorb the rounding residue."""
    total = sum(weights.values())
    if total <= 0 or not weights:
        return {k: round(1.0 / len(weights), decimals) for k in weights} if weights else {}
    out = {k: round(v / total, decimals) for k, v in weights.items()}
    keys = list(out)
    out[keys[-1]] = round(out[keys[-1]] + (1.0 - sum(out.values())), decimals)
    return out


def current_weights(customer_id: int, vertical: str) -> dict:
    """The weights the scorer applies today for this tenant: pillar weights over the pillars it scores
    (CustomerConfig.pillar_weights, else every catalog pillar's weight_l2) and KPI weights within each
    pillar over the KPIs it scores (CustomerConfig.kpi_weights, else the catalog's weight_l1),
    each normalised within its group — the scorer normalises by the group total, so this is
    ratio-preserving. Also says where they came from."""
    from models import CustomerConfig
    from utils.vertical_registry import get_kpis, get_pillars
    from utils.vertical_health import flatten_kpi_weights
    cfg = config()
    dec = int(cfg['adjustment']['weight_decimals'])
    kpis, pillars = get_kpis(vertical), get_pillars(vertical)
    cc = CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()
    if cc and cc.pillar_weights:
        pw = {p: float(w) for p, w in cc.pillar_weights.items() if p in pillars}
        origin = cc.weights_origin or 'customer_config'
    else:
        pw = {p: float(d.get('weight_l2', 0) or 0) for p, d in pillars.items()}
        origin = 'catalog'
    enabled = set(cc.enabled_kpis) if cc and cc.enabled_kpis else set(kpis)
    flat_cc = flatten_kpi_weights(cc.kpi_weights) if cc and cc.kpi_weights else {}
    kw: Dict[str, Dict[str, float]] = {}
    for code, d in kpis.items():
        p = d.get('pillar')
        if code not in enabled or p not in pw:
            continue
        w = flat_cc.get(code, d.get('weight_l1', 0) or 0)
        kw.setdefault(p, {})[code] = float(w) if w and w > 0 else 1.0
    return {
        'pillar_weights': _round_to_one(pw, dec),
        'kpi_weights': {p: _round_to_one(ws, dec) for p, ws in kw.items()},
        'origin': origin, 'config_version': cc.config_version if cc else None,
        'lifecycle_enabled': bool(cc and cc.lifecycle_stage_weights and cc.lifecycle_stage_weights.get('enabled')),
    }


# ── samples: outcomes → labels ────────────────────────────────────────

def _outcome_samples(customer_id: int, vertical: str) -> Tuple[List[dict], dict]:
    from models import ContextNode
    from utils.taxonomy_loader import get_taxonomy
    cfg = config()
    tax = get_taxonomy(vertical)
    label_of = {}
    for b in cfg['label_buckets']['negative']:
        label_of[b] = 'negative'
    for b in cfg['label_buckets']['positive']:
        label_of[b] = 'positive'
    unknown = [b for b in label_of if b not in tax.revenue_bucket_map]
    if unknown:
        raise WizardCConfigError(f'config/wizard_c.json names revenue buckets the {vertical} taxonomy does not define: {unknown}')
    rows = ContextNode.query.filter_by(customer_id=int(customer_id), node_type='OUTCOME', source='observed') \
        .order_by(ContextNode.occurred_at, ContextNode.node_id).all()
    samples, counts = [], {'total': 0, 'positive': 0, 'negative': 0, 'unbucketed': 0, 'rejected': 0, 'by_bucket': {}}
    for n in rows:
        p = n.properties or {}
        if (p.get('review') or {}).get('status') == 'rejected':
            counts['rejected'] += 1
            continue
        bucket = tax.revenue_bucket(n.node_subtype)
        label = label_of.get(bucket)
        if not bucket or not label or not n.occurred_at:
            counts['unbucketed'] += 1
            continue
        counts['total'] += 1
        counts[label] += 1
        counts['by_bucket'][bucket] = counts['by_bucket'].get(bucket, 0) + 1
        samples.append({'node_id': n.node_id, 'account_id': n.account_id, 'occurred_at': n.occurred_at,
                        'subtype': n.node_subtype, 'bucket': bucket, 'label': label,
                        'revenue': float(n.revenue_impact) if n.revenue_impact is not None else None})
    counts['accounts'] = len({s['account_id'] for s in samples})
    return samples, counts


def _gate(counts: dict) -> Optional[dict]:
    g = config()['gate']
    short = []
    if counts['total'] < g['min_outcomes_total']:
        short.append(f"{counts['total']} labelled outcomes < min_outcomes_total {g['min_outcomes_total']}")
    for cls in ('positive', 'negative'):
        if counts[cls] < g['min_outcomes_per_class']:
            short.append(f"{counts[cls]} {cls} outcomes < min_outcomes_per_class {g['min_outcomes_per_class']}")
    if counts['accounts'] < g['min_accounts_with_outcomes']:
        short.append(f"{counts['accounts']} accounts with an outcome < min_accounts_with_outcomes {g['min_accounts_with_outcomes']}")
    return {'gate': dict(g), 'short': short} if short else None


# ── features: KPI scores in the window before each outcome ────────────

def _features(samples: List[dict], kpis: dict, kpi_weights: Dict[str, Dict[str, float]]) -> int:
    """Attach to every sample its KPI scores (0-100, catalog-scored means over the window) and pillar
    scores (weight_l1-weighted, the scorer's L2). Returns the number of samples with no KPI row in the window."""
    from models import KPIMeasurement
    from utils.generic_scorer import score_kpi
    window = timedelta(days=int(config()['feature_window_days']))
    acct_ids = sorted({s['account_id'] for s in samples})
    rows_by_acct: Dict[int, list] = defaultdict(list)
    if acct_ids:
        for r in KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(acct_ids)).all():
            if r.kpi_code in kpis and r.measured_at:
                rows_by_acct[r.account_id].append(r)
    flat_w = {code: w for ws in kpi_weights.values() for code, w in ws.items()}
    unfeatured = 0
    for s in samples:
        lo, hi = s['occurred_at'] - window, s['occurred_at']
        vals: Dict[str, list] = defaultdict(list)
        for r in rows_by_acct.get(s['account_id'], []):
            if lo <= r.measured_at <= hi:
                vals[r.kpi_code].append(float(r.value))
        kpi_scores = {code: round(score_kpi(sum(v) / len(v), kpis[code]), 4) for code, v in vals.items()}
        by_pillar: Dict[str, list] = defaultdict(list)
        for code, sc in kpi_scores.items():
            by_pillar[kpis[code]['pillar']].append((sc, flat_w.get(code, 1.0)))
        pillar_scores = {p: round(sum(sc * w for sc, w in xs) / (sum(w for _, w in xs) or 1.0), 4) for p, xs in by_pillar.items()}
        s['kpi_scores'], s['pillar_scores'] = kpi_scores, pillar_scores
        if not kpi_scores:
            unfeatured += 1
    return unfeatured


# ── effects ───────────────────────────────────────────────────────────

def _pooled_sd(a: List[float], b: List[float]) -> float:
    va = statistics.variance(a) if len(a) > 1 else None
    vb = statistics.variance(b) if len(b) > 1 else None
    if va is None and vb is None:
        return 0.0
    if va is None:
        return math.sqrt(vb)
    if vb is None:
        return math.sqrt(va)
    return math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))


def _tier(d: Optional[float], n_pos: int, n_neg: int) -> str:
    c = config()['confidence']
    if d is None or n_pos < c['min_samples_per_side'] or n_neg < c['min_samples_per_side']:
        return 'none'
    ad = abs(d)
    for tier in ('high', 'medium', 'low'):
        if ad >= float(c['tiers'][tier]):
            return tier
    return 'none'


def _effect(pos: List[Tuple[int, float]], neg: List[Tuple[int, float]]) -> dict:
    """pos/neg: (account_id, score) per sample where the feature exists."""
    c = config()['confidence']
    ps, ns = [x for _, x in pos], [x for _, x in neg]
    out = {'n_pos': len(ps), 'n_neg': len(ns), 'accounts_pos': len({a for a, _ in pos}), 'accounts_neg': len({a for a, _ in neg}),
           'mean_pos': round(statistics.mean(ps), 2) if ps else None, 'mean_neg': round(statistics.mean(ns), 2) if ns else None,
           'effect_pts': None, 'd': None, 'direction': 'no_data', 'confidence': 'none'}
    if not ps or not ns:
        return out
    effect = statistics.mean(ps) - statistics.mean(ns)
    sd = _pooled_sd(ps, ns)
    cap = float(config()['adjustment']['d_cap'])
    if sd > 0:
        d = effect / sd
    else:   # perfectly separated (or constant): a clear effect is at least the cap, no effect is 0
        d = math.copysign(cap * 2, effect) if abs(effect) >= float(c['flat_effect_pts']) else 0.0
    out.update({'effect_pts': round(effect, 2), 'sd': round(sd, 2), 'd': round(d, 3)})
    if abs(effect) < float(c['flat_effect_pts']):
        out['direction'] = 'flat'
    else:
        out['direction'] = 'discriminates' if effect > 0 else 'inverse'
    out['confidence'] = _tier(d, len(ps), len(ns))
    return out


def _evidence(samples: List[dict], kpis: dict, pillar_codes: List[str]) -> dict:
    kpi_ev, pillar_ev = {}, {}
    for code in sorted(k for k in kpis if kpis[k].get('pillar') in pillar_codes):
        pos = [(s['account_id'], s['kpi_scores'][code]) for s in samples if s['label'] == 'positive' and code in s['kpi_scores']]
        neg = [(s['account_id'], s['kpi_scores'][code]) for s in samples if s['label'] == 'negative' and code in s['kpi_scores']]
        if not pos and not neg:
            continue
        kpi_ev[code] = {'pillar': kpis[code]['pillar'], 'name': kpis[code].get('name'), **_effect(pos, neg)}
    for p in pillar_codes:
        pos = [(s['account_id'], s['pillar_scores'][p]) for s in samples if s['label'] == 'positive' and p in s['pillar_scores']]
        neg = [(s['account_id'], s['pillar_scores'][p]) for s in samples if s['label'] == 'negative' and p in s['pillar_scores']]
        pillar_ev[p] = _effect(pos, neg)
    return {'pillars': pillar_ev, 'kpis': kpi_ev}


# ── the proposal ──────────────────────────────────────────────────────

def _adjusts(ev: dict) -> bool:
    return _TIER_ORDER.index(ev['confidence']) >= _TIER_ORDER.index(config()['adjustment']['adjust_from_confidence'])


def _factor(ev: dict) -> float:
    a = config()['adjustment']
    if not _adjusts(ev) or ev.get('d') is None:
        return 1.0
    d = max(-float(a['d_cap']), min(float(a['d_cap']), float(ev['d'])))
    return round(1.0 + float(a['gain']) * d, 4)


def _propose_weights(current: dict, evidence: dict) -> Tuple[dict, dict, int]:
    """Returns (pillar_weights, kpi_weights, adjusted_count). Keys without a confident effect keep their weight."""
    a = config()['adjustment']
    dec = int(a['weight_decimals'])
    adjusted = 0
    pw = {}
    for p, w in current['pillar_weights'].items():
        f = _factor(evidence['pillars'].get(p, {'confidence': 'none'}))
        adjusted += int(f != 1.0)
        evidence['pillars'].setdefault(p, {})['factor'] = f
        pw[p] = max(float(a['min_pillar_weight']), w * f)
    pw = _round_to_one(pw, dec)
    kw = {}
    cap = float(a['max_kpi_weight_within_pillar'])
    for p, ws in current['kpi_weights'].items():
        raw = {}
        for code, w in ws.items():
            f = _factor(evidence['kpis'].get(code, {'confidence': 'none'}))
            adjusted += int(f != 1.0)
            if code in evidence['kpis']:
                evidence['kpis'][code]['factor'] = f
            raw[code] = w * f
        norm = _round_to_one(raw, dec)
        if len(norm) >= int(a['kpi_cap_min_kpis']):
            excess = sum(w - cap for w in norm.values() if w > cap)
            if excess > 0:
                under = [c for c, w in norm.items() if w <= cap]
                for c in norm:
                    if norm[c] > cap:
                        norm[c] = cap
                for c in under:
                    norm[c] += excess / len(under)
                norm = _round_to_one(norm, dec)
        kw[p] = norm
    for p, ws in current['pillar_weights'].items():
        evidence['pillars'][p].update({'current_weight': ws, 'proposed_weight': pw[p]})
    for p, ws in current['kpi_weights'].items():
        for code, w in ws.items():
            if code in evidence['kpis']:
                evidence['kpis'][code].update({'current_weight': w, 'proposed_weight': kw[p][code]})
    return pw, kw, adjusted


# ── impact: before / after on every account's latest scored month ─────

def _impact(customer_id: int, vertical: str, current: dict, proposed_pw: dict, proposed_kw: dict) -> dict:
    """Side by side, never written. `before` is the stored latest health row (what the tenant sees);
    `after` rescores that month's KPI inputs with the proposed weights; `before_recomputed` rescores them
    with the current weights so a mismatch with the stored row (a lifecycle profile, a catalog change since)
    is visible rather than silently folded in."""
    import utils.health_thresholds as ht
    from models import Account, HealthScore, KPIMeasurement
    from utils.vertical_registry import get_kpis, get_pillars
    from utils.generic_scorer import score_account_health_explained
    dec = int(config()['impact_band_decimals'])
    kpis, pillars = get_kpis(vertical), get_pillars(vertical)
    cur_flat = {c: w for ws in current['kpi_weights'].values() for c, w in ws.items()}
    new_flat = {c: w for ws in proposed_kw.values() for c, w in ws.items()}
    accounts = Account.query.filter_by(customer_id=int(customer_id)).order_by(Account.account_id).all()
    per_account, deltas, band_changes, mismatches, unscored = [], [], 0, 0, 0
    for a in accounts:
        latest = HealthScore.query.filter_by(account_id=a.account_id).order_by(HealthScore.measurement_month.desc()).first()
        if not latest or latest.health_score is None:
            unscored += 1
            continue
        month = latest.measurement_month
        start = datetime(month.year, month.month, 1)
        end = datetime(month.year + (month.month == 12), month.month % 12 + 1, 1)
        vals: Dict[str, list] = defaultdict(list)
        for r in KPIMeasurement.query.filter(KPIMeasurement.account_id == a.account_id, KPIMeasurement.measured_at >= start,
                                             KPIMeasurement.measured_at < end).all():
            vals[r.kpi_code].append(float(r.value))
        kpi_vals = {c: sum(v) / len(v) for c, v in vals.items()}
        before_re = score_account_health_explained(kpi_vals, kpis, pillars, current['pillar_weights'], set(current['pillar_weights']), cur_flat)
        after = score_account_health_explained(kpi_vals, kpis, pillars, proposed_pw, set(proposed_pw), new_flat)
        before, after_h = float(latest.health_score), round(after['health'], dec)
        band_b, band_a = ht.classify(before), ht.classify(after_h)
        matches = abs(round(before_re['health'], dec) - before) <= 10 ** -dec
        mismatches += int(not matches)
        band_changes += int(band_b != band_a)
        deltas.append(after_h - before)
        per_account.append({'account_id': a.account_id, 'account_name': a.account_name, 'month': month.isoformat(),
                            'before': before, 'before_recomputed': round(before_re['health'], dec), 'stored_matches_recompute': matches,
                            'after': after_h, 'delta': round(after_h - before, dec), 'band_before': band_b, 'band_after': band_a,
                            'pillars_before': {k: round(v, dec) for k, v in before_re['pillars'].items()},
                            'pillars_after': {k: round(v, dec) for k, v in after['pillars'].items()},
                            'revenue': float(a.revenue) if a.revenue is not None else None})
    return {'accounts': per_account, 'summary': {
        'accounts_scored': len(per_account), 'accounts_unscored': unscored,
        'mean_delta': round(statistics.mean(deltas), dec) if deltas else None,
        'max_abs_delta': round(max(abs(d) for d in deltas), dec) if deltas else None,
        'band_changes': band_changes, 'stored_vs_recompute_mismatches': mismatches,
        'note': 'computed side by side from the latest scored month of each account; nothing is written until approval',
    }}


def propose(customer_id: int, actor: Optional[dict] = None) -> dict:
    """Explicit trigger only (trigger_wizard 'c' / POST /api/calibrations/propose) — never from process_data."""
    from extensions import db
    from models import Customer, WeightCalibration
    from utils.vertical_registry import get_vertical_for_customer, get_kpis, get_catalog_version
    actor = actor or current_actor()
    cfg = config()
    cust = db.session.get(Customer, int(customer_id))
    if not cust:
        raise ValueError(f'customer {customer_id} not found')
    vertical = get_vertical_for_customer(int(customer_id))     # fails closed on an unset vertical
    kpis = get_kpis(vertical)                                   # fails closed on an unknown vertical
    samples, counts = _outcome_samples(customer_id, vertical)
    base = {'customer_id': int(customer_id), 'vertical': vertical, 'method_version': cfg['method_version'],
            'outcome_counts': counts, 'labels': cfg['label_buckets'], 'feature_window_days': cfg['feature_window_days'],
            'data_origin': cust.data_origin}
    gate = _gate(counts)
    if gate:
        logger.info('wizard_c customer=%s insufficient_outcomes %s', customer_id, gate['short'])
        return {**base, 'status': 'insufficient_outcomes', 'gate': gate['gate'], 'short_by': gate['short'], 'proposal_id': None,
                'note': 'no proposal: log outcomes (log_outcome / outcomes.csv) until the gate opens; the labels are outcomes, never health scores'}
    current = current_weights(customer_id, vertical)
    counts['unfeatured'] = _features(samples, kpis, current['kpi_weights'])
    evidence = _evidence(samples, kpis, list(current['pillar_weights']))
    proposed_pw, proposed_kw, adjusted = _propose_weights(current, evidence)
    base['current'] = {'pillar_weights': current['pillar_weights'], 'kpi_weights': current['kpi_weights'], 'origin': current['origin']}
    if adjusted == 0:
        logger.info('wizard_c customer=%s no_confident_effect (%d samples, %d unfeatured)', customer_id, len(samples), counts['unfeatured'])
        return {**base, 'status': 'no_confident_effect', 'evidence': evidence, 'proposal_id': None,
                'note': f"{counts['total']} labelled outcomes but no KPI or pillar reached confidence "
                        f"'{cfg['adjustment']['adjust_from_confidence']}' ({counts['unfeatured']} outcomes had no KPI row in the "
                        f"{cfg['feature_window_days']}-day window before them); nothing to approve"}
    impact = _impact(customer_id, vertical, current, proposed_pw, proposed_kw)
    now = datetime.utcnow()
    open_rows = WeightCalibration.query.filter_by(customer_id=int(customer_id), state='proposed').all()
    row = WeightCalibration(
        customer_id=int(customer_id), vertical=vertical, state='proposed', method_version=cfg['method_version'],
        catalog_version=get_catalog_version(vertical), config_snapshot={k: cfg[k] for k in ('feature_window_days', 'gate', 'confidence', 'adjustment', 'label_buckets')},
        outcome_counts=counts, outcome_node_ids=[s['node_id'] for s in samples],
        current_pillar_weights=current['pillar_weights'], current_kpi_weights=current['kpi_weights'],
        proposed_pillar_weights=proposed_pw, proposed_kpi_weights=proposed_kw, evidence=evidence, impact=impact,
        proposed_at=now, proposed_by=actor['label'], proposed_by_key_id=actor.get('key_id'), notes=[],
    )
    db.session.add(row)
    db.session.flush()
    for old in open_rows:
        old.state, old.superseded_by, old.decided_at = 'superseded', row.id, now
        _note(old, actor, 'superseded', f'superseded by proposal #{row.id}')
    db.session.commit()
    for old in open_rows:
        _audit(customer_id, 'superseded', actor, f'#{old.id} superseded by #{row.id}')
    _audit(customer_id, 'propose', actor,
           f"#{row.id} {vertical} outcomes={counts['total']} (+{counts['positive']}/-{counts['negative']}, {counts['accounts']} accounts) "
           f"adjusted={adjusted} mean_delta={impact['summary']['mean_delta']} by {actor['label']}")
    logger.info('wizard_c customer=%s proposal #%s: %d adjustments from %d outcomes', customer_id, row.id, adjusted, counts['total'])
    return {**base, 'status': 'proposed', 'proposal_id': row.id, 'adjusted': adjusted, 'superseded': [o.id for o in open_rows],
            **row_view(row)}


# ── read ──────────────────────────────────────────────────────────────

def row_view(row) -> dict:
    iso = lambda x: x.isoformat() if x else None
    return {
        'proposal_id': row.id, 'customer_id': row.customer_id, 'vertical': row.vertical, 'state': row.state,
        'method_version': row.method_version, 'catalog_version': row.catalog_version, 'config_snapshot': row.config_snapshot,
        'outcome_counts': row.outcome_counts, 'outcome_node_ids': row.outcome_node_ids,
        'current': {'pillar_weights': row.current_pillar_weights, 'kpi_weights': row.current_kpi_weights},
        'proposed': {'pillar_weights': row.proposed_pillar_weights, 'kpi_weights': row.proposed_kpi_weights},
        'evidence': row.evidence, 'impact': row.impact,
        'proposed_at': iso(row.proposed_at), 'proposed_by': row.proposed_by, 'proposed_by_key_id': row.proposed_by_key_id,
        'decided_at': iso(row.decided_at), 'decided_by': row.decided_by, 'decided_by_key_id': row.decided_by_key_id,
        'decision_note': row.decision_note, 'applied_config_version': row.applied_config_version, 'recompute': row.recompute,
        'superseded_by': row.superseded_by, 'notes': row.notes or [],
    }


def _get(customer_id: int, proposal_id: int):
    from extensions import db
    from models import WeightCalibration
    row = db.session.get(WeightCalibration, int(proposal_id))
    if not row or int(row.customer_id) != int(customer_id):
        raise ValueError(f'calibration proposal {proposal_id} not found for customer {customer_id}')
    return row


def get_calibration(customer_id: int, proposal_id: Optional[int] = None) -> dict:
    """One proposal in full (the given one, else the latest) plus the list of every proposal and the weights in force."""
    from models import WeightCalibration
    from utils.vertical_registry import get_vertical_for_customer
    rows = WeightCalibration.query.filter_by(customer_id=int(customer_id)).order_by(WeightCalibration.id.desc()).all()
    row = _get(customer_id, proposal_id) if proposal_id is not None else (rows[0] if rows else None)
    vertical = get_vertical_for_customer(int(customer_id))
    return {
        'customer_id': int(customer_id), 'vertical': vertical, 'in_force': current_weights(customer_id, vertical),
        'proposal': row_view(row) if row else None,
        'proposals': [{'proposal_id': r.id, 'state': r.state, 'proposed_at': r.proposed_at.isoformat(), 'proposed_by': r.proposed_by,
                       'decided_at': r.decided_at.isoformat() if r.decided_at else None, 'decided_by': r.decided_by,
                       'outcomes': (r.outcome_counts or {}).get('total')} for r in rows],
        'count': len(rows), 'gate': config()['gate'],
    }


# ── decide ────────────────────────────────────────────────────────────

def _bump_version(v: Optional[str]) -> str:
    try:
        major, minor = str(v or '1.0').split('.')[:2]
        return f'{int(major)}.{int(minor) + 1}'
    except (ValueError, TypeError):
        return f'{v}.1'


def approve(customer_id: int, proposal_id: int, note: Optional[str] = None, actor: Optional[dict] = None) -> dict:
    """proposed → approved: the weights go into CustomerConfig (customized_by='wizard_c:<id>', config_version
    bumped, weights_origin='wizard_c'), then the existing pipeline recomputes health (mode from config) so the
    rows carry weight_source='wizard_c'. A recompute failure is recorded on the row, not hidden — the approval
    happened; the rows still show the previous weights and say so."""
    from extensions import db
    from models import CustomerConfig, HealthScore, Account
    actor = actor or current_actor()
    row = _get(customer_id, proposal_id)
    if row.state != 'proposed':
        raise ValueError(f'calibration proposal {row.id} is {row.state}; only a proposed one can be approved')
    cc = CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()
    if not cc:
        raise ValueError(f'customer {customer_id} has no CustomerConfig row')
    now = datetime.utcnow()
    cc.pillar_weights = dict(row.proposed_pillar_weights)
    cc.kpi_weights = {p: dict(ws) for p, ws in (row.proposed_kpi_weights or {}).items()}
    cc.customized_by = f'wizard_c:{row.id}'
    cc.config_version = _bump_version(cc.config_version)
    cc.weights_origin = 'wizard_c'
    row.state, row.decided_at, row.decided_by, row.decided_by_key_id = 'approved', now, actor['label'], actor.get('key_id')
    row.decision_note = (note or '').strip() or None
    row.applied_config_version = cc.config_version
    _note(row, actor, 'approve', note)
    db.session.commit()
    _audit(customer_id, 'approve', actor, f'#{row.id} by {actor["label"]} → config_version {cc.config_version}, weights_origin=wizard_c')

    mode = config()['recompute_mode']
    rec = {'mode': mode, 'status': None, 'run_id': None, 'steps': None, 'error': None, 'health_rows_wizard_c': 0}
    try:
        from mcp_server.cs_pulse_onboarding import _process_data_impl
        res = _process_data_impl(int(customer_id), mode=mode)
        rec.update({'status': res.get('status'), 'run_id': res.get('run_id'), 'steps': res.get('steps_completed')})
    except Exception as e:
        logger.warning('wizard_c: health recompute after approval of #%s failed: %s', row.id, e)
        db.session.rollback()
        rec.update({'status': 'failed', 'error': str(e)[:300]})
    acct_ids = [a.account_id for a in Account.query.filter_by(customer_id=int(customer_id)).all()]
    if acct_ids:
        rec['health_rows_wizard_c'] = HealthScore.query.filter(HealthScore.account_id.in_(acct_ids),
                                                               HealthScore.weight_source == 'wizard_c').count()
    row = _get(customer_id, proposal_id)
    row.recompute = rec
    db.session.commit()
    _audit(customer_id, 'recompute', actor, f"#{row.id} {mode} → {rec['status']} run={rec['run_id']} wizard_c_rows={rec['health_rows_wizard_c']}"
                                             + (f" error={rec['error'][:80]}" if rec.get('error') else ''))
    return row_view(row)


def reject(customer_id: int, proposal_id: int, note: Optional[str] = None, actor: Optional[dict] = None) -> dict:
    from extensions import db
    actor = actor or current_actor()
    row = _get(customer_id, proposal_id)
    if row.state != 'proposed':
        raise ValueError(f'calibration proposal {row.id} is {row.state}; only a proposed one can be rejected')
    row.state, row.decided_at, row.decided_by, row.decided_by_key_id = 'rejected', datetime.utcnow(), actor['label'], actor.get('key_id')
    row.decision_note = (note or '').strip() or None
    _note(row, actor, 'reject', note)
    db.session.commit()
    _audit(customer_id, 'reject', actor, f'#{row.id} by {actor["label"]}' + (f': {row.decision_note[:120]}' if row.decision_note else ''))
    return row_view(row)
