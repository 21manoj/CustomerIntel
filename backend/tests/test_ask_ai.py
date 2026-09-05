"""
Ask AI over the journey contract (P10) — against Postgres.

The rule under test: every answer sentence cites ids the model was shown;
a ghost citation or an uncited sentence is dropped and listed under
`unsupported`; numbers come from the read layer; without a key the stub
answers from the narrative block; portfolio questions cite rows; the
scrubber hides later evidence; the HTTP route is key-authenticated; the
MCP tool is registered and frictionless; every model call is metered.
"""
import asyncio
import os
import sys
import uuid
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop('ANTHROPIC_API_KEY', None)          # stub path by default; tests that need the model path set a fake key
os.environ['FEATURE_SIGNAL_ENGINE'] = 'true'

from flask import Flask
from extensions import db

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common
_common._flask_app = app

import utils.health_thresholds as ht
from models import Account, HealthScore, ContextNode, JourneyData

from ask_ai import settings
from ask_ai.answer import ask, validate_answer, decide_scope, apply_as_of, STUB_MODEL, GENERATOR, TOOL_NAME


@pytest.fixture(scope='module')
def tenant():
    """A customer with two accounts; the first carries three scored months and
    two structured signals (so a journey + narrative exist), the second only
    scores — enough for a portfolio with two rows."""
    _assert_isolated_test_db(TEST_DB)
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer, submit_signal
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'AskAI {tag}', domain=f'askai-{tag}.test', vertical='saas_premium',
                              admin_email=f'ask_{tag}@t.test', admin_name='A')['customer_id']
        a = Account(customer_id=cid, account_name='Northwind Analytics', revenue=1_800_000, vertical='saas_premium',
                    external_account_id='northwind.com',
                    profile_metadata={'primary_champion_name': 'Elena Rossi', 'primary_champion_title': 'VP Data',
                                      'csm_name': 'Maya Johnson', 'renewal_date': '2026-08-01'})
        b = Account(customer_id=cid, account_name='Contoso Freight', revenue=400_000, vertical='saas_premium',
                    external_account_id='contoso.com', profile_metadata={})
        db.session.add_all([a, b])
        db.session.flush()
        for m, s in [(date(2026, 1, 1), 80), (date(2026, 2, 1), 66), (date(2026, 3, 1), 48)]:
            db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s, kpi_only_score=s,
                                       health_status=ht.classify(s)))
        for m, s in [(date(2026, 1, 1), 85), (date(2026, 2, 1), 86)]:
            db.session.add(HealthScore(account_id=b.account_id, measurement_month=m, health_score=s, kpi_only_score=s,
                                       health_status=ht.classify(s)))
        db.session.add(ContextNode(customer_id=cid, account_id=a.account_id, node_type='STAKEHOLDER', node_subtype='champion',
                                   source='observed', title='Elena Rossi (VP Data)', properties={'name': 'Elena Rossi', 'title': 'VP Data'},
                                   tier=1, occurred_at=datetime(2025, 8, 1)))
        db.session.commit()
        r1 = submit_signal(cid, a.account_id, 'Champion (VP Data) left the company — CRM contact updated',
                           source_type='crm_activity', signal_type='champion_departure', occurred_at='2026-02-10T09:30:00Z',
                           participants=[{'name': 'Elena Rossi', 'role': 'VP Data'}], source_ref='crm:evt:1')
        r2 = submit_signal(cid, a.account_id, 'Ticket #4412: validating a second provider for training jobs',
                           source_type='ticket', signal_type='competitor_mention', occurred_at='2026-03-20T08:00:00Z')
        assert r1['processed'] and r2['processed']
        from journeys.wizard_a import run_wizard_a
        run_wizard_a(cid, [a.account_id, b.account_id])
        yield cid, a.account_id, b.account_id, r1['evidence']['node_id'], r2['evidence']['node_id']
        db.session.remove()
        db.drop_all()


def _journey(cid, aid):
    with app.app_context():
        return JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json


# ── the validator ───────────────────────────────────────────────────────

