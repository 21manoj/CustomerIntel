"""
Signal engine settings — config/signal_engine.json, loaded once.

    from signal_engine import settings
    settings.get('dedup', 'window_days')        # 7
    settings.llm_model()                         # env SIGNAL_ENRICHMENT_MODEL or llm.model

Nothing in the engine carries its own number; if a value is missing here
the loader raises, it does not invent a default.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'signal_engine.json'
MODEL_ENV = 'SIGNAL_ENRICHMENT_MODEL'


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
            raise KeyError(f'signal_engine.json has no {"/".join(keys)}')
        node = node[k]
    return node


def llm_model() -> str:
    return os.environ.get(MODEL_ENV) or get('llm', 'model')
