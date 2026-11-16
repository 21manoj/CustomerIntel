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
EXPOSURE_MODEL = 'exposure'
EXPOSURE_REQUIRED = ('version', 'vertical', 'model', 'basis', 'horizon_months', 'account_fields', 'tiers',
                     'failure_events', 'annual_failure_likelihood_by_health', 'regulated_flag_note')

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
    if data.get('model') == EXPOSURE_MODEL:
        raise EconomicsConfigError(f'{path.name} prices exposure, not revenue: vertical {v!r} has no revenue economics '
                                   f'(use roi.exposure / settings.exposure_economics)')
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


def _require_sourced(d, where: str) -> None:
    """An assumed figure block: a basis sentence, and a sources list (each entry a claim, a url or null,
    and whether the page was read directly). An empty list is allowed only with the basis saying so."""
    if not isinstance(d, dict) or not d.get('basis'):
        raise EconomicsConfigError(f'{where} needs a basis sentence')
    srcs = d.get('sources')
    if not isinstance(srcs, list):
        raise EconomicsConfigError(f'{where} needs a sources list (may be empty when the basis says no source exists)')
    for i, s in enumerate(srcs):
        if not isinstance(s, dict) or not s.get('claim') or 'url' not in s or not isinstance(s.get('verified'), bool):
            raise EconomicsConfigError(f'{where}.sources[{i}] needs claim, url (or null) and verified true/false')


@lru_cache(maxsize=16)
def exposure_economics(vertical: str) -> dict:
    """The exposure model of a vertical whose money basis is exposure (catalog capability
    money_basis = 'exposure'), validated. Same file location and fail-closed discipline as economics():
    config/economics/<vertical>.json, with "model": "exposure". Shape:

      account_fields                    which Account.profile_metadata keys hold the tier, the regulated
                                        flag and the tenant's own recorded failure cost
      tiers                             ordered tier names, most critical first
      failure_events.<id>               {label, applies_when (a profile_metadata boolean key, or null =
                                        always), cost_by_tier {<tier>: number}, basis, sources}
      annual_failure_likelihood_by_health  {bands: [{min_health, value}, ...] highest min_health first,
                                        ending at 0; basis; sources}
      regulated_flag_note               {text, basis, sources} shown on flagged accounts; never summed
    """
    from utils.vertical_registry import normalize_vertical, get_pillars, capability
    v = normalize_vertical(vertical)
    get_pillars(v)                                     # raises ValueError for a vertical with no catalog
    if capability(v, 'money_basis') != EXPOSURE_MODEL:
        raise EconomicsConfigError(f'vertical {v!r} does not use the exposure money basis (catalog capability money_basis)')
    path = economics_path(v)
    if not path.exists():
        raise EconomicsConfigError(f'no economics file for vertical {v!r}: expected {path}')
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    missing = [k for k in EXPOSURE_REQUIRED if k not in data]
    if missing:
        raise EconomicsConfigError(f'{path.name}: missing {missing}')
    if data['vertical'] != v:
        raise EconomicsConfigError(f'{path.name} declares vertical={data["vertical"]!r}, expected {v!r}')
    if data['model'] != EXPOSURE_MODEL or data['basis'] != ECONOMICS_BASIS:
        raise EconomicsConfigError(f'{path.name}: model must be {EXPOSURE_MODEL!r} and basis {ECONOMICS_BASIS!r}')
    fields = data['account_fields']
    for k in ('tier', 'regulated_flag', 'recorded_failure_cost'):
        if not isinstance(fields, dict) or not isinstance(fields.get(k), str) or not fields[k]:
            raise EconomicsConfigError(f'{path.name}: account_fields.{k} must name a profile field')
    tiers = data['tiers']
    if not isinstance(tiers, list) or not tiers or len(set(tiers)) != len(tiers) or not all(isinstance(t, str) and t for t in tiers):
        raise EconomicsConfigError(f'{path.name}: tiers must be a non-empty list of distinct names')
    events = data['failure_events']
    if not isinstance(events, dict) or not events:
        raise EconomicsConfigError(f'{path.name}: failure_events must be a non-empty object')
    for eid, ev in events.items():
        where = f'{path.name}: failure_events.{eid}'
        _require_sourced(ev, where)
        if not ev.get('label'):
            raise EconomicsConfigError(f'{where} needs a label')
        if ev.get('applies_when') is not None and not isinstance(ev['applies_when'], str):
            raise EconomicsConfigError(f'{where}.applies_when must be a profile field name or null')
        costs = ev.get('cost_by_tier')
        if not isinstance(costs, dict) or set(costs) != set(tiers):
            raise EconomicsConfigError(f'{where}.cost_by_tier must give exactly the tiers {tiers}')
        for t, c in costs.items():
            if isinstance(c, bool) or not isinstance(c, (int, float)) or c < 0:
                raise EconomicsConfigError(f'{where}.cost_by_tier.{t} must be a number >= 0')
    lik = data['annual_failure_likelihood_by_health']
    _require_sourced(lik, f'{path.name}: annual_failure_likelihood_by_health')
    bands = lik.get('bands')
    if not isinstance(bands, list) or not bands:
        raise EconomicsConfigError(f'{path.name}: annual_failure_likelihood_by_health.bands must be a non-empty list')
    mins = []
    for i, b in enumerate(bands):
        ok = (isinstance(b, dict) and isinstance(b.get('min_health'), (int, float)) and isinstance(b.get('value'), (int, float))
              and not isinstance(b.get('value'), bool) and 0 <= b['value'] <= 1)
        if not ok:
            raise EconomicsConfigError(f'{path.name}: annual_failure_likelihood_by_health.bands[{i}] needs min_health and a value in 0..1')
        mins.append(b['min_health'])
    if mins != sorted(mins, reverse=True) or len(set(mins)) != len(mins) or mins[-1] != 0:
        raise EconomicsConfigError(f'{path.name}: likelihood bands must run from the highest min_health down to 0')
    note = data['regulated_flag_note']
    _require_sourced(note, f'{path.name}: regulated_flag_note')
    if not note.get('text'):
        raise EconomicsConfigError(f'{path.name}: regulated_flag_note needs text')
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
