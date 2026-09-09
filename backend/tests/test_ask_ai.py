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

from extensions import db

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')


def _assert_isolated_test_db(uri):
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    if 'test' not in uri.rsplit('/', 1)[-1].lower():
        raise RuntimeError('refusing non-test database')


from mcp_server.common import get_flask_app
app = get_flask_app()

import utils.health_thresholds as ht
from models import Account, HealthScore, ContextNode, JourneyData

from ask_ai import settings
from ask_ai.answer import (ask, validate_answer, decide_scope, apply_as_of, history_block, normalize_history,
                           STUB_MODEL, GENERATOR, TOOL_NAME)


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
        cid = create_customer(data_origin='synthetic_test', name=f'AskAI {tag}', domain=f'askai-{tag}.test', vertical='saas_premium',
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
        assert res['answer'].endswith('The champion left in February 2026, recorded from a CRM activity.') and res['answer'].startswith('[Test fixture] ')
        assert {u['reason'] for u in res['unsupported']} == {'unresolved_citation', 'no_citation'}
        ghost = next(u for u in res['unsupported'] if u['reason'] == 'unresolved_citation')
        assert ghost['unresolved'] == ['sig:999999']
        assert set(res['citations']) == {good} and res['citations'][good]['kind'] == 'signal'
        assert 'no outcome after the renewal date' in res['evidence_gaps'] and res['confidence'] == 0.7
        # the model saw the rules and the narrative, not raw tables
        assert 'Every sentence cites' in seen['system'] and '[narrative]' in seen['user'] and f'"id":"{good}"' in seen['user']
        assert 'row:' in seen['user']                                     # the account's own portfolio row is citable too

    def test_narrative_never_teaches_a_citation_the_model_cannot_use(self):
        """The narrative is prose ABOUT the episodes and its `cites` are episode ids, so whatever it
        names the model will cite — and validate_answer keeps a sentence only when every citation
        resolves. A narrative sentence citing an episode that did not fit the context budget produced
        a fluent answer that was then dropped whole, i.e. a confident empty string."""
        from ask_ai.answer import _narrative_for_context
        narrative = {'chapters': [
            {'phase': 'deterioration', 'from': '2025-10', 'to': '2025-12', 'sentences': [
                {'text': 'The fabric failed three times.', 'cites': ['sig:1', 'sig:999']},   # one shown, one not
                {'text': 'Nobody could say why.', 'cites': ['sig:999']},                     # none shown
            ]},
            {'phase': 'resolution', 'from': '2026-01', 'to': '2026-02', 'sentences': [
                {'text': 'Goodput came back.', 'cites': ['sig:998']},                        # none shown
            ]},
        ]}
        chapters, dropped = _narrative_for_context(narrative, {'sig:1': {}})
        assert dropped == 2
        assert len(chapters) == 1 and chapters[0]['phase'] == 'deterioration'  # an emptied chapter is dropped whole
        assert chapters[0]['sentences'] == [{'text': 'The fabric failed three times.', 'cites': ['sig:1']}]
        assert all(c in {'sig:1'} for ch in chapters for s in ch['sentences'] for c in s['cites'])

    def test_an_answer_emptied_by_the_citation_rule_says_so(self, tenant, monkeypatch):
        cid, aid, _, _, _ = tenant

        def fake_model(customer_id, system, user):
            return {'answer_sentences': [
                {'text': 'Everything here cites an episode that was never shown.', 'cites': ['sig:999999']},
            ], 'evidence_gaps': [], 'confidence': 0.86}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'what happened here?', account_id=aid)
        assert res['sentences'] == [] and res['unsupported']
        assert res['answer'] == '[Test fixture]'                     # no dangling whitespace after the label
        assert any('every sentence was dropped by the citation rule' in g for g in res['evidence_gaps'])
        assert any('unresolved_citation' in g for g in res['evidence_gaps'])

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


# ── conversation (in-session multi-turn) ────────────────────────────────

