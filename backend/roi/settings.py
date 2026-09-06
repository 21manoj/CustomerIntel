"""
Power-of-1 settings — config/power_of_1.json and config/economics/<vertical>.json, loaded once.

    from roi import settings
    settings.get('priority', 'weights', 'phase')      # 0.25
    settings.economics('saas_premium')                # the vertical's assumed economics (raises for an unknown vertical)

Nothing in roi/ carries its own number; a missing key or a missing
economics file raises — no default, no fallback vertical.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / 'config'
CONFIG_PATH = CONFIG_DIR / 'power_of_1.json'
ECONOMICS_DIR = CONFIG_DIR / 'economics'

ECONOMICS_REQUIRED = ('version', 'vertical', 'basis', 'horizon_months',
                      'retention_sensitivity_per_health_point', 'revenue_at_risk_share_by_band')
ECONOMICS_BASIS = 'assumed'


class EconomicsConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def reload() -> dict:
    load.cache_clear()
    economics.cache_clear()
    return load()


def get(*keys):
    """Nested lookup; KeyError names the missing path."""
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise KeyError(f'power_of_1.json has no {"/".join(keys)}')
        node = node[k]
    return node


def economics_path(vertical: str) -> Path:
    return ECONOMICS_DIR / f'{vertical}.json'


@lru_cache(maxsize=16)
def economics(vertical: str) -> dict:
    """The vertical's assumed economics, validated. Fails closed: an unknown vertical (no catalog)
    or a catalog vertical without an economics file raises — there is no vertical to borrow from."""
    from utils.vertical_registry import normalize_vertical, get_pillars
    v = normalize_vertical(vertical)
    get_pillars(v)                                     # raises ValueError for a vertical with no catalog
    path = economics_path(v)
    if not path.exists():
        raise EconomicsConfigError(f'no economics file for vertical {v!r}: expected {path}')
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    missing = [k for k in ECONOMICS_REQUIRED if k not in data]
    if missing:
        raise EconomicsConfigError(f'{path.name}: missing {missing}')
    if data['vertical'] != v:
        raise EconomicsConfigError(f'{path.name} declares vertical={data["vertical"]!r}, expected {v!r}')
    if data['basis'] != ECONOMICS_BASIS:
        raise EconomicsConfigError(f'{path.name}: basis must be {ECONOMICS_BASIS!r}')
    sens = data['retention_sensitivity_per_health_point']
    if not isinstance(sens, dict) or not isinstance(sens.get('value'), (int, float)) or not sens.get('basis'):
        raise EconomicsConfigError(f'{path.name}: retention_sensitivity_per_health_point needs value + basis sentence')
    bands = data['revenue_at_risk_share_by_band']
    for band in ('critical', 'at_risk', 'healthy'):
        if not isinstance(bands.get(band), (int, float)):
            raise EconomicsConfigError(f'{path.name}: revenue_at_risk_share_by_band.{band} must be a number')
    if not bands.get('basis'):
        raise EconomicsConfigError(f'{path.name}: revenue_at_risk_share_by_band needs a basis sentence')
    return data


def economics_verticals() -> list:
    return sorted(p.stem for p in ECONOMICS_DIR.glob('*.json'))
