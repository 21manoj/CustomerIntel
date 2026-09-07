"""
Power-of-1 settings — config/power_of_1.json, config/economics/<vertical>.json and
config/investment/<vertical>.json, loaded once.

    from roi import settings
    settings.get('priority', 'weights', 'phase')      # 0.25
    settings.economics('saas_premium')                # the vertical's assumed economics (raises for an unknown vertical)
    settings.investment('saas_premium')               # the vertical's assumed investment-cost model (raises for an unknown vertical)

Nothing in roi/ carries its own number; a missing key or a missing
economics/investment file raises — no default, no fallback vertical.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / 'config'
CONFIG_PATH = CONFIG_DIR / 'power_of_1.json'
ECONOMICS_DIR = CONFIG_DIR / 'economics'
INVESTMENT_DIR = CONFIG_DIR / 'investment'

ECONOMICS_REQUIRED = ('version', 'vertical', 'basis', 'horizon_months',
                      'retention_sensitivity_per_health_point', 'revenue_at_risk_share_by_band')
ECONOMICS_BASIS = 'assumed'

INVESTMENT_REQUIRED = ('version', 'vertical', 'basis', 'csm_hourly_cost', 'playbooks', 'pillars', 'kpis')
INVESTMENT_BASIS = 'assumed'


class EconomicsConfigError(ValueError):
    pass


class InvestmentConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


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


def investment_path(vertical: str) -> Path:
    return INVESTMENT_DIR / f'{vertical}.json'


def _require_money_shape(d, where: str, allow_null: bool = False) -> None:
    """A {value, basis} object (roi.basis.money()'s shape): value a number (or null when allow_null —
    the honest 'doesn't fit this shape' case, e.g. a pure-expansion playbook's health-point lift),
    basis a non-empty sentence."""
    if not isinstance(d, dict) or 'value' not in d or 'basis' not in d:
        raise InvestmentConfigError(f'{where} needs a {{value, basis}} object')
    v = d['value']
    if v is not None and not isinstance(v, (int, float)):
        raise InvestmentConfigError(f'{where}.value must be a number or null')
    if v is None and not allow_null:
        raise InvestmentConfigError(f'{where}.value must not be null')
    if not d.get('basis'):
        raise InvestmentConfigError(f'{where} needs a basis sentence')


@lru_cache(maxsize=16)
def investment(vertical: str) -> dict:
    """The vertical's assumed investment-cost model, validated. Fails closed: an unknown vertical
    (no catalog) or a catalog vertical without an investment file raises — there is no vertical to
    borrow from, same discipline as economics(). csm_hourly_cost is one blended rate for the whole
    file; each playbooks.<id> entry carries estimated_csm_hours / estimated_cost_per_execution /
    estimated_health_point_lift_per_execution ({value, basis}, the lift may be null with a note —
    e.g. a pure-expansion playbook has no health-point lift); pillars.<code> and kpis.<code> roll up
    which playbooks cover them (playbooks: [] + status: 'not_covered' when none do)."""
    from utils.vertical_registry import normalize_vertical, get_pillars
    v = normalize_vertical(vertical)
    get_pillars(v)                                     # raises ValueError for a vertical with no catalog
    path = investment_path(v)
    if not path.exists():
        raise InvestmentConfigError(f'no investment file for vertical {v!r}: expected {path}')
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    missing = [k for k in INVESTMENT_REQUIRED if k not in data]
    if missing:
        raise InvestmentConfigError(f'{path.name}: missing {missing}')
    if data['vertical'] != v:
        raise InvestmentConfigError(f'{path.name} declares vertical={data["vertical"]!r}, expected {v!r}')
    if data['basis'] != INVESTMENT_BASIS:
        raise InvestmentConfigError(f'{path.name}: basis must be {INVESTMENT_BASIS!r}')
    _require_money_shape(data['csm_hourly_cost'], f'{path.name}: csm_hourly_cost')
    if not isinstance(data['playbooks'], dict):
        raise InvestmentConfigError(f'{path.name}: playbooks must be an object')
    for pid, pb in data['playbooks'].items():
        if not isinstance(pb, dict):
            raise InvestmentConfigError(f'{path.name}: playbooks.{pid} must be an object')
        for key in ('targets_pillars', 'targets_kpis'):
            if not isinstance(pb.get(key), list):
                raise InvestmentConfigError(f'{path.name}: playbooks.{pid}.{key} must be a list')
        _require_money_shape(pb.get('estimated_csm_hours'), f'{path.name}: playbooks.{pid}.estimated_csm_hours')
        _require_money_shape(pb.get('estimated_cost_per_execution'), f'{path.name}: playbooks.{pid}.estimated_cost_per_execution')
        _require_money_shape(pb.get('estimated_health_point_lift_per_execution'),
                             f'{path.name}: playbooks.{pid}.estimated_health_point_lift_per_execution', allow_null=True)
    if not isinstance(data['pillars'], dict):
        raise InvestmentConfigError(f'{path.name}: pillars must be an object')
    for pcode, pdef in data['pillars'].items():
        if not isinstance(pdef, dict) or not pdef.get('name') or not isinstance(pdef.get('playbooks'), list):
            raise InvestmentConfigError(f'{path.name}: pillars.{pcode} needs name + a playbooks list')
    if not isinstance(data['kpis'], dict):
        raise InvestmentConfigError(f'{path.name}: kpis must be an object')
    return data


def investment_verticals() -> list:
    return sorted(p.stem for p in INVESTMENT_DIR.glob('*.json'))
