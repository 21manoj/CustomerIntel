"""
Ask AI money answers without a model, on real Postgres (the CFO question that came back as one
account's story instead of playbook costs):
  * a cost question on a revenue tenant is answered from the cost data: each playbook's cost per run and
    cost per health point, best first; the answer is labelled "no model"
  * an exposure-basis tenant gets exposure blocks (exposure:portfolio, exposure:<account>) and exposure
    answers; cost sits beside exposure and is never divided into a return
  * when the money data cannot be built and the question asked for it, the answer leads with that in plain
    words: no file names, no module paths
  * an account picked with a portfolio-worded question: the answer says which scope ran
  * every suggested question, the platform set and each vertical's own set, runs without a model and
    without error; portfolio-scope ones run portfolio-wide; no file paths in the answer or the gaps
"""
import os
import re
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.pop('ANTHROPIC_API_KEY', None)

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()
from models import Account, HealthScore                    # noqa: E402
from ask_ai import settings                                # noqa: E402
from ask_ai.answer import ask, STUB_MODEL, STUB_LABEL      # noqa: E402
from utils.vertical_registry import capability             # noqa: E402

COST_Q = 'Across the portfolio, what does each playbook cost to run, and which gives the most health-point lift per dollar?'
PATHISH = re.compile(r'\.json|\.py\b|config/|backend/|roi\.|Traceback|Error\(')


def _verticals():
    return sorted(p.name[:-len('_kpi_catalog.json')] for p in (BACKEND / 'config').glob('*_kpi_catalog.json'))


def _by_basis(basis):
    return next(v for v in _verticals() if capability(v, 'money_basis') == basis
                and (BACKEND / 'config' / 'investment' / f'{v}.json').is_file())


def _tenant(vertical, accounts):
    from mcp_server.cs_pulse_onboarding import create_customer
    tag = uuid.uuid4().hex[:8]
    cid = create_customer(name=f'AskMoney {tag}', domain=f'askmoney-{tag}.test', vertical=vertical,
                          admin_email=f'askmoney_{tag}@t.test', admin_name='P', data_origin='synthetic_test')['customer_id']
    ids = []
    for name, revenue, profile, health in accounts:
        a = Account(customer_id=cid, account_name=name, revenue=revenue, vertical=vertical, profile_metadata=profile)
        db.session.add(a)
        db.session.flush()
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=date(2026, 5, 1), health_score=health + 6))
        db.session.add(HealthScore(account_id=a.account_id, measurement_month=date(2026, 6, 1), health_score=health))
        ids.append(a.account_id)
    db.session.commit()
    from journeys.wizard_a import run_wizard_a
    run_wizard_a(cid, evaluate_playbooks=False)          # the portfolio rows Ask AI reads
    return cid, ids


@pytest.fixture(scope='module')
def tenants():
    with app.app_context():
        db.create_all()
        rev = _tenant(_by_basis('revenue'), [('Northwind Analytics', 1_800_000, {'renewal_date': '2026-12-01'}, 48),
                                             ('Contoso Freight', 400_000, {}, 82)])
        exp = _tenant(_by_basis('exposure'), [
            ('Core Processor', 1_900_000, {'criticality_tier': 'critical', 'holds_customer_data': True}, 45),
            ('Statement Printer', 60_000, {'criticality_tier': 'standard'}, 55),
            ('Untiered Vendor', 10_000, {}, 70)])
        yield {'revenue': rev, 'exposure': exp}
        db.session.remove()


def _clean(res):
    text = ' '.join([res['answer']] + [str(g) for g in res['evidence_gaps']] + list(res.get('notices') or []))
    assert not PATHISH.search(text), text


def test_cost_question_on_a_revenue_tenant_is_answered_from_the_cost_data(tenants):
    cid, _ = tenants['revenue']
    with app.app_context():
        res = ask(cid, COST_Q)
    assert res['scope'] == 'portfolio'
    assert res['model'] == STUB_MODEL and res['model_label'] == STUB_LABEL
    assert 'per health point' in res['answer'] and 'per run' in res['answer'] and '$' in res['answer']
    assert 'gives the most health-point lift per dollar' in res['answer']
    assert 'investment_cost:portfolio' in res['citations']
    assert res['unsupported'] == []
    _clean(res)


def test_revenue_at_risk_question_is_answered_from_revenue_figures_not_the_cost_list(tenants):
    """Found on a 20-account validation tenant: every money question on a revenue tenant got the playbook-cost list."""
    cid, ids = tenants['revenue']
    with app.app_context():
        res = ask(cid, 'How much revenue is at risk across the portfolio, and which accounts carry most of it?')
        one = ask(cid, 'How much revenue is at risk on this account?', account_id=ids[0])
        both = ask(cid, 'Across the portfolio, how much revenue is at risk and what does each playbook cost?')
    assert res['scope'] == 'portfolio' and res['model'] == STUB_MODEL
    assert res['answer'].replace('[Test fixture] ', '').startswith('Revenue at risk across 2 accounts is $')
    assert 'Northwind Analytics' in res['answer'] and 'carrying the most of it' in res['answer']
    assert 'per run' not in res['answer']
    assert 'po1:portfolio' in res['citations'] and res['unsupported'] == []
    assert 'at risk' in one['answer'] and 'per run' not in one['answer'] and one['unsupported'] == []
    assert f'po1:{ids[0]}' in one['citations']
    assert 'Revenue at risk' in both['answer'] and 'per run' in both['answer'] and both['unsupported'] == []
    for r in (res, one, both):
        _clean(r)


