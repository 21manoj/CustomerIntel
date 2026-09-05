"""
Ask AI settings — config/ask_ai.json, loaded once.

    from ask_ai import settings
    settings.get('context', 'max_chars')
    settings.llm_model()                 # env ASK_AI_MODEL or llm.model

Same contract as signal_engine/settings.py: nothing in the package carries
its own number; a missing key raises, it does not invent a default.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'ask_ai.json'
MODEL_ENV = 'ASK_AI_MODEL'


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
            raise KeyError(f'ask_ai.json has no {"/".join(keys)}')
        node = node[k]
    return node


def llm_model() -> str:
    return os.environ.get(MODEL_ENV) or get('llm', 'model')
