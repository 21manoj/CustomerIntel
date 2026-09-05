#!/usr/bin/env python3
"""
Generic Health Scorer — Vertical-Agnostic

Scores KPI values against ANY JSON catalog definition. Zero dependency on
any specific vertical (dc2_s, saas_premium, etc.). If you delete every
vertical directory, this module still works.

Usage:
    from utils.generic_scorer import score_kpi, score_account_health

    # Score a single KPI
    score = score_kpi(value=85.0, kpi_def={"target": {"value": 90}, "ranges": {...}, "higher_is_better": True})

    # Score full account health from KPI values + catalog
    health, pillars = score_account_health(
        kpi_values={"P1-KPI1": 12, "P3-KPI1": 85},
        kpi_catalog={"P1-KPI1": {...}, "P3-KPI1": {...}},
        pillar_catalog={"P1": {"weight_l2": 0.15}, "P3": {"weight_l2": 0.25}},
        pillar_weight_overrides={"P1": 0.20},  # optional CustomerConfig overrides
    )

Design principle: DC2_S is not special. This scorer reads from dicts/JSON —
it doesn't know or care which vertical produced them.
"""

import utils.health_thresholds as ht


def score_kpi(value: float, kpi_def: dict) -> float:
    """
    Score a single KPI value using 4-band interpolation.

    Bands (higher_is_better):
      critical.min  → 0
      risk boundary → at_risk_min (50)
      target        → healthy_min (70)
      healthy.max   → 100

    Args:
        value: The actual KPI measurement
        kpi_def: KPI definition dict with 'target', 'ranges', 'higher_is_better'

    Returns:
        Health score 0-100
    """
    target_raw = kpi_def.get('target', 100)
    if isinstance(target_raw, dict):
        target = target_raw.get('value', 100)
        operator = target_raw.get('operator', '>')
    else:
        target = target_raw
        operator = '>'

    ranges = kpi_def.get('ranges', {})
    higher_is_better = kpi_def.get('higher_is_better', operator in ('>', '>='))

    SCORE_AT_TARGET = ht.healthy_min()   # 70
    SCORE_AT_RISK = ht.at_risk_min()     # 50

    if ranges and target:
        healthy_range = ranges.get('healthy', {})
        critical_range = ranges.get('critical', {})
        risk_range = ranges.get('risk', {})

        if higher_is_better:
            floor = critical_range.get('min', 0)
            risk_boundary = risk_range.get('min', critical_range.get('max', floor))
            healthy_max = healthy_range.get('max', target * 1.2 if target else 100)

            if value <= floor:
                score = 0.0
            elif value < risk_boundary:
                span = risk_boundary - floor
                score = (SCORE_AT_RISK * (value - floor) / span) if span > 0 else 0.0
            elif value < target:
                span = target - risk_boundary
                score = SCORE_AT_RISK + ((SCORE_AT_TARGET - SCORE_AT_RISK) * (value - risk_boundary) / span) if span > 0 else SCORE_AT_RISK
            elif value >= healthy_max:
                score = 100.0
            else:
                span = healthy_max - target
                score = SCORE_AT_TARGET + ((100.0 - SCORE_AT_TARGET) * (value - target) / span) if span > 0 else 100.0
        else:
            ceiling = critical_range.get('max', target * 4 if target else 100)
            risk_boundary = risk_range.get('max', critical_range.get('min', ceiling))
            healthy_min_val = healthy_range.get('min', 0)

            if value >= ceiling:
                score = 0.0
            elif value > risk_boundary:
                span = ceiling - risk_boundary
                score = (SCORE_AT_RISK * (ceiling - value) / span) if span > 0 else 0.0
            elif value > target:
                span = risk_boundary - target
                score = SCORE_AT_RISK + ((SCORE_AT_TARGET - SCORE_AT_RISK) * (risk_boundary - value) / span) if span > 0 else SCORE_AT_RISK
            elif value <= healthy_min_val:
                score = 100.0
            else:
                span = target - healthy_min_val
                score = SCORE_AT_TARGET + ((100.0 - SCORE_AT_TARGET) * (target - value) / span) if span > 0 else 100.0

    elif target and target > 0:
        # Fallback: simple ratio scoring (no range info)
        if operator in ('<', '<='):
            score = min(100, (target / max(value, 0.01)) * 100)
        else:
            score = min(100, (value / target) * 100)
    else:
        score = value

    return max(0.0, min(100.0, score))


