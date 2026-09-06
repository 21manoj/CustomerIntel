"""app_api settings — config/app_api.json, loaded once. Nothing in app_api/ carries its own number."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'app_api.json'


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def get(*keys):
    node = load()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise KeyError(f'app_api.json has no {"/".join(keys)}')
        node = node[k]
    return node


def reload() -> dict:
    load.cache_clear()
    return load()
