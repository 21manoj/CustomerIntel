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


def test_tool_schema_enum_is_exactly_the_tenant_vocabulary():
    """Free text can reach every role: the tool schema's subtype enum is the
    tenant's whole vocabulary (base + overlay), and every role has subtypes."""
    from signal_engine.enrichment import record_signals_tool, vocabulary_block, examples_block
    from utils.taxonomy_loader import get_taxonomy
    for vertical in ('dc2_s', 'saas_premium', 'datacenter_v1', 'healthcare_provider'):
        tax = get_taxonomy(vertical)
        enum = record_signals_tool(tax, [])['input_schema']['properties']['signals']['items']['properties']['subtype']['enum']
        assert enum == tax.all_subtypes() and len(enum) > 100, vertical
        assert all(len(subs) > 0 for subs in tax.signal_roles.values()), vertical
        block = vocabulary_block(tax)
        assert all(role in block for role in tax.signal_roles), vertical
        assert len(tax.examples) >= 7 and 'signals:' in examples_block(tax), vertical


def test_every_vertical_has_its_own_vocabulary_overlay():
    from utils.taxonomy_loader import get_taxonomy
    for vertical in ('dc2_s', 'saas_premium', 'datacenter_v1', 'healthcare_provider'):
        tax = get_taxonomy(vertical)
        assert len(tax.all_subtypes()) > 100, f'{vertical} has no overlay vocabulary'
    assert 'ehr_downtime' in get_taxonomy('healthcare_provider').signal_roles['infra_incident']
    assert 'ehr_downtime' not in get_taxonomy('saas_premium').signal_roles['infra_incident']


def test_roster_role_enum_only_when_roster_known():
    from signal_engine.enrichment import record_signals_tool
    from utils.taxonomy_loader import get_taxonomy
    tax = get_taxonomy('dc2_s')
    person = record_signals_tool(tax, [])['input_schema']['properties']['signals']['items']['properties']['people']['items']
    assert 'roster_role' not in person['properties']
    person = record_signals_tool(tax, [{'name': 'A', 'role': 'champion'}])['input_schema']['properties']['signals']['items']['properties']['people']['items']
    assert person['properties']['roster_role']['enum'] == ['champion']


def test_normalize_extraction_drops_unknown_and_flags_review():
    from signal_engine.enrichment import normalize_extraction
    from utils.taxonomy_loader import get_taxonomy
    tax = get_taxonomy('saas_premium')
    out = normalize_extraction({'signals': [
        {'subtype': 'seat_underutilization', 'quote': 'half the seats', 'sentiment_score': -0.4, 'urgency_score': 0.5,
         'escalation_probability': 0.1, 'confidence': 0.9, 'people': [{'name': 'Priya', 'roster_role': 'champion'}]},
        {'subtype': 'made_up_thing', 'quote': 'x', 'sentiment_score': 0, 'urgency_score': 0, 'confidence': 1},
    ], 'requires_review': False, 'is_duplicate': False, 'suggested_action': 'call'}, tax)
    assert [s['subtype'] for s in out['signals']] == ['seat_underutilization']
    assert out['signals'][0]['role'] == 'usage_decline'
    assert out['dropped_unknown_subtypes'] == 1 and out['requires_review'] is True
    assert out['intent_signals'] == ['seat_underutilization'] and out['stakeholder_roles'][0]['roster_role'] == 'champion'


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