class TestConversation:
    """The rule under test: a follow-up may inherit WHAT the conversation is
    about, and nothing else. Scope carries over; facts do not — the recap is
    shown outside the context blocks and so can never be cited."""

    rows = [{'account_id': 1, 'account_name': 'Acme'}, {'account_id': 2, 'account_name': 'Acme Cloud'}]

    def _turn(self, q='how is Acme Cloud doing?', a='Acme Cloud slipped to 48.', aid=2):
        return {'question': q, 'answer': a, 'account_id': aid}

    def test_a_follow_up_that_names_nothing_inherits_the_previous_account(self):
        # the case the feature exists for: "their" has no referent without the last turn
        assert decide_scope(1, 'what about their champion?', None, self.rows) == ('portfolio', None)
        assert decide_scope(1, 'what about their champion?', None, self.rows, [self._turn()]) == ('account', 2)

    def test_this_turns_own_words_still_win_over_the_conversation(self):
        h = [self._turn()]
        assert decide_scope(1, 'and across accounts?', None, self.rows, h) == ('portfolio', None)   # changed the subject
        assert decide_scope(1, 'how is Acme doing?', None, self.rows, h) == ('account', 1)          # named another account
        assert decide_scope(1, 'anything', 7, self.rows, h) == ('account', 7)                       # explicit id still wins

    def test_the_most_recent_resolved_turn_is_the_one_inherited(self):
        h = [self._turn(aid=2), self._turn(q='and the portfolio?', a='Two accounts.', aid=None), self._turn(aid=1)]
        assert decide_scope(1, 'why?', None, self.rows, h) == ('account', 1)

    def test_an_account_not_in_this_portfolio_is_never_inherited(self):
        # the client sends the history back, so its ids are caller input: the portfolio rows are the
        # authority, exactly as they are for a name matched out of the question text
        assert decide_scope(1, 'why?', None, self.rows, [self._turn(aid=424242)]) == ('portfolio', None)

    def test_history_is_normalized_capped_and_junk_tolerant(self):
        assert normalize_history(None) == [] and normalize_history([]) == []
        assert normalize_history(['nope', 42, None, {}, {'question': 'q'}, {'answer': 'a'}]) == []   # a half-turn is not a turn
        assert normalize_history({'question': 'q', 'answer': 'a'}) == [{'question': 'q', 'answer': 'a', 'account_id': None}]
        assert normalize_history([{'question': 'q', 'answer': 'a', 'account_id': 'bogus'}])[0]['account_id'] is None
        many = [{'question': f'q{i}', 'answer': f'a{i}'} for i in range(20)]
        kept = normalize_history(many)
        assert len(kept) == settings.get('conversation', 'max_turns')
        assert kept[-1]['question'] == 'q19'                                  # the newest turns, not the oldest

    def test_strip_prefix_removes_a_leading_disclosure_but_only_that(self):
        assert normalize_history([{'question': 'q', 'answer': '[Test fixture] a'}],
                                  strip_prefix='[Test fixture] ') == [{'question': 'q', 'answer': 'a', 'account_id': None}]
        # no prefix present: left alone
        assert normalize_history([{'question': 'q', 'answer': 'a'}],
                                  strip_prefix='[Test fixture] ') == [{'question': 'q', 'answer': 'a', 'account_id': None}]
        # not synthetic (strip_prefix=None, the real-tenant case): left alone even if it happens to start similarly
        assert normalize_history([{'question': 'q', 'answer': '[Test fixture] a'}]) == \
            [{'question': 'q', 'answer': '[Test fixture] a', 'account_id': None}]

    def test_the_recap_is_capped_and_says_it_is_not_evidence(self):
        assert history_block([]) == ''
        block = history_block(normalize_history([{'question': 'q' * 900, 'answer': 'a' * 900} for _ in range(6)]))
        assert len(block) <= settings.get('conversation', 'max_chars') + len('\n\n')
        assert 'not evidence' in block and 'no citable ids' in block

    def test_the_recap_reaches_the_model_but_can_never_be_cited(self, tenant, monkeypatch):
        """The whole safety property in one test: the model is shown the earlier
        turns, but nothing in them is in `citable`, so a sentence that leans on
        the recap alone is dropped like any other uncited claim."""
        cid, aid, _, _, _ = tenant
        seen = {}

        def _fake(customer_id, system, user):
            seen['system'], seen['user'] = system, user
            return ({'answer_sentences': [
                {'text': 'As I said earlier, the champion left.', 'cites': ['turn:1']},
                {'text': 'As I said earlier, the champion left.', 'cites': []},
            ], 'evidence_gaps': [], 'confidence': 0.9}, 'fake-model')

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', _fake)
        with app.app_context():
            res = ask(cid, 'what about their champion?', account_id=aid,
                      history=[{'question': 'tell me about Northwind Analytics', 'answer': 'Health fell to 48 in March.',
                                'account_id': aid}])
        assert 'CONVERSATION SO FAR' in seen['user'] and 'Health fell to 48 in March.' in seen['user']
        assert seen['user'].index('CONVERSATION SO FAR') < seen['user'].index('QUESTION:')   # before the question it disambiguates
        assert 'The conversation recap is not evidence' in seen['system']
        assert res['sentences'] == []                                        # both dropped: one ghost id, one uncited
        assert {u['reason'] for u in res['unsupported']} == {'unresolved_citation', 'no_citation'}
        assert not any(c.startswith('turn:') for c in res['citations'])
        assert res['history_turns'] == 1

    def test_a_carried_over_follow_up_says_so_in_the_answer(self, tenant):
        cid, aid, _, _, _ = tenant
        with app.app_context():
            first = ask(cid, 'Tell me about Northwind Analytics')
            follow = ask(cid, 'and what about their champion?',
                         history=[{'question': 'Tell me about Northwind Analytics', 'answer': first['answer'],
                                   'account_id': first['scope_detail']['account_id']}])
        assert first['scope_detail']['scope_carried_from_history'] is False and first['history_turns'] == 0
        # without the history this question has no account in it at all and would answer portfolio-wide
        assert follow['scope'] == 'account' and follow['scope_detail']['account_id'] == aid
        assert follow['scope_detail']['scope_carried_from_history'] is True
        assert follow['history_turns'] == 1
        with app.app_context():
            blind = ask(cid, 'and what about their champion?')
        assert blind['scope'] == 'portfolio'                                  # the before picture, in the same test

    def test_the_disclosure_prefix_is_never_doubled_on_a_follow_up(self, tenant, monkeypatch):
        """A synthetic tenant's answer is prefixed with its disclosure label (e.g.
        "[Test fixture] "). That prefixed text is what the client has and sends
        back as history — replayed unstripped, the model imitates the pattern
        it sees in its own history and starts its new answer the same way, which
        then got the mechanical prefix added again on top, doubling it. Covers
        both halves of the fix: the model never sees the prefix in its history
        (so it has no pattern to imitate), and even if it produced the prefix
        anyway, the final answer still carries it exactly once."""
        cid, aid, _, _, _ = tenant
        prior_answer = '[Test fixture] The champion is engaged.'
        seen = {}

        def _fake(customer_id, system, user):
            seen['user'] = user
            return ({'answer_sentences': [
                {'text': '[Test fixture] They confirmed renewal interest.', 'cites': [f'row:{aid}']},
            ], 'evidence_gaps': [], 'confidence': 0.9}, 'fake-model')

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', _fake)
        with app.app_context():
            res = ask(cid, 'what about their champion?', account_id=aid,
                      history=[{'question': 'how is Northwind Analytics doing?', 'answer': prior_answer, 'account_id': aid}])
        assert '[Test fixture]' not in seen['user']                # stripped before the model ever saw it
        assert 'The champion is engaged.' in seen['user']           # the actual content still reached it
        assert res['answer'] == '[Test fixture] They confirmed renewal interest.'
        assert res['answer'].count('[Test fixture]') == 1           # not doubled even though the model produced it anyway

    def test_naming_the_account_again_is_not_reported_as_carried_over(self, tenant):
        cid, aid, _, _, _ = tenant
        with app.app_context():
            res = ask(cid, 'how is Northwind Analytics doing now?',
                      history=[{'question': 'q', 'answer': 'a', 'account_id': aid}])
        assert res['scope_detail']['account_id'] == aid
        assert res['scope_detail']['scope_carried_from_history'] is False     # its own words picked it, not the history

    def test_stub_ranks_a_referring_follow_up_on_the_previous_question_too(self, tenant):
        """No API key: the deterministic path also has to make a follow-up land
        somewhere sensible, or the feature is untestable without a live model."""
        cid, aid, _, _, _ = tenant
        with app.app_context():
            res = ask(cid, 'and after that?', account_id=aid,
                      history=[{'question': 'what happened with the champion change?', 'answer': 'x', 'account_id': aid}])
        assert res['model'] == STUB_MODEL and res['sentences']
        assert any('champion' in s['text'].lower() for s in res['sentences'])


