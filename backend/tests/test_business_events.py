"""
The business event log (playbooks/events.py, models.BusinessEvent) on a real Postgres tenant; no model is called:

  * every playbook transition writes one event, in the same transaction: the audit rows `intervention.*`
    and the events match one for one, so a transition that commits without an event fails here
  * an event that cannot be written stops the transition (nothing is half-recorded)
  * each event carries the business context: account, playbook and version, step, money exposure, who, why
  * settings changes (level, per-class level, stop switch, playbook switched off or on) are events too
  * events never cross tenants; filters, paging and the date range work; an unknown filter value is refused
  * who may read is per role, from config: admin, VP and CSM read the tenant; CRO and CFO are not granted;
    'own_accounts' narrows to the user's accounts plus tenant-level events
  * the CSV export has every column and defuses spreadsheet formulas
  * backfill rebuilds the same events from history, marks them, and adds nothing on a second run
  * deleting a tenant deletes its events; the route, the MCP tool and the registries are wired
"""
import csv
import io
from collections import Counter

import pytest

from test_playbook_autonomy import app, auto, _schema, _row, PERSON, db      # noqa: F401 — fixtures
from models import BusinessEvent, Intervention, ToolAuditLog
from playbooks import events

AUDIT_TO_EVENT = {'propose': 'proposed', 'approve': 'approved', 'send': 'sent', 'agent_draft': 'agent_drafted'}
ALL_OFF_BUT = lambda *keep: [p for p in ('note_auto', 'note_human', 'sched_one', 'esc_one', 'offer_one', 'money_sched', 'plan3') if p not in keep]


def _types(cid, **kw):
    return Counter(e.event_type for e in BusinessEvent.query.filter_by(customer_id=cid, **kw).all())


def _story(cid, accts):
    """One tenant through every transition: a three-step plan run to an outcome, a failure and a decline that
    lower levels, a policy escalation reviewed afterwards, an agent draft, and every kind of settings change."""
    from playbooks import agent
    from playbooks.definitions import configure_tenant
    from playbooks.governance import evaluate, approve, report, review
    configure_tenant(cid, automation_level=3, disabled_playbooks=ALL_OFF_BUT('plan3'), changed_by='user:42')
    evaluate(cid, account_id=accts['one'])                                   # step 1 (notify) goes by policy
    plan = _row(cid, accts['one'], 'plan3')
    report(cid, plan.id, 'started', actor=PERSON)
    report(cid, plan.id, 'done', note='owner reminded', actor=PERSON)        # step 2 (schedule) by policy
    report(cid, plan.id, 'done', actor=PERSON)                               # step 3 (escalate) waits for a person
    agent.draft(cid, plan.id, requested_by=PERSON)
    approve(cid, plan.id, note='go to the sponsor', actor=PERSON)
    report(cid, plan.id, 'done', note='sponsor met', outcome_type='escalation_resolved', actor=PERSON)

    evaluate(cid, account_id=accts['two'])
    failed = _row(cid, accts['two'], 'plan3')
    report(cid, failed.id, 'failed', note='wrong contact', actor=PERSON)     # lowers notify 3 → 2

    configure_tenant(cid, automation_level=2, disabled_playbooks=ALL_OFF_BUT('sched_one'), changed_by='user:42')
    evaluate(cid, account_id=accts['one'])
    first = _row(cid, accts['one'], 'sched_one')
    approve(cid, first.id, note='plan looks right', actor=PERSON)            # also the plan approval
    evaluate(cid, account_id=accts['two'])
    declined = _row(cid, accts['two'], 'sched_one') if _row(cid, accts['two'], 'sched_one').state == 'proposed' else None
    evaluate(cid, account_id=accts['three'])
    third = _row(cid, accts['three'], 'sched_one')
    if third.state == 'sent':
        report(cid, third.id, 'cancelled', note='customer asked us to wait', actor=PERSON)

    configure_tenant(cid, automation_level=4, disabled_playbooks=ALL_OFF_BUT('esc_one', 'offer_one'), changed_by='user:42')
    evaluate(cid, account_id=accts['one'])
    esc, offer = _row(cid, accts['one'], 'esc_one'), _row(cid, accts['one'], 'offer_one')
    review(cid, esc.id, note='right call', actor=PERSON)
    report(cid, offer.id, 'cancelled', note='no discount this quarter', actor=PERSON)   # a decline: lowers nothing below the floor
    configure_tenant(cid, automation_levels_by_class={'schedule': 1}, kill_switch=True, changed_by='user:42')
    configure_tenant(cid, automation_levels_by_class={'schedule': None}, kill_switch=False, changed_by='user:42')
    return {'plan': plan.id, 'failed': failed.id, 'first': first.id, 'esc': esc.id, 'offer': offer.id, 'declined': declined}


