"""
Playbook autonomy: automation levels 0-4, multi-step plans, the always-human rules, review afterwards,
automatic lowering, the stop switch and raise suggestions (docs/design/playbook-governance-layer.md, "Steps and autonomy levels"),
on a real Postgres tenant whose playbooks are patched in (no vertical file is changed):

  * effects / steps / always_human validate at load; the shipped vertical files carry them
  * a level above the platform ceiling, or any level for an always-human class, is refused; changes are logged
  * what policy approves at each level (0 nothing, 1 declared-auto notify, 2 notify+schedule once the plan was
    approved by a person, 3 notify+schedule, 4 escalate too with a review afterwards)
  * offers, money/contract/exit effects and flagged accounts wait for a person at every level
  * a plan runs step by step: policy sends the steps the level covers, the others wait for a person
  * a decline or a failure lowers that action class by one level and withdraws the plan approval
  * the stop switch halts the next step and every approval
  * a clean run of approvals by people produces a suggestion to raise, never a raise
"""
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app                # noqa: E402
app = get_flask_app()
from models import Account, ContextNode, Intervention, JourneyData, ToolAuditLog   # noqa: E402

VERTICAL = 'saas_premium'
PERSON = {'key_kind': 'user', 'key_record': None, 'key_id': None, 'label': 'user:42'}


@pytest.fixture(scope='module', autouse=True)
def _schema():
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def _defs(code):
    trig = {'kpi': [{'code': code, 'op': '<', 'value': 50}], 'urgency': 'high'}
    eo = {'types': ['escalation_resolved'], 'window_days': 30}
    base = lambda pid, ac, ap='human', **kw: {'id': pid, 'label': pid.replace('_', ' '), 'action_class': ac, 'approval': ap,
                                             'expected_outcome': eo, 'trigger': trig, **kw}
    return [
        base('note_auto', 'notify', 'auto'),
        base('note_human', 'notify'),
        base('sched_one', 'schedule'),
        base('esc_one', 'escalate'),
        base('offer_one', 'offer'),
        base('money_sched', 'schedule', effects=['money']),
        base('plan3', 'schedule', steps=[
            {'id': 'remind', 'label': 'Remind the owner', 'action_class': 'notify'},
            {'id': 'meet', 'label': 'Book the review', 'action_class': 'schedule'},
            {'id': 'exec', 'label': 'Escalate to the sponsor', 'action_class': 'escalate'},
        ]),
    ]


@pytest.fixture()
def auto(monkeypatch):
    """A fresh tenant with four accounts that all breach one KPI, and seven patched-in playbooks that all fire
    on it. `version` is a one-item list so a test can publish a new playbook version."""
    from models import KPIMeasurement
    from playbooks import definitions
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_kpis
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Autonomy {tag}', domain=f'auto-{tag}.test', vertical=VERTICAL,
                              admin_email=f'auto_{tag}@t.test', admin_name='A', data_origin='synthetic_test')['customer_id']
        code = sorted(get_kpis(VERTICAL))[0]
        accts = {}
        for name, pm in [('one', {}), ('two', {}), ('three', {}), ('flagged', {'regulated': 'yes'})]:
            a = Account(customer_id=cid, account_name=f'Acct {name}', revenue=100_000, vertical=VERTICAL, profile_metadata=pm)
            db.session.add(a); db.session.commit()
            db.session.add(KPIMeasurement(account_id=a.account_id, kpi_code=code, value=30, measured_at=datetime(2026, 9, 1)))
            accts[name] = a.account_id
        db.session.commit()
        gov, tax, seen = definitions.governance(), get_taxonomy(VERTICAL), set()
        pbs = [definitions._validate_playbook(p, VERTICAL, tax, gov, seen) for p in _defs(code)]
        version = ['1.0']
        real = definitions.load_vertical
        monkeypatch.setattr(definitions, 'load_vertical', lambda v: (
            {'vertical': v, 'version': version[0], 'playbooks': pbs, 'source': 'test', 'always_human': {'account_flags': ['regulated']}}
            if v == VERTICAL else real(v)))
        from journeys.wizard_a import run_wizard_a
        run_wizard_a(cid, evaluate_playbooks=False)
        for jd in JourneyData.query.filter_by(customer_id=cid).all():
            jd.journey_json = {**(jd.journey_json or {}), 'as_of': '2026-10-01T00:00:00'}
        db.session.commit()
        yield cid, accts, version
        db.session.remove()


