"""
Peer headroom — how much of an account's exposure the industry benchmarks
already in the graph say is realistically addressable (design §3, 2026-09-08).

    headroom_for_accounts(customer_id, vertical, account_ids)  -> {account_id: block}
    NOT_COVERED                                                 the absent block, used when there is nothing to read

The benchmarks are the EXTERNAL_CONTEXT / industry_benchmark nodes that
`utils.csv_ingest.load_benchmarks` writes from `industry_benchmarks.csv`:
p25/p50/p75/p90 per KPI plus the publishing source. Until now nothing in
roi/, journeys/ or ask_ai/ read them back.

Basis. The percentiles describe a peer population this tenant did not
measure — platform-curated reference data, the same kind of input as
config/economics/<vertical>.json. The account's own KPI value is `derived`;
the peer distribution it is placed against is `assumed`; by the weakest-link
rule (roi.basis.money) every figure that rests on both is `assumed`, and its
chain names the actual publishing source string off the node.

Position. The latest value is placed in the peer distribution by linear
interpolation between the published percentile points, and CLAMPED to the
lowest / highest published percentile rather than extrapolated past the
data — at or below p25 the honest answer is "at or below the 25th
percentile", not "at the 0th". `higher_is_better: false` KPIs are mirrored,
so `position` always means "how good, versus peers", and
headroom = 1 - position.

Discount, never a boost, and floored. addressable_weighted =
revenue_weighted × (min_multiplier + (1 - min_multiplier) × headroom). The
floor matters: benchmarks are trailing KPI data, and this platform's whole
premise is that the leading (qualitative) layer diverges from the trailing
one. Without a floor an account sitting at p90 on every KPI while its exec
sponsor walks out would be multiplied to nothing by the very layer that
cannot see the problem. The floor bounds how far peer data may discount a
journey-derived risk; it can never erase it.

Not covered is absent, not zero. A vertical with no benchmark nodes, or an
account none of whose measured KPIs are benchmarked, gets multiplier 1.0 —
revenue_weighted passes through undiscounted and says so. Absence of peer
evidence is not evidence of no headroom.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from roi import settings

NODE_TYPE = 'EXTERNAL_CONTEXT'
NODE_SUBTYPE = 'industry_benchmark'

STATUS_COVERED = 'covered'
STATUS_NOT_COVERED = 'not_covered'

NOT_COVERED_BASIS = ('no industry_benchmark node covers a KPI measured on this account; '
                     'exposure is not discounted (absence of peer evidence is not evidence of no headroom)')

NOT_COVERED: dict = {
    'status': STATUS_NOT_COVERED, 'factor': None, 'multiplier': 1.0,
    'covered_kpis': 0, 'measured_kpis': 0, 'benchmarked_kpis': 0,
    'kpis': [], 'sources': [], 'benchmark_node_ids': [], 'basis': NOT_COVERED_BASIS,
}


def _cfg(*keys):
    return settings.get('priority', 'benchmark_headroom', *keys)


def _percentile_points() -> List[Tuple[str, float]]:
    """[('p25', 0.25), …] in ascending percentile order, from config/power_of_1.json."""
    return sorted(_cfg('percentiles').items(), key=lambda kv: kv[1])


def _to_float(v) -> Optional[float]:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return f if f == f and f not in (float('inf'), float('-inf')) else None      # NaN / inf out


def kpi_higher_is_better(kdef: dict) -> bool:
    """The catalog's own direction for a KPI (explicit flag, else the target operator)."""
    target = kdef.get('target')
    op = target.get('operator', '>') if isinstance(target, dict) else '>'
    return bool(kdef.get('higher_is_better', op in ('>', '>=')))


# ── reading the nodes ─────────────────────────────────────────────────

def load_customer_benchmarks(customer_id: int) -> Dict[str, dict]:
    """{kpi_code: {points, source, node_id, unit}} from the tenant's industry_benchmark nodes.

    One query for the whole tenant: load_benchmarks() attaches every benchmark node to the
    customer's FIRST account (the node model requires an account_id) and dedups on
    source_event_id `bench_<kpi_code>`, so these are customer-scoped, one per KPI, not
    per-account. A row whose percentiles do not parse, or that carries fewer than
    min_percentile_points of them, or whose percentiles are not strictly increasing, is
    unusable data — dropped, never repaired.
    """
    from models import ContextNode
    min_points = int(_cfg('min_percentile_points'))
    names = _percentile_points()
    out: Dict[str, dict] = {}
    rows = (ContextNode.query
            .filter_by(customer_id=int(customer_id), node_type=NODE_TYPE, node_subtype=NODE_SUBTYPE)
            .order_by(ContextNode.node_id).all())
    for n in rows:
        props = n.properties or {}
        code = str(props.get('kpi_code') or '').strip()
        if not code or code in out:                                             # first node per KPI wins: deterministic
            continue
        points = [(pct, _to_float(props.get(name))) for name, pct in names]
        points = [(pct, v) for pct, v in points if v is not None]
        if len(points) < min_points:
            continue
        if any(hi <= lo for (_, lo), (_, hi) in zip(points, points[1:])):        # non-monotonic percentiles: unusable
            continue
        out[code] = {'points': points, 'source': (props.get('benchmark_source') or None),
                     'unit': (props.get('unit') or None), 'node_id': n.node_id,
                     'percentiles': {name: v for (name, _), (_, v) in zip(names, points)} if len(points) == len(names)
                     else {name: _to_float(props.get(name)) for name, _ in names}}
    return out