SCORER_VERSION = '2.0'   # 2.0: returns what it used (weights, codes) — 2026-09-05


def score_account_health(
    kpi_values: dict,
    kpi_catalog: dict,
    pillar_catalog: dict,
    pillar_weight_overrides: dict = None,
    enabled_pillars: set = None,
) -> tuple:
    """(overall_health, pillar_averages) — see score_account_health_explained for the full record."""
    r = score_account_health_explained(kpi_values, kpi_catalog, pillar_catalog, pillar_weight_overrides, enabled_pillars)
    return r['health'], r['pillars']


def score_account_health_explained(
    kpi_values: dict,
    kpi_catalog: dict,
    pillar_catalog: dict,
    pillar_weight_overrides: dict = None,
    enabled_pillars: set = None,
) -> dict:
    """
    Calculate account health from KPI values using any catalog.

    L1: KPI values → individual scores (0-100) via score_kpi()
    L2: KPI scores → pillar averages (weighted by weight_l1)
    L3: Pillar averages → overall health (weighted by weight_l2 or overrides)

    Args:
        kpi_values: {kpi_code: actual_value} — the measurements
        kpi_catalog: {kpi_code: kpi_def} — KPI definitions with ranges, target, weight_l1
        pillar_catalog: {pillar_code: pillar_def} — pillar definitions with weight_l2
        pillar_weight_overrides: optional {pillar_code: weight} from CustomerConfig
        enabled_pillars: optional set of pillar codes to include (skip others)

    Returns:
        (overall_health: float, pillar_averages: dict)
        overall_health is 0-100
        pillar_averages is {P1: 75.3, P2: 82.1, ...}
    """
    # L1: Score each KPI and accumulate per-pillar weighted sums
    pillar_scores = {}
    kpi_scores, kpi_weights, used, dropped = {}, {}, [], []

    for kpi_code, value in kpi_values.items():
        if kpi_code not in kpi_catalog:
            dropped.append(kpi_code)
            continue

        kpi_def = kpi_catalog[kpi_code]
        pillar = kpi_def.get('pillar', kpi_def.get('l1_category'))

        if not pillar:
            dropped.append(kpi_code)
            continue

        # Skip disabled pillars
        if enabled_pillars is not None and pillar not in enabled_pillars:
            dropped.append(kpi_code)
            continue

        if pillar not in pillar_scores:
            pillar_scores[pillar] = {'weighted_sum': 0.0, 'total_weight': 0.0}

        # Score the KPI value
        score = score_kpi(value, kpi_def)

        # L1 weight from definition (equal weight fallback)
        l1_weight = kpi_def.get('weight_l1', 0)
        if not l1_weight or l1_weight <= 0:
            l1_weight = 1.0

        pillar_scores[pillar]['weighted_sum'] += score * l1_weight
        pillar_scores[pillar]['total_weight'] += l1_weight
        kpi_scores[kpi_code] = round(score, 2)
        kpi_weights[kpi_code] = l1_weight
        used.append(kpi_code)

    # L2: Pillar weighted averages
    pillar_averages = {}
    for pillar, data in pillar_scores.items():
        if data['total_weight'] > 0:
            avg = data['weighted_sum'] / data['total_weight']
        else:
            avg = 0.0
        pillar_averages[pillar] = max(0.0, min(100.0, avg))

    # L3: Overall health from pillar scores
    overall_health = 0.0
    total_weight = 0.0
    pillar_weights_used = {}

    for pillar, score in pillar_averages.items():
        # Priority: override weights → catalog weight_l2 → equal weight
        if pillar_weight_overrides and pillar in pillar_weight_overrides:
            weight = float(pillar_weight_overrides[pillar])
        else:
            weight = pillar_catalog.get(pillar, {}).get('weight_l2', 0.2)
        pillar_weights_used[pillar] = weight
        overall_health += score * weight
        total_weight += weight

    if total_weight > 0:
        overall_health = overall_health / total_weight

    overall_health = max(0.0, min(100.0, overall_health))

    return {
        'health': overall_health, 'pillars': pillar_averages,
        'kpi_scores': kpi_scores, 'kpi_weights': kpi_weights,
        'pillar_weights': pillar_weights_used,             # normalised by total_weight at use; stored as applied
        'kpi_codes_used': sorted(used), 'kpi_codes_dropped': sorted(dropped),
        'scorer_version': SCORER_VERSION,
    }