class TestValidator:
    def test_keeps_only_sentences_whose_citations_resolve(self, tenant, monkeypatch):
        cid, aid, _, node1, _ = tenant
        j = _journey(cid, aid)
        good = f'sig:{node1}'
        assert any(e['episode_id'] == good for e in j['episodes'])
        seen = {}

        def fake_model(customer_id, system, user):
            seen['system'], seen['user'] = system, user
            return {'answer_sentences': [
                {'text': 'The champion left in February 2026, recorded from a CRM activity.', 'cites': [good]},
                {'text': 'The CFO said the budget was cut.', 'cites': ['sig:999999']},            # ghost
                {'text': 'Health will probably recover by summer.', 'cites': []},                   # uncited
            ], 'evidence_gaps': ['no outcome after the renewal date'], 'confidence': 0.7}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'why did the champion leave?', account_id=aid)
        assert res['scope'] == 'account' and res['model'] == 'fake-model' and res['generator'] == GENERATOR
        assert [s['cites'] for s in res['sentences']] == [[good]]
        assert res['answer'].startswith('The champion left')
        assert {u['reason'] for u in res['unsupported']} == {'unresolved_citation', 'no_citation'}
        ghost = next(u for u in res['unsupported'] if u['reason'] == 'unresolved_citation')
        assert ghost['unresolved'] == ['sig:999999']
        assert set(res['citations']) == {good} and res['citations'][good]['kind'] == 'signal'
        assert 'no outcome after the renewal date' in res['evidence_gaps'] and res['confidence'] == 0.7
        # the model saw the rules and the narrative, not raw tables
        assert 'Every sentence cites' in seen['system'] and '[narrative]' in seen['user'] and f'"id":"{good}"' in seen['user']
        assert 'row:' in seen['user']                                     # the account's own portfolio row is citable too

    def test_validate_answer_flags_numbers_not_in_cited_blocks(self):
        citable = {'sig:1': {'episode_id': 'sig:1', 'title': 'health 62.0 in March 2026'}}
        kept, unsupported = validate_answer({'answer_sentences': [
            {'text': 'Health stood at 62 in 2026.', 'cites': ['sig:1']},
            {'text': 'Health fell by 14 points.', 'cites': ['sig:1']},
            {'text': 'Same id twice is one citation.', 'cites': ['sig:1', 'sig:1']},
        ]}, citable, max_sentences=2)
        assert 'unverified_numbers' not in kept[0]
        assert kept[1]['unverified_numbers'] == ['14']                    # computed, not read
        assert unsupported[0]['reason'] == 'over_max_sentences'

    def test_validate_answer_tolerates_bad_shapes(self):
        kept, unsupported = validate_answer({'answer_sentences': ['not a dict', {'text': '', 'cites': ['x']},
                                                                  {'text': 'ok', 'cites': 'sig:1'}]}, {'sig:1': {}}, 5)
        assert kept == [{'text': 'ok', 'cites': ['sig:1']}] and unsupported == []


# ── the stub ────────────────────────────────────────────────────────────

class TestStub:
    def test_stub_answers_from_narrative_with_its_citations(self, tenant):
        cid, aid, _, node1, _ = tenant
        with app.app_context():
            res = ask(cid, 'what happened with the champion change?', account_id=aid)
        assert res['model'] == STUB_MODEL and res['generator'] == GENERATOR and res['scope'] == 'account'
        assert res['sentences'] and res['unsupported'] == []
        narrative_texts = {s['text'] for ch in _journey(cid, aid)['narrative']['chapters'] for s in ch['sentences']}
        assert all(s['text'] in narrative_texts for s in res['sentences'])          # nothing the story did not say
        assert any(f'sig:{node1}' in s['cites'] for s in res['sentences'])
        assert all(c in res['citations'] for s in res['sentences'] for c in s['cites'])
        assert any(g.startswith('stub:') for g in res['evidence_gaps'])
        assert res['confidence'] == settings.get('answer', 'stub_confidence')

    def test_account_named_in_question_selects_that_account(self, tenant):
        cid, aid, _, _, _ = tenant
        with app.app_context():
            res = ask(cid, 'Tell me about Northwind Analytics')
        assert res['scope'] == 'account' and res['scope_detail']['account_id'] == aid

    def test_role_named_in_question_pulls_role_evidence(self, tenant):
        cid, aid, _, _, node2 = tenant
        with app.app_context():
            res = ask(cid, 'is there commercial pressure on this account?', account_id=aid)
        assert res['scope_detail']['role_filter'] == 'commercial_pressure'
        with app.app_context():
            missing = ask(cid, 'any infra incident?', account_id=aid)
        assert missing['scope_detail']['role_filter'] == 'infra_incident'
        assert any('no observed evidence with role infra_incident' in g for g in missing['evidence_gaps'])   # absence is an answer

    def test_no_journey_is_a_lookup_error(self, tenant):
        cid, *_ = tenant
        with app.app_context(), pytest.raises(LookupError):
            ask(cid, 'anything', account_id=999999)
        with app.app_context(), pytest.raises(ValueError):
            ask(cid, '   ')


