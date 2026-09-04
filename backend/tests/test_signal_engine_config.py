"""
Signal engine v2 — no second vocabulary, no bare numbers.

  * every LLM intent code is a taxonomy subtype (classification is a lookup)
  * every urgency role floor names a real signal role
  * the retired modules stay gone
  * urgency is role floor ⊕ perceived, from config/signal_engine.json
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_every_intent_code_is_a_taxonomy_subtype():
    from signal_engine.enrichment import VALID_INTENTS
    from utils.taxonomy_loader import get_taxonomy
    tax = get_taxonomy('dc2_s')
    missing = [i for i in VALID_INTENTS if not tax.signal_role(i)]
    assert not missing, f'intent codes with no taxonomy role: {missing}'


def test_urgency_role_floors_name_real_roles():
    import json
    from signal_engine import settings
    roles = {k for k in json.load(open(BACKEND / 'config' / 'taxonomy_base.json'))['signal_roles'] if not k.startswith('_')}
    floors = settings.get('urgency', 'structural_by_role')
    assert set(floors) <= roles, set(floors) - roles
    assert set(floors.values()) <= {'low', 'medium', 'high', 'critical'}


def test_retired_modules_are_gone():
    pkg = BACKEND / 'signal_engine'
    for name in ('fusion.py', 'collision.py', 'cleanup.py'):
        assert not (pkg / name).exists(), name
    import signal_engine.models as m
    assert not hasattr(m, 'AlertRecord') and not hasattr(m, 'TIER_1_SUBTYPES')


def test_urgency_is_role_floor_or_perceived_whichever_is_higher():
    from signal_engine.urgency import classify_structural_urgency, resolve_effective_urgency
    assert classify_structural_urgency('escalation') == 'critical'
    assert classify_structural_urgency('routine') is None
    assert classify_structural_urgency(None) is None
    assert resolve_effective_urgency(None, 0.1, 0.0) == 'low'
    assert resolve_effective_urgency(None, 0.65, 0.0) == 'high'
    assert resolve_effective_urgency(None, 0.65, 0.9) == 'critical'      # escalation boost
    assert resolve_effective_urgency('medium', 0.1, 0.0) == 'medium'     # floor wins
    assert resolve_effective_urgency('medium', 0.85, 0.0) == 'critical'  # perceived wins


def test_model_env_override(monkeypatch):
    from signal_engine import settings
    monkeypatch.delenv(settings.MODEL_ENV, raising=False)
    assert settings.llm_model() == settings.get('llm', 'model')
    monkeypatch.setenv(settings.MODEL_ENV, 'claude-test-override')
    assert settings.llm_model() == 'claude-test-override'


def test_missing_setting_raises_instead_of_defaulting():
    import pytest
    from signal_engine import settings
    with pytest.raises(KeyError):
        settings.get('llm', 'no_such_key')