def test_every_transition_writes_exactly_one_event(auto):
    cid, accts, _ = auto
    with app.app_context():
        ids = _story(cid, accts)
        audits = Counter()
        for a in ToolAuditLog.query.filter(ToolAuditLog.customer_id == cid, ToolAuditLog.tool.like('intervention.%')).all():
            t = a.tool.split('.', 1)[1]
            audits[AUDIT_TO_EVENT.get(t, t)] += 1
        got = _types(cid)
        only_events = {'level_set', 'class_level_set', 'class_level_cleared', 'stop_switch_on', 'stop_switch_off',
                       'playbook_disabled', 'playbook_enabled'}                  # settings changes have no intervention.* audit row
        assert {k: v for k, v in got.items() if k not in only_events} == dict(audits)
        for t in ('proposed', 'approved', 'sent', 'started', 'step_done', 'done', 'failed', 'declined', 'reviewed',
                  'agent_drafted', 'plan_approved', 'autonomy_lowered', 'level_set', 'class_level_set', 'class_level_cleared',
                  'stop_switch_on', 'stop_switch_off', 'playbook_disabled', 'playbook_enabled'):
            assert got[t] >= 1, f'the story produced no {t} event'
        assert set(got) <= set(events.EVENT_TYPES) and not BusinessEvent.query.filter_by(customer_id=cid, backfilled=True).count()

        # the plan's own timeline reads in order, with who and what
        line = [(e.event_type, e.step_id, e.step_number, e.actor_kind) for e in
                BusinessEvent.query.filter_by(customer_id=cid, intervention_id=ids['plan']).order_by(BusinessEvent.id)]
        assert line == [
            ('proposed', 'remind', 1, 'local'), ('approved', 'remind', 1, 'policy'), ('sent', 'remind', 1, 'policy'),
            ('started', 'remind', 1, 'user'), ('step_done', 'remind', 1, 'user'),
            ('approved', 'meet', 2, 'policy'), ('sent', 'meet', 2, 'policy'), ('step_done', 'meet', 2, 'user'),
            ('agent_drafted', None, None, 'agent'), ('approved', 'exec', 3, 'user'), ('sent', 'exec', 3, 'user'), ('done', 'exec', 3, 'user')]


def test_events_carry_the_business_context(auto):
    cid, accts, _ = auto
    with app.app_context():
        ids = _story(cid, accts)
        ev = {e.event_type: e for e in BusinessEvent.query.filter_by(customer_id=cid, intervention_id=ids['plan']).order_by(BusinessEvent.id)}
        p = ev['proposed']
        assert (p.account_id, p.playbook_id, p.playbook_version, p.steps_total, p.action_class) == (accts['one'], 'plan3', '1.0', 3, 'notify')
        assert float(p.exposure_revenue) == 100_000 and p.state_after == 'proposed' and p.trigger_key and p.detail['cites']
        d = ev['done']
        row = db.session.get(Intervention, ids['plan'])
        assert (d.state_before, d.state_after, d.reason, d.actor_label) == ('sent', 'closed', 'sponsor met', 'user:42')
        assert d.outcome_type == 'escalation_resolved' and d.outcome_node_id == row.outcome_node_id and d.detail['in_window'] is True
        assert ev['sent'].delivery_status == row.delivery['status'] and ev['approved'].action_class == 'escalate'
        assert ev['agent_drafted'].detail['requested_by'] == 'user:42' and ev['agent_drafted'].detail['steps'] == 3

        low = BusinessEvent.query.filter_by(customer_id=cid, event_type='autonomy_lowered', intervention_id=ids['failed']).one()
        assert (low.action_class, low.detail['from'], low.detail['to'], low.actor_kind, low.account_id) == ('notify', 3, 2, 'user', accts['two'])
        plan_ok = BusinessEvent.query.filter_by(customer_id=cid, event_type='plan_approved').one()
        assert (plan_ok.playbook_id, plan_ok.intervention_id, plan_ok.actor_label) == ('sched_one', ids['first'], 'user:42')
        dec = BusinessEvent.query.filter_by(customer_id=cid, event_type='declined', intervention_id=ids['offer']).one()
        assert dec.state_before == 'proposed' and dec.reason == 'no discount this quarter'

        sets = {e.event_type: e for e in BusinessEvent.query.filter_by(customer_id=cid, intervention_id=None).order_by(BusinessEvent.id)}
        assert sets['stop_switch_on'].actor_label == 'user:42' and sets['stop_switch_on'].actor_kind == 'user'
        assert sets['class_level_set'].action_class == 'schedule' and sets['class_level_set'].detail['to'] == 1
        assert sets['playbook_disabled'].playbook_id and sets['playbook_enabled'].playbook_id