def load_catalog_from_json(json_path: str, expected_vertical: str = None) -> tuple:
    """
    Load KPI and pillar catalogs from a JSON file.

    Expected JSON structure:
    {
        "vertical": "datacenter_v1",
        "pillars": {"P1": {"name": "...", "weight_l2": 0.15}, ...},
        "kpis": {"P1-KPI1": {"name": "...", "pillar": "P1", "weight_l1": 0.2, "ranges": {...}}, ...}
    }

    Also supports flat KPI-only format (pillar defs extracted from KPIs):
    {"P1-KPI1": {"pillar": "P1", "weight_l1": 0.2, ...}, ...}

    Args:
        expected_vertical: if given and the file declares a "vertical" field,
            they must match. Catches a catalog file being copied to a new
            vertical's filename without its content actually being updated
            for that vertical — the general form of the Aug 21 2026
            vertical-coupling audit's Finding 1 (get_kpi_catalog serving
            dc2_s's content while reporting its own vertical as
            datacenter_v1). The path itself is already name-derived
            (utils.vertical_registry._find_json_catalog_path), so this is a
            second, independent check on the content, not the filename.
            No-op for files with no "vertical" field (not every catalog
            declares one) or when expected_vertical isn't given.

    Returns:
        (kpi_catalog, pillar_catalog) tuple of dicts
    """
    import json
    with open(json_path, 'r') as f:
        data = json.load(f)

    declared_vertical = data.get('vertical') if isinstance(data, dict) else None
    if expected_vertical is not None and declared_vertical is not None \
            and declared_vertical != expected_vertical:
        raise ValueError(
            f"Catalog file {json_path} declares vertical={declared_vertical!r} "
            f"but was loaded to resolve vertical={expected_vertical!r} — "
            f"refusing to serve mismatched content under the wrong vertical's name."
        )

    if 'kpis' in data and 'pillars' in data:
        return data['kpis'], data['pillars']

    # Flat format: assume all entries are KPIs, extract pillars
    kpi_catalog = data
    pillar_catalog = {}
    for kpi_code, kpi_def in kpi_catalog.items():
        pillar = kpi_def.get('pillar', '')
        if pillar and pillar not in pillar_catalog:
            pillar_catalog[pillar] = {
                'name': pillar,
                'weight_l2': kpi_def.get('pillar_weight_l2', 0.2),
            }

    return kpi_catalog, pillar_catalog


def load_catalog_from_dict(kpi_dict: dict) -> tuple:
    """
    Load catalogs from a dict (e.g., from CustomerConfig.kpi_definitions JSON column).

    Same format support as load_catalog_from_json.

    Returns:
        (kpi_catalog, pillar_catalog) tuple of dicts
    """
    if 'kpis' in kpi_dict and 'pillars' in kpi_dict:
        return kpi_dict['kpis'], kpi_dict['pillars']

    # Flat format
    kpi_catalog = kpi_dict
    pillar_catalog = {}
    for kpi_code, kpi_def in kpi_catalog.items():
        pillar = kpi_def.get('pillar', '')
        if pillar and pillar not in pillar_catalog:
            pillar_catalog[pillar] = {
                'name': pillar,
                'weight_l2': kpi_def.get('pillar_weight_l2', 0.2),
            }

    return kpi_catalog, pillar_catalog