# ── portfolio ───────────────────────────────────────────────────────────

class TestPortfolio:
    def test_portfolio_scope_uses_row_citations(self, tenant):
        cid, aid, bid, _, _ = tenant
        with app.app_context():
            res = ask(cid, 'which accounts are most at risk?')
        assert res['scope'] == 'portfolio' and res['scope_detail']['accounts'] == 2
        assert res['sentences'] and all(c.startswith('row:') for s in res['sentences'] for c in s['cites'])
        assert set(res['citations']) <= {f'row:{aid}', f'row:{bid}'}
        assert res['sentences'][0]['cites'] == [f'row:{aid}']            # Northwind: at-risk, lower score, ranks first
        assert 'Northwind Analytics' in res['sentences'][0]['text'] and 'unverified_numbers' not in res['sentences'][0]

    def test_portfolio_model_path_rejects_unknown_rows(self, tenant, monkeypatch):
        cid, aid, bid, _, _ = tenant
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', lambda c, s, u: ({'answer_sentences': [
            {'text': 'Northwind Analytics is at risk.', 'cites': [f'row:{aid}']},
            {'text': 'Globex is fine.', 'cites': ['row:424242']},
        ], 'evidence_gaps': [], 'confidence': 0.9}, 'fake-model'))
        with app.app_context():
            res = ask(cid, 'summarise the portfolio')
        assert [s['cites'] for s in res['sentences']] == [[f'row:{aid}']]
        assert res['unsupported'][0]['unresolved'] == ['row:424242']

    def test_decide_scope_rules(self):
        rows = [{'account_id': 1, 'account_name': 'Acme'}, {'account_id': 2, 'account_name': 'Acme Cloud'}]
        assert decide_scope(1, 'anything', 7, rows) == ('account', 7)
        assert decide_scope(1, 'how is Acme Cloud doing?', None, rows) == ('account', 2)     # longest name wins
        assert decide_scope(1, 'which accounts are most at risk for Acme?', None, rows) == ('portfolio', None)
        assert decide_scope(1, 'general question', None, rows) == ('portfolio', None)


# ── time travel ─────────────────────────────────────────────────────────

class TestScrubber:
    def test_as_of_hides_later_evidence_and_revalidates_narrative(self, tenant):
        cid, aid, _, node1, node2 = tenant
        j = _journey(cid, aid)
        j['evidence'] = {str(node1): {'node_id': node1}, str(node2): {'node_id': node2}}
        then, gaps = apply_as_of(j, datetime(2026, 2, 28, 23, 59, 59))
        ids = {e['episode_id'] for e in then['episodes']}
        assert f'sig:{node1}' in ids and f'sig:{node2}' not in ids
        assert set(then['evidence']) == {str(node1)}
        assert all(s['month'] <= '2026-02-01' for s in then['leading_vs_trailing']['series'])
        assert then['narrative']['validated'] and f'sig:{node2}' not in then['narrative']['cited_episode_ids']
        assert any('hidden by the scrubber' in g for g in gaps)
        with app.app_context():
            res = ask(cid, 'is there commercial pressure?', account_id=aid, as_of='2026-02-28')
        assert f'sig:{node2}' not in res['citations'] and str(node2) not in res['citations']
        assert any('no observed evidence with role commercial_pressure' in g for g in res['evidence_gaps'])


# ── settings ────────────────────────────────────────────────────────────

class TestSettings:
    def test_missing_setting_raises_and_env_overrides_model(self, monkeypatch):
        with pytest.raises(KeyError):
            settings.get('llm', 'no_such_key')
        monkeypatch.delenv(settings.MODEL_ENV, raising=False)
        assert settings.llm_model() == settings.get('llm', 'model')
        monkeypatch.setenv(settings.MODEL_ENV, 'claude-test-override')
        assert settings.llm_model() == 'claude-test-override'


# ── metering ────────────────────────────────────────────────────────────

