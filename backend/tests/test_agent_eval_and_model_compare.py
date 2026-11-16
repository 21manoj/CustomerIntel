"""
The two step-7 harnesses, with no model call anywhere:

  * evals/playbook_agent_eval: the stub passes; a fake model that obeys the planted instruction fails the model's
    check (it repeats the marker) while the platform's check holds (nothing moved); a paid model needs --allow-spend
  * scripts/compare_models: the diff counts what differs; a paid side is refused without --allow-spend
"""
import json

import pytest

from test_playbook_autonomy import app, auto, _schema, PERSON, db      # noqa: F401 — fixtures


def test_agent_eval_stub_passes_and_obedient_fake_shows_the_platform_holds(auto, monkeypatch, tmp_path):
    cid, _, _ = auto
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with app.app_context():
        from evals import playbook_agent_eval as ev
        from playbooks import governance as gov
        gov.evaluate(cid, actor=PERSON)
        before = ev._snapshot(cid)

        stub = ev.run(cid, model='stub')
        assert stub['verdict'] == 'pass' and stub['models_used'] == ['stub'] and 'not a model result' in stub['evidence_label']
        m = stub['metrics']
        assert m['interventions'] >= 1 and m['step_coverage'] == 1.0 and m['state_changes'] == 0 and m['tokens'] == {'in': 0, 'out': 0}

        fake = ev.run(cid, model='fake-obedient')
        fm = fake['metrics']
        assert fm['state_changes'] == 0                                         # the platform's job: held
        assert fm['planted_marker_echoed'] == fm['planted_cases'] >= 1          # the model's job: failed, and reported
        assert fm['planted_extra_keys_ignored'] == fm['planted_cases']
        assert fm['discarded'] >= 1                                             # the step it invented was thrown away
        assert fake['verdict'] == 'fail' and fake['checks']['state_changes'] and not fake['checks']['planted_marker_echoed']
        assert ev._snapshot(cid) == before                                      # dry run: the eval itself wrote nothing

        path = ev.write_scorecard(stub, tmp_path)
        assert json.loads(path.read_text())['bars']['status'].startswith('proposed')


def test_agent_eval_refuses_a_paid_model_without_allow_spend(auto, monkeypatch):
    cid, _, _ = auto
    with app.app_context():
        from evals import playbook_agent_eval as ev
        from utils import model_provider
        monkeypatch.setattr(model_provider, 'available', lambda: False)
        with pytest.raises(ValueError, match='no model is configured'):
            ev.run(cid, model='configured', allow_spend=True)
        monkeypatch.setattr(model_provider, 'available', lambda: True)
        monkeypatch.setattr(model_provider, 'tool_call', lambda **kw: pytest.fail('a paid call was attempted'))
        with pytest.raises(PermissionError, match='--allow-spend'):
            ev.run(cid, model='configured')
        assert ev.run(cid, model='stub')['metrics']['tokens'] == {'in': 0, 'out': 0}     # stub never calls, even with a key


def test_model_compare_diff_and_spend_guard():
    from scripts import compare_models as cm
    a = {'signals': {'c1': {'roles': ['escalation']}, 'c2': {'roles': ['advocacy']}},
         'accounts': {'Acme': {'arc': 'x', 'health_band': 'at_risk'}, 'Bolt': {'arc': 'y', 'health_band': 'healthy'}},
         'playbooks': ['Acme → save', 'Bolt → grow']}
    b = {'signals': {'c1': {'roles': ['escalation']}, 'c2': {'roles': []}},
         'accounts': {'Acme': {'arc': 'x', 'health_band': 'at_risk'}, 'Bolt': {'arc': 'z', 'health_band': 'healthy'}},
         'playbooks': ['Acme → save']}
    d = cm.diff(a, b)
    assert d['communications'] == 2 and d['communications_read_differently'] == 1 and d['role_agreement'] == 0.5
    assert d['accounts_concluded_differently'] == 1 and d['account_differences'][0]['account'] == 'Bolt'
    assert d['playbooks_only_a'] == ['Bolt → grow'] and d['playbooks_only_b'] == []
    assert cm.is_paid('claude-sonnet-5') and not cm.is_paid('stub') and not cm.is_paid('oracle')
    with pytest.raises(PermissionError, match='--allow-spend'):
        cm.compare({'manifest_id': 'm', 'vertical': 'v'}, 'oracle', 'some-model')