def test_a_transition_that_cannot_write_its_event_does_not_happen(auto, monkeypatch):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant
        from playbooks.governance import evaluate, approve
        configure_tenant(cid, disabled_playbooks=ALL_OFF_BUT('note_human'))
        evaluate(cid, account_id=accts['one'])
        rid = _row(cid, accts['one'], 'note_human').id
        before = _types(cid)

        def broken(*a, **kw):
            raise RuntimeError('event store unavailable')
        monkeypatch.setattr(events, 'record', broken)
        with pytest.raises(RuntimeError):
            approve(cid, rid, actor=PERSON)
        db.session.rollback()
        row = db.session.get(Intervention, rid)
        assert row.state == 'proposed' and row.approved_by is None and _types(cid) == before
        with pytest.raises(RuntimeError):
            configure_tenant(cid, automation_level=3, changed_by='user:42')
        db.session.rollback()
        from playbooks.definitions import tenant_config
        assert tenant_config(cid)['automation_level'] == 0


def test_the_reader_filters_pages_and_stays_inside_the_tenant(auto):
    cid, accts, _ = auto
    with app.app_context():
        from mcp_server.cs_pulse_onboarding import create_customer
        ids = _story(cid, accts)
        other = create_customer(name='Other tenant', domain=f'other-{cid}.test', vertical='saas_premium', admin_email=f'o{cid}@t.test',
                                admin_name='O', data_origin='synthetic_test')['customer_id']
        assert events.list_events(other)['total'] == 0
        assert events.list_events(other, intervention_id=ids['plan'])['events'] == []

        everything = events.list_events(cid, limit=500)
        assert everything['total'] == len(everything['events']) == BusinessEvent.query.filter_by(customer_id=cid).count()
        assert all(e['at'] >= n['at'] for e, n in zip(everything['events'], everything['events'][1:]))      # newest first
        named = next(e for e in everything['events'] if e['intervention_id'] == ids['plan'])
        assert named['account'] == 'Acct one' and named['event'] == events.EVENT_TYPES[named['event_type']]

        assert {e['account_id'] for e in events.list_events(cid, account_id=accts['two'])['events']} == {accts['two']}
        assert {e['playbook_id'] for e in events.list_events(cid, playbook_id='plan3')['events']} == {'plan3'}
        assert {e['event_type'] for e in events.list_events(cid, event_type='sent')['events']} == {'sent'}
        assert len(events.list_events(cid, intervention_id=ids['plan'])['events']) == 12
        page = events.list_events(cid, limit=5, offset=5)
        assert [e['id'] for e in page['events']] == [e['id'] for e in everything['events'][5:10]] and page['total'] == everything['total']
        today = everything['events'][0]['at'][:10]
        assert events.list_events(cid, since=today, until=today)['total'] == everything['total']
        assert events.list_events(cid, until='2020-01-01')['total'] == 0 and events.list_events(cid, since='2999-01-01')['total'] == 0
        for bad in ({'event_type': 'nope'}, {'since': 'yesterday'}, {'limit': 0}, {'offset': -1}):
            with pytest.raises(ValueError):
                events.list_events(cid, **bad)
        assert len(events.list_events(cid, limit=10_000)['events']) <= events.settings()['max_page_size']


def test_who_may_read_is_per_role_from_config(auto, monkeypatch):
    cid, accts, _ = auto
    with app.app_context():
        assert [events.read_scope(r) for r in ('admin', 'vpcsm', 'csm')] == ['tenant'] * 3
        assert [events.read_scope(r) for r in ('cro', 'cfo', 'nobody', None)] == [None] * 4
        with monkeypatch.context() as m:
            m.setattr(events, 'settings', lambda: {'read_scope_by_role': {'csm': 'own_accounts', 'cfo': 'everything'}})
            assert events.read_scope('csm') == 'own_accounts' and events.read_scope('cfo') is None  # an unknown scope grants nothing

        _story(cid, accts)
        mine = events.list_events(cid, only_account_ids=[accts['two']], limit=500)['events']
        assert {e['account_id'] for e in mine} == {accts['two'], None}                     # their accounts + tenant-level settings
        assert any(e['event_type'] == 'stop_switch_on' for e in mine)
        none = events.list_events(cid, only_account_ids=[], limit=500)['events']
        assert none and {e['account_id'] for e in none} == {None}


