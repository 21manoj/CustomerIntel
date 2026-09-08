"""
signal_engine/external_research.py — the Claygent-inspired research step.

The rule under test: research_company() is gated by the real LLM budget
circuit breaker (not a new rate limiter), every call is metered whether it
succeeds or fails, research_and_ingest() reuses the exact same ingest +
process_now path submit_signal does, no API key means a real error (no
stub), and an account that doesn't belong to the customer is rejected
before any model call is made.
"""
import os
import sys
import uuid
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop('ANTHROPIC_API_KEY', None)          # tests that need the model path set a fake key
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

from models import Account
from signal_engine.external_research import research_company, research_and_ingest, MODULE_NAME


@pytest.fixture(scope='module')
def tenant():
    _assert_isolated_test_db(TEST_DB)
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(data_origin='synthetic_test', name=f'ExtResearch {tag}', domain=f'extresearch-{tag}.test',
                              vertical='saas_premium', admin_email=f'er_{tag}@t.test', admin_name='A')['customer_id']
        a = Account(customer_id=cid, account_name='Northwind Analytics', revenue=1_800_000, vertical='saas_premium',
                    external_account_id='northwind.com')
        db.session.add(a)
        db.session.commit()
        yield cid, a.account_id
        db.session.remove()
        db.drop_all()


def _fake_client(text='No recent leadership changes found. Reported but not verified: possible layoffs (source: a review site).'):
    class _Usage:
        input_tokens, output_tokens = 800, 120

    class _TextBlock:
        type = 'text'
        def __init__(self, t):
            self.text = t

    class _Resp:
        def __init__(self, t):
            self.usage = _Usage()
            self.content = [_TextBlock(t)]

    class _Messages:
        def __init__(self, t):
            self._t = t
            self.calls = []
        def create(self, **kw):
            self.calls.append(kw)
            return _Resp(self._t)

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages(text)

    return _Client


class TestResearchCompany:
    def test_no_api_key_raises_no_stub(self, tenant):
        cid, aid = tenant
        with pytest.raises(RuntimeError, match='no stub exists'):
            research_company('Some Company', customer_id=cid)

    def test_real_call_uses_web_search_tool_and_is_metered(self, tenant, monkeypatch):
        cid, aid = tenant
        usage = []
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        FakeClient = _fake_client()
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', FakeClient)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: usage.append(kw))

        with app.app_context():
            res = research_company('Northwind Analytics', domain='northwind.com', customer_id=cid)

        assert res['status'] == 'ok'
        assert 'reported but not verified' in res['findings_text'].lower()
        assert res['model'] == 'claude-opus-5'
        # metered exactly once, success, right module — the standing rule this project checks for every LLM call site
        assert len(usage) == 1
        assert usage[0]['customer_id'] == cid and usage[0]['module'] == MODULE_NAME and usage[0]['success'] is True
        assert usage[0]['tokens_in'] == 800 and usage[0]['tokens_out'] == 120

    def test_web_search_tool_declared_correctly(self, tenant, monkeypatch):
        cid, aid = tenant
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        FakeClient = _fake_client()
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', FakeClient)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: None)

        client_instance = FakeClient()
        monkeypatch.setattr(anthropic, 'Anthropic', lambda: client_instance)
        with app.app_context():
            research_company('Acme Corp', customer_id=cid)

        call = client_instance.messages.calls[0]
        assert call['tools'] == [{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 5}]
        assert 'Acme Corp' in call['messages'][0]['content']
        assert call['model'] == 'claude-opus-5'

    def test_no_findings_text_is_reported_as_error_not_raised(self, tenant, monkeypatch):
        cid, aid = tenant
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        FakeClient = _fake_client(text='')
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', FakeClient)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: None)

        with app.app_context():
            res = research_company('Empty Findings Co', customer_id=cid)
        assert res['status'] == 'error' and 'no findings' in res['error']