class TestMetering:
    def test_model_call_is_forced_tool_use_and_metered(self, tenant, monkeypatch):
        cid, aid, *_ = tenant
        calls, usage = {}, []

        class _Usage:
            input_tokens, output_tokens = 1234, 56

        class _Block:
            type = 'tool_use'
            input = {'answer_sentences': [], 'evidence_gaps': [], 'confidence': 0.1}

        class _Resp:
            usage, content = _Usage(), [_Block()]

        class _Messages:
            def create(self, **kw):
                calls.update(kw)
                return _Resp()

        class _Client:
            def __init__(self, api_key=None):
                self.messages = _Messages()

        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', _Client)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: usage.append(kw))
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setenv(settings.MODEL_ENV, 'claude-test-model')
        with app.app_context():
            res = ask(cid, 'anything at all', account_id=aid)
        assert res['model'] == 'claude-test-model' and res['sentences'] == []
        assert calls['tool_choice'] == {'type': 'tool', 'name': TOOL_NAME} and calls['tools'][0]['name'] == TOOL_NAME
        assert calls['max_tokens'] == settings.get('llm', 'max_tokens') and calls['model'] == 'claude-test-model'
        assert usage == [{'customer_id': cid, 'module': settings.get('llm', 'module'), 'tokens_in': 1234, 'tokens_out': 56,
                          'model': 'claude-test-model', 'success': True}]

    def test_failed_model_call_is_metered_and_raised(self, tenant, monkeypatch):
        cid, aid, *_ = tenant
        usage = []

        class _Client:
            def __init__(self, api_key=None):
                pass

            class messages:
                @staticmethod
                def create(**kw):
                    raise RuntimeError('boom')

        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', _Client)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: usage.append(kw))
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        with app.app_context(), pytest.raises(RuntimeError):
            ask(cid, 'anything', account_id=aid)
        assert usage and usage[0]['success'] is False and usage[0]['error_message'] == 'boom'


# ── HTTP + MCP surface ──────────────────────────────────────────────────

class TestSurface:
    @pytest.fixture(scope='class')
    def client(self, tenant):
        key = 'askai-server-key-' + uuid.uuid4().hex
        os.environ['MCP_SERVER_API_KEY'] = key
        import mcp_server.auth as auth
        auth.MCP_SERVER_API_KEY = key
        from server import build_asgi_app
        asgi = build_asgi_app(TEST_DB, create_schema=False)
        from starlette.testclient import TestClient
        with TestClient(asgi) as c:
            c.key = key
            yield c
        os.environ['MCP_TRANSPORT'] = 'stdio'

    def test_http_401_without_key_and_200_with(self, client, tenant):
        cid, aid, *_ = tenant
        from ask_ai.http import ROUTES
        assert '/api/ask' in ROUTES
        assert client.post('/api/ask', json={'customer_id': cid, 'question': 'why?', 'account_id': aid}).status_code == 401
        h = {'Authorization': f'Bearer {client.key}'}
        r = client.post('/api/ask', headers=h, json={'customer_id': cid, 'question': 'what happened with the champion?', 'account_id': aid})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['generator'] == GENERATOR and body['sentences'] and body['citation_rule']
        assert client.post('/api/ask', headers=h, json={'customer_id': cid}).status_code == 400
        assert client.post('/api/ask', headers=h, json={'customer_id': cid, 'question': 'x', 'account_id': 999999}).status_code == 404
        assert client.post('/api/ask', headers=h, json={'customer_id': cid, 'question': 'x', 'account_id': aid, 'as_of': 'nope'}).status_code == 400

    def test_mcp_tool_registered_and_frictionless(self, tenant):
        from mcp_server.cs_pulse_mcp_server import mcp
        from mcp_server.onboarding_tool_registry import ONBOARDING_TOOLS
        assert 'ask' in ONBOARDING_TOOLS
        tool = asyncio.run(mcp.get_tool('ask'))
        assert tool is not None and 'question' in tool.parameters['properties']
        cid, aid, *_ = tenant
        from mcp_server.cs_pulse_onboarding import ask as ask_tool
        res = ask_tool(cid, 'what happened with the champion?', account_id=aid)
        assert res['model'] == STUB_MODEL and res['sentences']
        from fastmcp.exceptions import ToolError
        with pytest.raises(ToolError):
            ask_tool(cid, 'x', account_id=999999)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