def latest_measurements(account_ids: List[int], codes: List[str]) -> Dict[int, Dict[str, float]]:
    """{account_id: {kpi_code: latest value}} in one query — the batched form of
    power_of_1._latest_measurements, which reads one account at a time."""
    from models import KPIMeasurement
    if not account_ids or not codes:
        return {}
    out: Dict[int, Dict[str, float]] = {}
    rows = (KPIMeasurement.query
            .filter(KPIMeasurement.account_id.in_(sorted(set(account_ids))), KPIMeasurement.kpi_code.in_(sorted(set(codes))))
            .order_by(KPIMeasurement.measured_at.desc()).all())
    for r in rows:
        out.setdefault(r.account_id, {}).setdefault(r.kpi_code, float(r.value))
    return out


# ── position and headroom ─────────────────────────────────────────────

def position(value: float, points: List[Tuple[float, float]]) -> float:
    """Where `value` sits in the peer distribution, as a percentile in [points[0].pct, points[-1].pct].
    Clamped at the published ends — nothing is claimed outside the percentiles that were published."""
    if value <= points[0][1]:
        return float(points[0][0])
    if value >= points[-1][1]:
        return float(points[-1][0])
    for (p_lo, v_lo), (p_hi, v_hi) in zip(points, points[1:]):
        if v_lo <= value <= v_hi:
            span = v_hi - v_lo
            return float(p_lo) + ((value - v_lo) / span) * (float(p_hi) - float(p_lo))
    return float(points[-1][0])                                                 # unreachable: points are monotonic


def kpi_headroom(value: float, kdef: dict, bench: dict) -> dict:
    """One KPI's peer position and headroom. headroom = 1 - "how good versus peers"."""
    raw = position(value, bench['points'])
    higher = kpi_higher_is_better(kdef)
    pos = raw if higher else 1.0 - raw
    return {'value': value, 'raw_percentile': round(raw, 4), 'position': round(pos, 4),
            'headroom': round(1.0 - pos, 4), 'higher_is_better': higher}


def account_headroom(kpis: Dict[str, dict], bench: Dict[str, dict], measurements: Dict[str, float]) -> dict:
    """One account's weighted peer headroom over the KPIs that have BOTH a measurement and a
    benchmark, weighted by the catalog's weight_l1 normalised over exactly that set."""
    covered = sorted(c for c in bench if c in kpis and c in measurements)
    if not covered:
        return {**NOT_COVERED, 'measured_kpis': len(measurements), 'benchmarked_kpis': len(bench)}
    weights = {c: float(kpis[c].get('weight_l1') or 0.0) for c in covered}
    total = sum(weights.values())
    if total <= 0:                                                              # a catalog whose covered KPIs are all weightless
        weights, total = {c: 1.0 for c in covered}, float(len(covered))
    rows, factor = [], 0.0
    for c in covered:
        b, kdef = bench[c], kpis[c]
        h = kpi_headroom(measurements[c], kdef, b)
        w = weights[c] / total
        factor += w * h['headroom']
        rows.append({'kpi': c, 'name': kdef.get('name'), 'pillar': kdef.get('pillar'),
                     'unit': kdef.get('unit') or b['unit'], 'weight': round(w, 4),
                     'percentiles': b['percentiles'], 'benchmark_source': b['source'],
                     'benchmark_node_id': b['node_id'], **h})
    factor = round(min(max(factor, 0.0), 1.0), 4)
    min_mult = float(_cfg('min_multiplier'))
    sources = sorted({r['benchmark_source'] for r in rows if r['benchmark_source']})
    return {
        'status': STATUS_COVERED, 'factor': factor,
        'multiplier': round(min_mult + (1.0 - min_mult) * factor, 4),
        'covered_kpis': len(covered), 'measured_kpis': len(measurements), 'benchmarked_kpis': len(bench),
        'kpis': rows, 'sources': sources, 'benchmark_node_ids': [r['benchmark_node_id'] for r in rows],
        'basis': (f'weighted peer headroom over {len(covered)} benchmarked KPI(s) '
                  f'({", ".join(covered)}); percentiles from ' + (', '.join(sources) if sources else 'industry_benchmarks.csv')),
    }


def headroom_for_accounts(customer_id: int, vertical: str, account_ids: List[int]) -> Dict[int, dict]:
    """{account_id: headroom block} — two queries for the whole tenant, whatever the account count.
    Every account gets a block; an account with no covered KPI gets the not_covered one."""
    from utils.vertical_registry import get_kpis
    try:
        kpis = get_kpis(vertical)
    except ValueError as exc:
        # A vertical with no catalog: nothing to weight or direct the KPIs by, so no headroom is
        # claimed. Reported as not_covered, never borrowed from another vertical — and NOT raised
        # here, because the callers that must fail closed already do so before this runs
        # (investment_priorities calls get_pillars first; list_journeys' behaviour is unchanged).
        return {int(a): {**NOT_COVERED, 'basis': f'no KPI catalog for vertical {vertical!r} ({exc}); '
                                                 'no peer headroom claimed, exposure is not discounted'} for a in account_ids}
    bench = load_customer_benchmarks(customer_id)
    if not bench:
        return {int(a): {**NOT_COVERED, 'basis': 'this tenant has no industry_benchmark node (industry_benchmarks.csv was never loaded); '
                                                 'exposure is not discounted'} for a in account_ids}
    codes = [c for c in bench if c in kpis]
    measured = latest_measurements(list(account_ids), codes)
    return {int(a): account_headroom(kpis, bench, measured.get(int(a), {})) for a in account_ids}


def money_chain_link(block: dict) -> str:
    """The basis-chain link one headroom block contributes, at its own (assumed) label."""
    return f'assumed: peer headroom ×{block["multiplier"]} — {block["basis"]}'