# ── investment context (priority / power_of_1 / roi) ────────────────────

class TestInvestmentContext:
    def test_account_scope_always_shows_priority_and_po1(self, tenant, monkeypatch):
        cid, aid, _, _, _ = tenant
        seen = {}

        def fake_model(customer_id, system, user):
            seen['system'], seen['user'] = system, user
            return {'answer_sentences': [
                {'text': 'This account is currently a protect priority.', 'cites': [f'priority:{aid}']},
                {'text': 'A one-point pillar move has a revenue value on this account.', 'cites': [f'po1:{aid}']},
            ], 'evidence_gaps': [], 'confidence': 0.8}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'is this a protect or grow priority?', account_id=aid)
        assert res['unsupported'] == [], res['unsupported']
        assert f'priority:{aid}' in res['citations'] and f'po1:{aid}' in res['citations']
        assert res['citations'][f'priority:{aid}']['lens'] in ('protect', 'grow')
        assert res['citations'][f'po1:{aid}']['revenue']['value'] == 1_800_000
        assert '[priority]' in seen['user'] and '[po1]' in seen['user']
        assert 'priority:' in seen['system'] and 'po1:' in seen['system'] and 'roi:portfolio' in seen['system']

    def test_account_scope_shows_investment_cost_for_a_cost_question(self, tenant, monkeypatch):
        """investment_cost is gated on the question in account scope, as it already was in portfolio
        scope: it is the largest optional block, and on a non-investment question it bought nothing
        while pushing episodes out of the budget (test_a_plain_account_question_skips_investment_cost)."""
        cid, aid, _, _, _ = tenant
        seen = {}

        def fake_model(customer_id, system, user):
            seen['system'], seen['user'] = system, user
            return {'answer_sentences': [
                {'text': 'The champion-departure playbook costs an estimated $510 to run.', 'cites': [f'investment_cost:{aid}']},
            ], 'evidence_gaps': [], 'confidence': 0.8}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'what would it cost to intervene on this account?', account_id=aid)
        assert res['unsupported'] == [], res['unsupported']
        assert f'investment_cost:{aid}' in res['citations']
        ic = res['citations'][f'investment_cost:{aid}']
        assert ic['vertical'] == 'saas_premium' and ic['basis'] == 'assumed'
        by_id = {p['playbook_id']: p for p in ic['playbooks']}
        assert by_id['champion_departure_sponsor_rebuild']['estimated_cost_per_execution']['value'] == 510.0
        assert by_id['champion_departure_sponsor_rebuild']['estimated_cost_per_execution']['basis'] == 'assumed'
        assert '[investment_cost]' in seen['user']
        assert 'investment_cost:' in seen['system'] and 'assumed' in seen['system']

    def test_a_plain_account_question_skips_investment_cost(self, tenant, monkeypatch):
        cid, aid, _, _, _ = tenant
        seen = {}

        def fake_model(customer_id, system, user):
            seen['user'] = user
            return {'answer_sentences': [], 'evidence_gaps': [], 'confidence': 0.5}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'walk me through what went wrong at this account', account_id=aid)
        assert '[investment_cost]' not in seen['user']
        assert f'investment_cost:{aid}' not in res['citations']
        assert '[episode]' in seen['user']                     # the budget it freed goes to the evidence

    def test_row_now_carries_priority_the_piece_a_fix(self, tenant):
        cid, aid, bid, _, _ = tenant
        with app.app_context():
            from journeys.read import list_journeys
            rows = list_journeys(cid)
        assert all('priority' in r for r in rows)                             # list_journeys already attached it
        assert {r['account_id'] for r in rows if r.get('priority')} == {aid, bid}
        with app.app_context():
            res = ask(cid, 'which accounts are most at risk?')                # portfolio, stub mode
        row_cite = next(iter(res['citations'].values()))
        assert 'priority' in row_cite                                        # now reaches the model, not stripped by _row_compact

    def test_portfolio_investment_question_pulls_aggregate_blocks(self, tenant, monkeypatch):
        cid, *_ = tenant
        seen = {}

        def fake_model(customer_id, system, user):
            seen['user'] = user
            return {'answer_sentences': [{'text': 'The portfolio has protect and grow exposure.', 'cites': ['priority:portfolio']}],
                    'evidence_gaps': [], 'confidence': 0.6}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'which account has the highest investment priority?')
        assert 'priority:portfolio' in res['citations']
        assert '[priority_portfolio]' in seen['user'] and '[po1_portfolio]' in seen['user'] and '[roi_portfolio]' in seen['user']
        assert '[investment_cost_portfolio]' in seen['user']

    def test_portfolio_cost_question_pulls_investment_cost(self, tenant, monkeypatch):
        """'cost'/'how much' are new gating phrases (config/ask_ai.json scope.investment_phrases) added
        alongside investment_cost — a plain cost question must trigger the same four-block gate as an
        'investment priority' question does, not just the phrases that predate this block."""
        cid, *_ = tenant
        seen = {}

        def fake_model(customer_id, system, user):
            seen['user'] = user
            return {'answer_sentences': [{'text': 'Running the relevant playbooks costs an estimated amount per execution.',
                                          'cites': ['investment_cost:portfolio']}], 'evidence_gaps': [], 'confidence': 0.6}, 'fake-model'

        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model', fake_model)
        with app.app_context():
            res = ask(cid, 'how much would it cost to fix the accounts at risk?')
        assert 'investment_cost:portfolio' in res['citations']
        assert '[investment_cost_portfolio]' in seen['user']

    def test_portfolio_plain_question_skips_investment_blocks(self, tenant, monkeypatch):
        cid, *_ = tenant
        seen = {}
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
        monkeypatch.setattr('ask_ai.answer._call_model',
                            lambda c, s, u: (seen.update(user=u) or {'answer_sentences': [], 'evidence_gaps': [], 'confidence': 0.5}, 'fake-model'))
        with app.app_context():
            ask(cid, 'which accounts are most at risk?')
        assert '[priority_portfolio]' not in seen['user'] and '[po1_portfolio]' not in seen['user'] and '[roi_portfolio]' not in seen['user']
        assert '[investment_cost_portfolio]' not in seen['user']

    def test_missing_economics_degrades_to_evidence_gap_not_a_crash(self, tenant, monkeypatch):
        """account_context() directly, not ask() — the stub never cites priority:/po1: (it doesn't
        know about them), so going through ask() in stub mode can't prove ctx actually carries them."""
        cid, aid, *_ = tenant
        import roi.settings as roi_settings
        from ask_ai.answer import account_context

        def boom(vertical):
            raise roi_settings.EconomicsConfigError(f'no economics file for vertical {vertical!r} (test)')
        monkeypatch.setattr(roi_settings, 'economics', boom)
        with app.app_context():
            ctx, gaps, meta, narrative = account_context(cid, aid, 'what happened?', None, None)
        assert any('power_of_1 not available' in g for g in gaps)
        assert f'priority:{aid}' in ctx.citable                               # investment_priorities doesn't need economics — unaffected
        assert f'po1:{aid}' not in ctx.citable

    def test_missing_investment_file_degrades_to_evidence_gap_not_a_crash(self, tenant, monkeypatch):
        """Same shape as the missing-economics test above, for roi.investment.investment_cost(): a
        missing/invalid config/investment/<vertical>.json must not crash Ask AI — priority/po1/roi are
        unaffected (a different config file), only investment_cost degrades to an evidence_gap."""
        cid, aid, *_ = tenant
        import roi.settings as roi_settings
        from ask_ai.answer import account_context

        def boom(vertical):
            raise roi_settings.InvestmentConfigError(f'no investment file for vertical {vertical!r} (test)')
        monkeypatch.setattr(roi_settings, 'investment', boom)
        with app.app_context():
            # a cost-shaped question: account scope only reaches investment_cost when the question asks for it
            ctx, gaps, meta, narrative = account_context(cid, aid, 'what would this cost?', None, None)
        assert any('investment cost not available' in g for g in gaps)
        assert f'priority:{aid}' in ctx.citable and f'po1:{aid}' in ctx.citable    # unaffected — different config file
        assert f'investment_cost:{aid}' not in ctx.citable

    def test_new_curated_questions_resolve_scope_and_dont_crash(self, tenant):
        """Stub-mode smoke test only: the stub never cites the new aggregate blocks (it doesn't know
        about them), so this proves scope resolution and no crash — real content quality is what
        scripts/eval_ask_ai_questions.py against a real model checks, per established practice."""
        cid, aid, *_ = tenant
        import json as _json
        from pathlib import Path
        qs = _json.loads(Path(__file__).resolve().parent.parent.joinpath('config/ask_ai_questions.json').read_text())
        new_ids = {'cfo-13', 'cfo-14', 'cfo-15', 'cro-13', 'cro-14', 'cfo-16', 'cfo-17', 'cro-15'}
        found = {q['id']: q for role in ('cfo', 'cro') for q in qs[role] if q['id'] in new_ids}
        assert set(found) == new_ids
        for qid, q in found.items():
            with app.app_context():
                res = ask(cid, q['text'], account_id=aid if q['scope'] == 'account' else None)
            assert res['scope'] == q['scope'], qid
            assert res['model'] == STUB_MODEL, qid


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

    def test_mcp_tool_registered_and_keyed(self, tenant, monkeypatch):
        from mcp_server.cs_pulse_mcp_server import mcp
        from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
        assert 'ask' in KEYED_TOOLS and 'ask' not in ONBOARDING_TOOLS      # a key is required over HTTP
        tool = asyncio.run(mcp.get_tool('ask'))
        assert tool is not None and 'question' in tool.parameters['properties']
        cid, aid, *_ = tenant
        from mcp_server.cs_pulse_onboarding import ask as ask_tool
        from fastmcp.exceptions import ToolError
        import mcp_server.auth as auth
        monkeypatch.setenv('MCP_TRANSPORT', 'http')
        monkeypatch.setattr(auth, 'MCP_AUTH_REQUIRED', True)
        monkeypatch.setattr(auth, 'MCP_SERVER_API_KEY', 'srv-test-key')
        with pytest.raises(ToolError, match='requires an API key'):
            ask_tool(cid, 'what happened with the champion?', account_id=aid)          # anonymous over HTTP: denied
        tok = auth._current_api_key_var.set('srv-test-key')
        try:
            res = ask_tool(cid, 'what happened with the champion?', account_id=aid)
            assert res['model'] == STUB_MODEL and res['sentences']
            with pytest.raises(ToolError):
                ask_tool(cid, 'x', account_id=999999)
        finally:
            auth._current_api_key_var.reset(tok)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
