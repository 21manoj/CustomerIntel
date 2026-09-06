"""
Adapter settings — config/adapters.json, loaded once.

    from adapters import settings
    settings.get('receiver', 'timestamp_tolerance_seconds')      # 300

Nothing in adapters/ carries its own number; a missing key raises, it is
not invented (the signal_engine/settings.py pattern).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'adapters.json'


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def reload() -> dict:
    load.cache_clear()
    return load()


def get(*keys):
    """Nested lookup; KeyError names the missing path."""
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise KeyError(f'adapters.json has no {"/".join(keys)}')
        node = node[k]
    return node


GOVERNANCE_PATH = Path(__file__).resolve().parent.parent / 'config' / 'playbook_governance.json'


@lru_cache(maxsize=1)
def contract() -> dict:
    """The webhook contract as the platform defines it (config/playbook_governance.json → webhook):
    header names, signature scheme, report states. One definition, read by both sides."""
    with open(GOVERNANCE_PATH, encoding='utf-8') as f:
        gov = json.load(f)
    return {**gov['webhook'], 'report_states': list(gov['report_states'])}