def _row(cid, aid, pid):
    return Intervention.query.filter_by(customer_id=cid, account_id=aid, playbook_id=pid).order_by(Intervention.id.desc()).first()


def _audits(cid, transition):
    return ToolAuditLog.query.filter_by(customer_id=cid, tool=f'intervention.{transition}').order_by(ToolAuditLog.id).all()


def _by_policy(cid, aid):
    from playbooks.governance import evaluate
    out = evaluate(cid, account_id=aid, dry_run=True)
    return {p['playbook_id']: p['approval_policy'] for p in out['proposed']}


# ── definitions ───────────────────────────────────────────────────────

def test_effects_steps_and_always_human_validate():
    from playbooks.definitions import (_validate_playbook, _validate_always_human, governance, load_vertical,
                                       PlaybookConfigError)
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_kpis
    with app.app_context():
        gov, tax = governance(), get_taxonomy(VERTICAL)
        code = sorted(get_kpis(VERTICAL))[0]
        good = _defs(code)[-1]
        ok = _validate_playbook(dict(good), VERTICAL, tax, gov, set())
        assert [s['id'] for s in ok['steps']] == ['remind', 'meet', 'exec'] and ok['effects'] == []
        assert ok['steps'][0] == {'id': 'remind', 'label': 'Remind the owner', 'action_class': 'notify', 'effects': []}
        two = good['steps'][:2]
        for change, msg in [
            ({'effects': ['refund']}, 'effects'),
            ({'steps': good['steps'][:1]}, 'steps must be a list of 2'),
            ({'steps': two * 5}, 'steps must be a list of 2'),
            ({'steps': [two[0], dict(two[0])]}, 'duplicate step id'),
            ({'steps': [two[0], {'id': 'x', 'action_class': 'phone'}]}, 'action_class'),
            ({'steps': [two[0], {'action_class': 'notify'}]}, 'needs a string id'),
            ({'steps': [two[0], {'id': 'x', 'action_class': 'notify', 'effects': ['gift']}]}, 'effects'),
            ({'approval': 'auto', 'action_class': 'notify', 'steps': two}, 'approval=auto needs every step'),
        ]:
            with pytest.raises(PlaybookConfigError, match=msg):
                _validate_playbook({**good, **change}, VERTICAL, tax, gov, set())
        assert _validate_always_human(None, 'v') == {'account_flags': []}
        with pytest.raises(PlaybookConfigError, match='account_flags'):
            _validate_always_human({'account_flags': 'regulated'}, 'v')

        # the shipped files: the vendor vertical keeps regulated services and exit work with a person and has
        # a three-step plan; the SaaS vertical has a two-step plan and no flags
        tprm = load_vertical('tprm_v1')
        by = {p['id']: p for p in tprm['playbooks']}
        assert tprm['always_human'] == {'account_flags': ['regulated_critical_service']}
        assert by['exit_readiness']['effects'] == ['exit']
        assert [s['action_class'] for s in by['remediation_overdue']['steps']] == ['notify', 'schedule', 'escalate']
        saas = load_vertical('saas_premium')
        assert saas['always_human'] == {'account_flags': []}
        assert [s['action_class'] for s in {p['id']: p for p in saas['playbooks']}['adoption_drop_renewal_review']['steps']] == ['notify', 'schedule']