def test_csv_export_has_every_column_and_defuses_formulas(auto):
    cid, accts, _ = auto
    with app.app_context():
        from playbooks.definitions import configure_tenant
        from playbooks.governance import evaluate, report
        configure_tenant(cid, disabled_playbooks=ALL_OFF_BUT('note_human'))
        evaluate(cid, account_id=accts['one'])
        report(cid, _row(cid, accts['one'], 'note_human').id, 'cancelled', note='=HYPERLINK("http://x","click")', actor=PERSON)
        rows = list(csv.DictReader(io.StringIO(events.export_csv(cid))))
        assert list(rows[0]) == events.CSV_COLUMNS and len(rows) == BusinessEvent.query.filter_by(customer_id=cid).count()
        dec = next(r for r in rows if r['event_type'] == 'declined')
        assert dec['reason'].startswith("'=") and dec['account'] == 'Acct one' and dec['exposure_revenue'] == '100000.0'
        assert len(list(csv.DictReader(io.StringIO(events.export_csv(cid, event_type='proposed'))))) == 1


def test_backfill_rebuilds_history_marks_it_and_is_idempotent(auto):
    cid, accts, _ = auto
    with app.app_context():
        _story(cid, accts)
        live = Counter((e.intervention_id, e.event_type) for e in BusinessEvent.query.filter_by(customer_id=cid)
                       if e.event_type not in events.SETTING_TYPES)
        live_settings = _types(cid) - Counter({t: 10**6 for t in set(events.EVENT_TYPES) - set(events.SETTING_TYPES)})
        BusinessEvent.query.filter_by(customer_id=cid).delete()
        db.session.commit()

        dry = events.backfill(cid, dry_run=True)
        assert dry['events'] > 0 and BusinessEvent.query.filter_by(customer_id=cid).count() == 0
        out = events.backfill(cid)
        assert out['events'] == dry['events'] == BusinessEvent.query.filter_by(customer_id=cid).count()
        assert BusinessEvent.query.filter_by(customer_id=cid, backfilled=False).count() == 0
        back = Counter((e.intervention_id, e.event_type) for e in BusinessEvent.query.filter_by(customer_id=cid)
                       if e.event_type not in events.SETTING_TYPES)
        assert back == live
        back_settings = _types(cid) - Counter({t: 10**6 for t in set(events.EVENT_TYPES) - set(events.SETTING_TYPES)})
        # switching a playbook off or on was never in the automation log, so it cannot be rebuilt
        assert back_settings == Counter({k: v for k, v in live_settings.items() if k not in ('playbook_disabled', 'playbook_enabled')})
        sent = BusinessEvent.query.filter_by(customer_id=cid, event_type='sent').first()
        assert sent.at == db.session.get(Intervention, sent.intervention_id).sent_at or sent.step_number > 1

        again = events.backfill(cid)
        assert again['events'] == 0 and again['tenants'] == {}

        # a live event after the backfill is not marked, and a later backfill leaves the tenant alone
        from playbooks.definitions import configure_tenant
        configure_tenant(cid, kill_switch=True, changed_by='user:42')
        assert BusinessEvent.query.filter_by(customer_id=cid, backfilled=False).count() == 1
        assert events.backfill(cid)['events'] == 0


def test_tenant_deletion_removes_events_and_everything_is_wired(auto):
    cid, accts, _ = auto
    with app.app_context():
        from models import Customer
        from mcp_server.cs_pulse_onboarding import delete_customer, list_business_events
        from mcp_server.onboarding_tool_registry import KEYED_TOOLS
        from mcp_server.auth import WRITE_TOOLS
        from app_api.http import ROUTES
        from fastmcp.exceptions import ToolError
        _story(cid, accts)
        out = list_business_events(cid, event_type='sent', limit=3)
        assert out['events'] and len(out['events']) <= 3 and out['coverage'].startswith('playbook events only')
        with pytest.raises(ToolError):
            list_business_events(cid, event_type='nope')
        assert 'list_business_events' in KEYED_TOOLS and 'list_business_events' not in WRITE_TOOLS and '/app/api/activity' in ROUTES
        n = BusinessEvent.query.filter_by(customer_id=cid).count()
        res = delete_customer(cid, db.session.get(Customer, cid).domain, 'test teardown')
        assert BusinessEvent.query.filter_by(customer_id=cid).count() == 0 and n > 0
        assert 'business_events' in str(res)