class TestResearchAndIngest:
    def test_unknown_account_rejected_before_any_model_call(self, tenant, monkeypatch):
        cid, aid = tenant
        calls = []
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', lambda *a, **kw: calls.append(1))
        with app.app_context(), pytest.raises(ValueError, match='does not belong'):
            research_and_ingest(cid, 999999)
        assert calls == []                                  # never reached the model

    def test_full_flow_writes_an_external_signal(self, tenant, monkeypatch):
        cid, aid = tenant
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        text = 'Confirmed: the company announced a new VP of Engineering this month (source: company press release).'
        FakeClient = _fake_client(text=text)
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', FakeClient)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: None)

        with app.app_context():
            res = research_and_ingest(cid, aid)

        assert res['status'] == 'ok'
        assert res['research']['findings_text'] == text
        assert res['ingest']['status'] in ('queued', 'exists')
        assert 'signal_id' in res['ingest']

        with app.app_context():
            from models import QualitativeSignal
            sig = QualitativeSignal.query.filter_by(signal_id=res['ingest']['signal_id']).first()
            assert sig is not None and sig.source_type == 'external'
            assert sig.account_id == aid and sig.customer_id == cid

    def test_reuploading_same_day_research_is_idempotent(self, tenant, monkeypatch):
        """Same account + same findings text within the dedup window — a second research
        call the same day should report 'duplicate' (the free-text lane's own dedup, per
        ingest()'s docstring), not create a second QualitativeSignal row."""
        cid, aid = tenant
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        FakeClient = _fake_client(text='No changes found in any category.')
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', FakeClient)
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: None)

        with app.app_context():
            first = research_and_ingest(cid, aid)
            second = research_and_ingest(cid, aid)
        assert first['ingest']['status'] == 'queued'
        assert second['ingest']['status'] == 'duplicate'
        assert second['ingest']['duplicate_of'] == first['ingest']['signal_id']


class TestExternalFindingsAreCitable:
    """The claim this module makes — findings go through the SAME pipeline
    internal signals use — is only worth something if they end where internal
    signals end: as a normal graph node Ask AI can cite. Every other test here
    stops at the QualitativeSignal row, one hop short of the thing that
    matters. This walks the rest of it, so a future change that quietly makes
    external findings uncitable fails here instead of in a demo."""

    def test_a_finding_becomes_a_citable_ask_ai_episode_carrying_its_external_provenance(self, tenant, monkeypatch):
        cid, aid = tenant
        text = ('Confirmed: the company announced a $40M Series C led by Redpoint on 12 March 2026 '
                "(source: the company's own press release). No recent layoffs found.")
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        import anthropic
        monkeypatch.setattr(anthropic, 'Anthropic', _fake_client(text=text))
        import utils.llm_budget_controller as budget
        monkeypatch.setattr(budget, 'record_usage', lambda **kw: None)

        with app.app_context():
            res = research_and_ingest(cid, aid, process_now=False)
            assert res['status'] == 'ok'

        # The research call is done; hand the queued signal to extraction WITHOUT a key so it takes
        # enrichment's keyword stub. What is under test here is where the written node ends up, not
        # how well a model types it — the extraction lane has its own tests, and the fake client above
        # only knows how to answer the research prompt, not the extraction tool call.
        monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
        with app.app_context():
            from signal_engine.pipeline import process_pending
            assert process_pending(customer_id=cid, limit=50)['processed'] >= 1
            from models import ContextNode
            nodes = [n for n in ContextNode.query.filter_by(account_id=aid, node_type='SIGNAL').all()
                     if n.source_platform == 'external']
            # written as an OBSERVED SIGNAL node like any other — that, and only that, is what makes it
            # reachable by journeys.journey_builder.collect_episodes, which is what mints the sig: id
            assert nodes, 'no external SIGNAL node: nothing downstream could cite the finding'
            assert all(n.source == 'observed' for n in nodes)
            ext_cites = {f'sig:{n.node_id}' for n in nodes}

            from journeys.wizard_a import run_wizard_a
            run_wizard_a(cid, [aid])
            from ask_ai.answer import account_context
            ctx, _gaps, _meta, _narrative = account_context(cid, aid, 'what did we find about them?', None, None)
            shown = ctx.text()
            citable = ext_cites & set(ctx.citable)

        # the point: an Ask AI sentence citing this finding resolves, instead of being dropped as a ghost id
        assert citable, f'{sorted(ext_cites)} is not citable by Ask AI'
        assert '"source_platform":"external"' in shown       # where it came from travels with the evidence
        assert '"evidence_tier":"observed"' in shown         # ... and it is not dressed up as anything stronger


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
