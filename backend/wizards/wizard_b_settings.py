"""
Wizard B settings — config/wizard_b.json, loaded once, plus one tenant's overrides.

    retention(customer_id)          the effective realized-retention settings for a tenant
    retention_view(customer_id)     defaults, the tenant's overrides and the editable fields
    save_retention(customer_id, d)  validate and store a tenant's overrides ({} clears them)

A tenant's overrides live in customer_configs.analysis_settings['realized_retention'].
Only keys named in the config's `limits` may be overridden, and only inside those limits.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'wizard_b.json'
SECTION = 'realized_retention'


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def get(*keys):
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise KeyError(f'wizard_b.json has no {"/".join(keys)}')
        node = node[k]
    return node


def defaults() -> dict:
    return {k: v for k, v in get(SECTION, 'defaults').items() if not k.startswith('_')}


def _coerce(key: str, value):
    rule = get(SECTION, 'limits').get(key)
    if rule is None:
        raise ValueError(f'{key} is not a setting a tenant can change')
    kind = rule['type']
    if kind == 'choice':
        if value not in rule['choices']:
            raise ValueError(f"{key} must be one of: {', '.join(rule['choices'])}")
        return value
    if isinstance(value, bool):
        raise ValueError(f'{key} must be a number')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{key} must be a number')
    if kind == 'int':
        if number != int(number):
            raise ValueError(f'{key} must be a whole number')
        number = int(number)
    if number < rule['min'] or number > rule['max']:
        raise ValueError(f"{key} must be between {rule['min']} and {rule['max']}")
    return number


def validate(overrides: Optional[dict]) -> dict:
    return {k: _coerce(k, v) for k, v in (overrides or {}).items() if v is not None and v != ''}


def _config_row(customer_id: int):
    from models import CustomerConfig
    return CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()


def overrides(customer_id: Optional[int]) -> dict:
    if customer_id is None:
        return {}
    row = _config_row(customer_id)
    stored = ((row.analysis_settings or {}) if row is not None else {}).get(SECTION) or {}
    limits = get(SECTION, 'limits')
    return {k: v for k, v in stored.items() if k in limits}


def retention(customer_id: Optional[int] = None) -> dict:
    """Effective settings: the config's defaults, the tenant's overrides on top, and the
    bucket lists that are not tenant-editable."""
    section = get(SECTION)
    out = defaults() | overrides(customer_id)
    for k in ('loss_buckets', 'expansion_buckets', 'narrative_buckets', 'new_account_subtypes', 'days_per_month'):
        out[k] = section[k]
    return out


def retention_view(customer_id: int) -> dict:
    base, own = defaults(), overrides(customer_id)
    fields = [{'key': k, **{kk: vv for kk, vv in rule.items()}, 'default': base[k], 'value': own.get(k, base[k]),
               'overridden': k in own} for k, rule in get(SECTION, 'limits').items()]
    return {'fields': fields, 'defaults': base, 'overrides': own, 'effective': base | own}


def save_retention(customer_id: int, data: Optional[dict]) -> dict:
    """Store the overrides that differ from the defaults; an empty dict clears them."""
    from extensions import db
    from models import CustomerConfig
    from sqlalchemy.orm.attributes import flag_modified
    base = defaults()
    clean = {k: v for k, v in validate(data).items() if v != base[k]}
    row = _config_row(customer_id)
    if row is None:
        row = CustomerConfig(customer_id=int(customer_id))
        db.session.add(row)
    settings = dict(row.analysis_settings or {})
    if clean:
        settings[SECTION] = clean
    else:
        settings.pop(SECTION, None)
    row.analysis_settings = settings
    flag_modified(row, 'analysis_settings')
    db.session.commit()
    return retention_view(customer_id)
