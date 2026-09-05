"""
Wizard D settings — config/wizard_d.json, loaded once.

    from wizards import wizard_d_settings as settings
    settings.get('calibration', 'min_labels')          # 30
    settings.vertical_get('saas_premium', 'prior', 'base_retention_at_decision')

Nothing in Foresight carries its own number; a missing key raises here, it
is never invented. `vertical_get` reads `<section>.verticals.<vertical>.<key>`
first and falls back to `<section>.<key>` — the only place a vertical-specific
value can come from is that config block.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'wizard_d.json'


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
            raise KeyError(f'wizard_d.json has no {"/".join(keys)}')
        node = node[k]
    return node


def vertical_get(vertical: str, section: str, *keys):
    """`section.verticals.<vertical>.<keys>` when the override exists, else `section.<keys>`."""
    overrides = get(section, 'verticals').get(vertical or '', {})
    node = overrides
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return get(section, *keys)
        node = node[k]
    return node
