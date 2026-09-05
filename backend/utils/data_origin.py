"""
Data-origin disclosure — config/data_origins.json.

    from utils.data_origin import validate, disclosure, is_synthetic, block
    block(customer)   -> {'data_origin', 'label', 'synthetic', 'disclosure'}   for any read surface
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config', 'data_origins.json')


@lru_cache(maxsize=1)
def origins() -> dict:
    with open(_PATH, encoding='utf-8') as f:
        return json.load(f)['origins']


def validate(value) -> str:
    v = (value or '').strip().lower()
    if v not in origins():
        raise ValueError(f"data_origin must be one of {sorted(origins())}, got {value!r}")
    return v


def is_synthetic(value) -> bool:
    o = origins().get((value or '').strip().lower())
    return True if o is None else bool(o['synthetic'])          # unknown / unset counts as not-real


def disclosure(value) -> str:
    o = origins().get((value or '').strip().lower())
    if o is None:
        return 'DATA ORIGIN NOT DECLARED: this tenant predates origin tracking; treat its numbers as unverified.'
    return o['disclosure']


def block(customer) -> dict:
    v = getattr(customer, 'data_origin', None)
    o = origins().get((v or '').strip().lower())
    return {'data_origin': v, 'label': o['label'] if o else 'Undeclared', 'synthetic': is_synthetic(v), 'disclosure': disclosure(v)}