def test_levels_are_validated_against_the_ceiling_and_changes_are_logged(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant, tenant_config, ceiling_for
        cfg = tenant_config(cid)
        assert cfg['automation_level'] == 0 and cfg['automation_levels_by_class'] == {} and cfg['ceiling'] == 4
        assert cfg['effective_levels'] == {'notify': 0, 'schedule': 0, 'escalate': 0, 'offer': 0}
        assert ceiling_for('offer') == 0 and ceiling_for('escalate') == 4
        for kw, msg in [({'automation_level': 5}, 'automation_level must be one of'),
                        ({'automation_levels_by_class': {'offer': 1}}, 'above the platform ceiling for that class'),
                        ({'automation_levels_by_class': {'phone': 1}}, 'unknown action class'),
                        ({'automation_levels_by_class': {'notify': 9}}, 'must be one of'),
                        ({'automation_levels_by_class': ['notify']}, 'must be an object')]:
            with pytest.raises(ValueError, match=msg):
                configure_tenant(cid, **kw)
        cfg = configure_tenant(cid, automation_level=3, automation_levels_by_class={'schedule': 1}, changed_by='user:42')
        assert cfg['effective_levels'] == {'notify': 3, 'schedule': 1, 'escalate': 3, 'offer': 0}
        cfg = configure_tenant(cid, automation_levels_by_class={'schedule': None}, kill_switch=True, changed_by='user:42')
        assert cfg['effective_levels']['schedule'] == 3 and cfg['kill_switch'] is True
        events = [(e['event'], e.get('by')) for e in cfg['autonomy_log']]
        assert events == [('level_set', 'user:42'), ('class_level_set', 'user:42'), ('class_level_cleared', 'user:42'),
                          ('stop_switch_on', 'user:42')]


# ── what policy approves at each level ────────────────────────────────

def test_policy_at_each_level_and_the_always_human_rules(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant
        expect = {
            0: set(),
            1: {'note_auto'},
            2: {'note_auto'},                                             # nothing else until a person approves a plan
            3: {'note_auto', 'note_human', 'sched_one', 'plan3'},          # plan3's first step is a notification
            4: {'note_auto', 'note_human', 'sched_one', 'plan3', 'esc_one'},
        }
        for level, auto_ids in expect.items():
            configure_tenant(cid, automation_level=level)
            got = _by_policy(cid, accts['one'])
            assert set(got) == {'note_auto', 'note_human', 'sched_one', 'esc_one', 'offer_one', 'money_sched', 'plan3'}
            assert {pid for pid, d in got.items() if d['by_policy']} == auto_ids, level
            assert got['offer_one']['reason'] == 'always_human:action_class:offer'
            assert got['money_sched']['reason'] == 'always_human:effect:money'
        assert got['esc_one'] == {'by_policy': True, 'level': 4, 'reason': 'level_4_covers:escalate', 'review_afterwards': True}
        assert got['sched_one']['review_afterwards'] is False
        flagged = _by_policy(cid, accts['flagged'])                      # still level 4
        assert not any(d['by_policy'] for d in flagged.values())
        assert flagged['note_auto']['reason'] == 'always_human:account_flag:regulated'
        configure_tenant(cid, automation_level=2)
        got = _by_policy(cid, accts['one'])
        assert got['sched_one']['reason'] == 'level_2_needs_the_plan_approved_once'
        assert got['esc_one']['reason'] == 'level_2_does_not_cover:escalate'
        configure_tenant(cid, automation_level=1)
        assert _by_policy(cid, accts['one'])['note_human']['reason'] == 'level_1_covers_only_playbooks_declared_auto'
        assert Intervention.query.filter_by(customer_id=cid).count() == 0   # dry runs wrote nothing


def test_level_2_waits_for_one_plan_approval_per_playbook_version(auto):
    cid, accts, version = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant, tenant_config
        from playbooks.governance import evaluate, approve
        configure_tenant(cid, automation_level=2, disabled_playbooks=['note_auto', 'note_human', 'esc_one', 'offer_one', 'money_sched', 'plan3'])
        out = evaluate(cid, account_id=accts['one'])
        assert out['auto_approved'] == [] and _row(cid, accts['one'], 'sched_one').state == 'proposed'
        approve(cid, _row(cid, accts['one'], 'sched_one').id, note='plan looks right', actor=PERSON)
        plans = tenant_config(cid)['approved_plans']
        assert plans['sched_one']['version'] == '1.0' and plans['sched_one']['by'] == 'user:42'
        assert len(_audits(cid, 'plan_approved')) == 1

        out = evaluate(cid, account_id=accts['two'])                     # the same plan, another account: policy
        row = _row(cid, accts['two'], 'sched_one')
        assert [a['intervention_id'] for a in out['auto_approved']] == [row.id]
        assert row.state == 'sent' and row.approved_by == 'policy:automation_level_2' and not row.review_required
        assert row.approval_policy['reason'] == 'level_2_covers:schedule'
        assert len(_audits(cid, 'plan_approved')) == 1                   # policy's approval is not a plan approval

        version[0] = '1.1'                                               # the playbook changed: ask a person again
        out = evaluate(cid, account_id=accts['three'])
        assert out['auto_approved'] == [] and _row(cid, accts['three'], 'sched_one').approval_policy['reason'] == 'level_2_needs_the_plan_approved_once'


def test_level_4_escalates_by_policy_and_marks_it_for_review(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant
        from playbooks.governance import evaluate, approve, review, list_interventions
        configure_tenant(cid, automation_level=4, disabled_playbooks=['note_auto', 'note_human', 'sched_one', 'money_sched', 'plan3'])
        out = evaluate(cid, account_id=accts['one'])
        esc, offer = _row(cid, accts['one'], 'esc_one'), _row(cid, accts['one'], 'offer_one')
        assert out['auto_approved'] == [{'intervention_id': esc.id, 'delivery': esc.delivery, 'review_afterwards': True}]
        assert esc.state == 'sent' and esc.review_required is True and offer.state == 'proposed'
        li = list_interventions(cid)
        assert li['awaiting_review'] == [esc.id] and li['awaiting_approval'] == [offer.id]
        v = review(cid, esc.id, note='right call', actor=PERSON)
        assert v['review'] == {**v['review'], 'required': True, 'pending': False, 'reviewed_by': 'user:42'}
        assert len(_audits(cid, 'reviewed')) == 1 and list_interventions(cid)['awaiting_review'] == []
        with pytest.raises(ValueError, match='already reviewed'):
            review(cid, esc.id, actor=PERSON)
        approve(cid, offer.id, actor=PERSON)                             # a person approved it: nothing to review
        with pytest.raises(ValueError, match='not marked for review'):
            review(cid, offer.id, actor=PERSON)

        evaluate(cid, account_id=accts['flagged'])                       # a flagged account stays with a person
        assert _row(cid, accts['flagged'], 'esc_one').state == 'proposed'
        assert _row(cid, accts['flagged'], 'esc_one').approval_policy['reason'] == 'always_human:account_flag:regulated'


# ── multi-step plans ──────────────────────────────────────────────────

def _only_plan3(cid, level):
    from playbooks.definitions import configure_tenant
    configure_tenant(cid, automation_level=level, disabled_playbooks=['note_auto', 'note_human', 'sched_one', 'esc_one', 'offer_one', 'money_sched'])


def test_a_plan_runs_step_by_step_policy_where_the_level_covers_it(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.governance import evaluate, approve, report, row_view
        _only_plan3(cid, 3)
        evaluate(cid, account_id=accts['one'])
        row = _row(cid, accts['one'], 'plan3')
        v = row_view(row)
        assert row.state == 'sent' and v['step'] == {'number': 1, 'of': 3, 'id': 'remind', 'label': 'Remind the owner',
                                                     'action_class': 'notify', 'state': 'sent'}
        assert [s['state'] for s in row.steps] == ['sent', 'pending', 'pending'] and v['awaiting_approval'] is False

        res = report(cid, row.id, 'done', note='owner reminded', actor=PERSON)      # step 2 is a schedule: level 3 covers it
        assert res['advanced_to'] == {'step': 'meet', 'number': 2, 'of': 3, 'by_policy': True, 'reason': 'level_3_covers:schedule'}
        row = _row(cid, accts['one'], 'plan3')
        assert row.state == 'sent' and [s['state'] for s in row.steps] == ['done', 'sent', 'pending']
        assert row.steps[1]['approved_by'] == 'policy:automation_level_3'

        res = report(cid, row.id, 'done', actor=PERSON)                              # step 3 is an escalation: a person
        assert res['advanced_to']['by_policy'] is False and res['advanced_to']['reason'] == 'level_3_does_not_cover:escalate'
        assert res['awaiting_approval'] is True and res['state'] == 'sent' and res['step']['state'] == 'awaiting_approval'
        with pytest.raises(ValueError, match='awaiting approval'):
            report(cid, row.id, 'done', actor=PERSON)
        sent = approve(cid, row.id, note='go', actor=PERSON)
        assert sent['payload']['step'] == {'number': 3, 'of': 3, 'id': 'exec', 'label': 'Escalate to the sponsor', 'action_class': 'escalate'}
        assert sent['payload']['review_afterwards'] is False and sent['step']['state'] == 'sent'
        with pytest.raises(ValueError, match='only a proposed one'):
            approve(cid, row.id, actor=PERSON)

        res = report(cid, row.id, 'done', outcome_type='escalation_resolved', actor=PERSON)
        row = _row(cid, accts['one'], 'plan3')
        assert row.state == 'closed' and row.closed_state == 'done' and [s['state'] for s in row.steps] == ['done', 'done', 'done']
        assert res['outcome']['expected'] is True and res['autonomy_lowered'] is None
        nodes = ContextNode.query.filter_by(customer_id=cid, node_type='INTERVENTION').all()
        assert len(nodes) == 1 and nodes[0].properties['step'] == 'exec' and nodes[0].properties['steps_total'] == 3
        assert len(_audits(cid, 'step_done')) == 2 and len(_audits(cid, 'approve')) == 3


def test_an_outcome_reported_mid_plan_closes_it(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.governance import evaluate, report
        _only_plan3(cid, 3)
        evaluate(cid, account_id=accts['one'])
        row = _row(cid, accts['one'], 'plan3')
        report(cid, row.id, 'done', outcome_type='escalation_resolved', actor=PERSON)
        row = _row(cid, accts['one'], 'plan3')
        assert row.state == 'closed' and [s['state'] for s in row.steps] == ['done', 'skipped', 'skipped']


# ── automatic lowering ────────────────────────────────────────────────

def test_declining_a_waiting_step_lowers_that_class_only(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import tenant_config
        from playbooks.governance import evaluate, report
        _only_plan3(cid, 3)
        evaluate(cid, account_id=accts['one'])
        row = _row(cid, accts['one'], 'plan3')
        report(cid, row.id, 'done', actor=PERSON)
        report(cid, row.id, 'done', actor=PERSON)                        # now waiting on the escalation
        res = report(cid, row.id, 'cancelled', note='not this vendor', actor=PERSON)
        assert res['autonomy_lowered'] == {'action_class': 'escalate', 'from': 3, 'to': 2, 'reason': 'declined',
                                           'plan_approval_withdrawn': None}
        row = _row(cid, accts['one'], 'plan3')
        assert row.closed_state == 'cancelled' and [s['state'] for s in row.steps] == ['done', 'done', 'declined']
        cfg = tenant_config(cid)
        assert cfg['automation_level'] == 3 and cfg['automation_levels_by_class'] == {'escalate': 2}
        assert cfg['effective_levels'] == {'notify': 3, 'schedule': 3, 'escalate': 2, 'offer': 0}
        last = cfg['autonomy_log'][-1]
        assert last['event'] == 'lowered' and last['reason'] == 'declined' and f'#{row.id}' in last['detail']
        assert len(_audits(cid, 'autonomy_lowered')) == 1 and len(_audits(cid, 'declined')) == 1


def test_a_failure_lowers_the_class_and_withdraws_the_plan_approval(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant, tenant_config
        from playbooks.governance import evaluate, approve, report
        configure_tenant(cid, automation_level=2, disabled_playbooks=['note_auto', 'note_human', 'esc_one', 'offer_one', 'money_sched', 'plan3'])
        evaluate(cid, account_id=accts['one'])
        row = _row(cid, accts['one'], 'sched_one')
        approve(cid, row.id, actor=PERSON)
        assert 'sched_one' in tenant_config(cid)['approved_plans']
        res = report(cid, row.id, 'failed', note='the meeting never happened', actor=PERSON)
        assert res['autonomy_lowered'] == {'action_class': 'schedule', 'from': 2, 'to': 1, 'reason': 'failed',
                                           'plan_approval_withdrawn': 'sched_one'}
        cfg = tenant_config(cid)
        assert cfg['approved_plans'] == {} and cfg['effective_levels']['schedule'] == 1 and cfg['effective_levels']['notify'] == 2
        out = evaluate(cid, account_id=accts['two'])                     # nothing goes by policy any more
        assert out['auto_approved'] == [] and _row(cid, accts['two'], 'sched_one').state == 'proposed'

        # at the floor nothing more is lowered; only a person raises it again
        configure_tenant(cid, automation_levels_by_class={'schedule': 0})
        res = report(cid, _row(cid, accts['two'], 'sched_one').id, 'cancelled', actor=PERSON)
        assert res['autonomy_lowered'] is None
        cfg = configure_tenant(cid, automation_levels_by_class={'schedule': None}, changed_by='user:42')
        assert cfg['effective_levels']['schedule'] == 2 and cfg['autonomy_log'][-1]['event'] == 'class_level_cleared'


# ── the stop switch ───────────────────────────────────────────────────

def test_the_stop_switch_halts_the_next_step_and_every_approval(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant
        from playbooks.governance import evaluate, approve, report
        _only_plan3(cid, 3)
        evaluate(cid, account_id=accts['one'])
        row = _row(cid, accts['one'], 'plan3')
        configure_tenant(cid, kill_switch=True)
        res = report(cid, row.id, 'done', actor=PERSON)                  # the report is recorded; the next step is not sent
        assert res['advanced_to'] == {'step': 'meet', 'number': 2, 'of': 3, 'by_policy': False, 'reason': 'stop_switch_on'}
        assert res['awaiting_approval'] is True
        with pytest.raises(ValueError, match='kill switch is on'):
            approve(cid, row.id, actor=PERSON)
        assert evaluate(cid)['status'] == 'disabled'
        configure_tenant(cid, kill_switch=False)
        assert approve(cid, row.id, actor=PERSON)['step']['state'] == 'sent'


# ── raise suggestions ─────────────────────────────────────────────────

def test_a_clean_run_of_approvals_suggests_a_raise_and_changes_nothing(auto, monkeypatch):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks import definitions
        from playbooks.definitions import configure_tenant, playbooks_for_customer, tenant_config
        from playbooks.governance import evaluate, approve, report, raise_suggestions
        monkeypatch.setitem(definitions.governance()['autonomy'], 'suggest_raise_after', 2)
        configure_tenant(cid, disabled_playbooks=['note_auto', 'note_human', 'esc_one', 'offer_one', 'money_sched', 'plan3'])
        assert raise_suggestions(cid) == []
        for name in ('one', 'two'):
            evaluate(cid, account_id=accts[name])
            approve(cid, _row(cid, accts[name], 'sched_one').id, actor=PERSON)
        sug = playbooks_for_customer(cid)['autonomy']['suggested_raises']
        assert len(sug) == 1 and sug[0]['action_class'] == 'schedule' and sug[0]['from'] == 0 and sug[0]['to'] == 2
        assert sug[0]['clean_approvals'] == 2
        assert tenant_config(cid)['effective_levels']['schedule'] == 0   # a suggestion, not a change
        evaluate(cid, account_id=accts['three'])
        report(cid, _row(cid, accts['three'], 'sched_one').id, 'cancelled', actor=PERSON)   # a decline breaks the run
        assert raise_suggestions(cid) == []


# ── surfaces ──────────────────────────────────────────────────────────

def test_review_tool_and_routes_are_registered():
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS
    from mcp_server.auth import WRITE_TOOLS
    import mcp_server.cs_pulse_onboarding as m
    from playbooks.http import ROUTES as PB_ROUTES
    from app_api.http import ROUTES as APP_ROUTES
    assert 'review_intervention' in KEYED_TOOLS and 'review_intervention' in WRITE_TOOLS and hasattr(m, 'review_intervention')
    assert '/api/interventions/{id}/review' in PB_ROUTES and '/app/api/interventions/{id}/review' in APP_ROUTES


def test_mcp_tools_carry_levels_by_class_and_review(auto):
    cid, accts, _ = auto
    with app.app_context():
        from fastmcp.exceptions import ToolError
        from mcp_server.cs_pulse_onboarding import configure_playbooks, evaluate_playbooks, review_intervention, get_playbooks
        out = configure_playbooks(cid, automation_level=4, automation_levels_by_class={'schedule': 1},
                                  disabled_playbooks=['note_auto', 'note_human', 'sched_one', 'offer_one', 'money_sched', 'plan3'])
        assert out['tenant']['effective_levels'] == {'notify': 4, 'schedule': 1, 'escalate': 4, 'offer': 0}
        with pytest.raises(ToolError, match='above the platform ceiling'):
            configure_playbooks(cid, automation_levels_by_class={'offer': 2})
        evaluate_playbooks(cid, account_id=accts['one'])
        row = _row(cid, accts['one'], 'esc_one')
        assert review_intervention(cid, row.id, note='seen')['review']['pending'] is False
        with pytest.raises(ToolError, match='already reviewed'):
            review_intervention(cid, row.id)
        d = get_playbooks(cid)
        assert d['always_human'] == {'account_flags': ['regulated']} and d['tenant']['ceiling'] == 4