def test_the_picker_does_not_change_a_cost_answer_but_the_scope_is_stated(tenants):
    cid, ids = tenants['revenue']
    with app.app_context():
        res = ask(cid, COST_Q, account_id=ids[0])
    assert res['scope'] == 'account'
    assert res['notices'] and 'Northwind Analytics only' in res['notices'][0]
    assert 'no account picked' in res['notices'][0]
    assert res['notices'][0] in res['answer'] and res['answer'].index(res['notices'][0]) < res['answer'].index('per run')
    assert res['scope_detail']['scope_mismatch'] == 'portfolio_wording_account_picked'
    with app.app_context():
        plain = ask(cid, 'what happened on this account recently?', account_id=ids[0])
    assert plain['notices'] == [] and 'scope_mismatch' not in plain['scope_detail']


def test_exposure_tenant_gets_exposure_blocks_and_answers(tenants):
    cid, ids = tenants['exposure']
    with app.app_context():
        res = ask(cid, 'What is the total expected annual exposure across the portfolio, split by tier?')
        one = ask(cid, 'What is the exposure on this one?', account_id=ids[0])
        none = ask(cid, 'What is the exposure on this one?', account_id=ids[2])
        cost = ask(cid, COST_Q)
    assert 'exposure:portfolio' in res['citations'] and not any(k.startswith('po1:') or k.startswith('roi:') for k in res['citations'])
    assert 'expected annual exposure' in res['answer'].lower() and '$' in res['answer']
    assert 'critical' in res['answer'].lower()
    assert '1 because no tier is recorded' in res['answer'] or 'could not be priced' in res['answer']
    assert f'exposure:{ids[0]}' in one['citations'] and 'Core Processor' in one['answer'] and '$' in one['answer']
    assert 'could not be priced' in none['answer'] and 'no tier is recorded' in none['answer'] and 'no_tier' not in none['answer']
    assert 'per run' in cost['answer']
    assert 'not divided into a return' in cost['answer'] or 'No playbook has been run yet' in cost['answer']
    for r in (res, one, none, cost):
        assert r['model_label'] == STUB_LABEL and r['unsupported'] == []
        _clean(r)


def test_missing_money_data_leads_the_answer_in_plain_words(tenants, monkeypatch):
    cid, _ = tenants['revenue']

    def boom(*a, **k):
        raise FileNotFoundError('/srv/app/backend/config/investment/whatever.json is missing')

    monkeypatch.setattr('roi.investment.investment_cost', boom)
    monkeypatch.setattr('roi.power_of_1.power_of_1', boom)
    monkeypatch.setattr('roi.measured.roi', boom)
    with app.app_context():
        res = ask(cid, COST_Q)
    assert res['notices'], res
    assert res['answer'].replace('[Test fixture] ', '').startswith(res['notices'][0])
    assert 'not set up for this tenant' in res['notices'][0]
    assert 'Playbook cost figures' in ' '.join(res['notices'])
    _clean(res)


def _question_cases():
    out = []
    for v in _verticals():
        for role, qs in settings.curated_questions(v).items():
            for q in qs:
                out.append((v, q['id'], q['scope'], q['text']))
    return out


def test_every_suggested_question_runs_without_a_model(tenants):
    """Each question of each vertical's set, asked of a tenant of the same money basis. A portfolio
    question is asked WITH an account picked on the wire being absent (what the page now sends)."""
    seen = set()
    failures = []
    with app.app_context():
        for vertical, qid, scope, text in _question_cases():
            basis = capability(vertical, 'money_basis')
            if (basis, qid) in seen:
                continue
            seen.add((basis, qid))
            cid, ids = tenants[basis]
            try:
                res = ask(cid, text, account_id=ids[0] if scope == 'account' else None)
            except Exception as e:                       # noqa: BLE001
                failures.append(f'{qid}: raised {type(e).__name__}: {e}')
                continue
            text_out = ' '.join([res['answer']] + [str(g) for g in res['evidence_gaps']])
            if scope == 'portfolio' and res['scope'] != 'portfolio':
                failures.append(f'{qid}: portfolio question ran as {res["scope"]}')
            if scope == 'account' and res['scope'] != 'account':
                failures.append(f'{qid}: account question ran as {res["scope"]}')
            if PATHISH.search(text_out):
                failures.append(f'{qid}: file path or module name in the answer: {text_out[:200]}')
            if res['model_label'] != STUB_LABEL:
                failures.append(f'{qid}: not labelled as no-model')
            if res['unsupported']:
                failures.append(f'{qid}: unsupported sentences {res["unsupported"]}')
    assert len(seen) > 20
    assert not failures, '\n'.join(failures)
