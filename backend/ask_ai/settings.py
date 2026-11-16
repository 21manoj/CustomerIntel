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
QUESTIONS_PATH = Path(__file__).resolve().parent.parent / 'config' / 'ask_ai_questions.json'
MODEL_ENV = 'ASK_AI_MODEL'


@lru_cache(maxsize=1)
def load() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


QUESTIONS_DIR = Path(__file__).resolve().parent.parent / 'config' / 'ask_ai_questions'


def _read_questions(path: Path) -> dict:
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    out = {k: v for k, v in data.items() if not k.startswith('_')}
    for role, qs in out.items():
        if not isinstance(qs, list):
            raise ValueError(f'{path.name}: {role!r} must be a list of questions')
        for q in qs:
            if not isinstance(q, dict) or not q.get('id') or not q.get('text') or q.get('scope') not in ('portfolio', 'account'):
                raise ValueError(f'{path.name}: every {role!r} question needs id, text and scope (portfolio or account); got {q!r}')
    return out


@lru_cache(maxsize=None)
def curated_questions(vertical: str | None = None) -> dict:
    """{role: [{id, scope, text}]} — the UI's suggested-question chips, quality-checked
    by scripts/eval_ask_ai_questions.py against what ask_ai/answer.py can actually ground.

    The platform set is config/ask_ai_questions.json. A vertical may ship
    config/ask_ai_questions/<vertical>.json: each role it lists replaces the platform's
    list for that role (its accounts may not be customers, so "revenue" questions would
    have nothing to ground on); a role it does not list keeps the platform's."""
    base = _read_questions(QUESTIONS_PATH)
    if vertical:
        override = QUESTIONS_DIR / f'{vertical}.json'
        if override.is_file():
            own = _read_questions(override)
            unknown = sorted(set(own) - set(base))
            if unknown:
                raise ValueError(f'{override.name}: unknown roles {unknown} (known: {sorted(base)})')
            base = {**base, **own}
    return base


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
