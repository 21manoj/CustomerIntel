"""
scripts/repair_intervention_titles.py + the shared title helper, on a real Postgres tenant:
  * approve titles the INTERVENTION node through playbooks.governance.intervention_title, the label in colon form
  * a node still carrying the old em-dash label reads as "playbook 'champion departure'" in the narrative
  * dry run reports node id / old / new and writes nothing; --apply fixes the title and rebuilds the journey,
    so the narrative sentence names the playbook properly; a second run changes nothing
  * --customer-id filters; a playbook that no longer exists falls back to its id humanised
  * a playbook label carrying the title separator is refused at load
  * health_counts() agrees with list_interventions on stuck + delivery problems
"""
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from flask import Flask                                   # noqa: E402
from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = TEST_DB
db.init_app(app)
import mcp_server.common as _common                       # noqa: E402
_common._flask_app = app
from models import Account, ContextNode, Intervention, JourneyData   # noqa: E402
from scripts import repair_intervention_titles as repair  # noqa: E402

PLAYBOOK = 'champion_departure_sponsor_rebuild'
OLD_LABEL = 'Champion departure — sponsor rebuild'        # the em-dash form the labels had when the live nodes were written
NEW_LABEL = 'Champion departure: sponsor rebuild'          # config/playbooks/saas_premium.json today
ACCOUNT = 'Northstar Mutual'


def _narrative_sentence(cid, aid, node_id):
    j = JourneyData.query.filter_by(customer_id=cid, account_id=aid).first().journey_json
    # lowercased: the counterfactual-hook sentence opens with the playbook and capitalises it
    return next(s['text'] for ch in j['narrative']['chapters'] for s in ch['sentences'] if f'int:{node_id}' in s['cites']).lower()


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer, submit_signal
        from signal_engine.pipeline import process_pending
        from journeys.wizard_a import run_wizard_a
        from playbooks.governance import evaluate, approve
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Repair {tag}', domain=f'rp-{tag}.test', vertical='saas_premium',
                              admin_email=f'rp_{tag}@t.test', admin_name='R', data_origin='synthetic_test')['customer_id']
        acct = Account(customer_id=cid, account_name=ACCOUNT, revenue=420_000, vertical='saas_premium', external_account_id='NOR',
                       profile_metadata={'primary_champion_name': 'Dana Whitfield', 'renewal_date': '2026-10-15'})
        db.session.add(acct); db.session.commit()
        submit_signal(cid, acct.account_id, 'Dana Whitfield is leaving at the end of the month', source_type='crm_activity',
                      signal_type='champion_departure', occurred_at='2026-08-04T10:00:00Z', process_now=False)
        process_pending(customer_id=cid, limit=100, rebuild_journeys=False)
        run_wizard_a(cid, evaluate_playbooks=False)
        evaluate(cid)
        row = Intervention.query.filter_by(customer_id=cid, playbook_id=PLAYBOOK).one()
        out = approve(cid, row.id, note='sponsor rebuild')          # no endpoint configured: not_configured, node still written
        assert out['state'] == 'sent' and out['delivery']['status'] == 'not_configured'
        yield cid, acct.account_id, out['node_id']
        db.session.remove()
        db.drop_all()


def _run(argv, capsys):
    assert repair.main(argv) == 0
    return capsys.readouterr().out


def test_approve_titles_the_node_through_the_shared_helper(tenant):
    cid, aid, node_id = tenant
    with app.app_context():
        from playbooks.governance import intervention_title, TITLE_SEP
        node = db.session.get(ContextNode, node_id)
        assert node.title == intervention_title(NEW_LABEL, ACCOUNT) == f'{NEW_LABEL}{TITLE_SEP}{ACCOUNT}'
        assert f"playbook '{NEW_LABEL.lower()}'" in _narrative_sentence(cid, aid, node_id)
        assert intervention_title('Label', None) == 'Label' and intervention_title(' L ', ' A ') == f'L{TITLE_SEP}A'
        assert len(intervention_title('x' * 1000, ACCOUNT)) == ContextNode.__table__.c.title.type.length


def test_dry_run_reports_the_old_title_and_writes_nothing(tenant, capsys):
    cid, aid, node_id = tenant
    with app.app_context():
        from journeys.wizard_a import run_wizard_a
        from playbooks.governance import TITLE_SEP
        node = db.session.get(ContextNode, node_id)
        node.title = f'{OLD_LABEL}{TITLE_SEP}{ACCOUNT}'
        db.session.commit()
        run_wizard_a(cid, [aid], evaluate_playbooks=False)
        assert "playbook 'champion departure'" in _narrative_sentence(cid, aid, node_id)      # the bug being repaired
        out = _run(['--customer-id', str(cid)], capsys)
        assert f'node {node_id} customer {cid} account {aid}' in out
        assert repr(f'{OLD_LABEL}{TITLE_SEP}{ACCOUNT}') in out and repr(f'{NEW_LABEL}{TITLE_SEP}{ACCOUNT}') in out
        assert 'titles would be changed: 1' in out and 'accounts touched: 1' in out
        db.session.expire_all()
        assert db.session.get(ContextNode, node_id).title == f'{OLD_LABEL}{TITLE_SEP}{ACCOUNT}'
        assert "playbook 'champion departure'" in _narrative_sentence(cid, aid, node_id)


def test_customer_filter_excludes_other_tenants(tenant, capsys):
    cid, aid, node_id = tenant
    with app.app_context():
        out = _run(['--customer-id', str(cid + 100_000)], capsys)
        assert 'intervention nodes: 0' in out and f'node {node_id}' not in out


def test_apply_fixes_the_title_rebuilds_the_journey_and_is_idempotent(tenant, capsys):
    cid, aid, node_id = tenant
    with app.app_context():
        from playbooks.governance import TITLE_SEP
        out = _run(['--customer-id', str(cid), '--apply'], capsys)
        assert 'titles changed: 1' in out and f'customer {cid}: 1 journeys rebuilt' in out
        db.session.expire_all()
        assert db.session.get(ContextNode, node_id).title == f'{NEW_LABEL}{TITLE_SEP}{ACCOUNT}'
        assert f"playbook '{NEW_LABEL.lower()}'" in _narrative_sentence(cid, aid, node_id)
        again = _run(['--customer-id', str(cid), '--apply'], capsys)
        assert 'titles changed: 0' in again and 'journeys rebuilt' not in again


def test_a_removed_playbook_falls_back_to_its_id_humanised(tenant):
    cid, aid, node_id = tenant
    with app.app_context():
        from playbooks.governance import TITLE_SEP
        node = db.session.get(ContextNode, node_id)
        node.properties = {**node.properties, 'playbook_id': 'retired_playbook'}
        db.session.commit()
        p = repair.plan(cid)
        assert p['nodes'] == 1 and not p['skipped'] and [(n.node_id, new) for n, _, new in p['changes']] == [(node_id, f'retired playbook{TITLE_SEP}{ACCOUNT}')]
        node.properties = {**node.properties, 'playbook_id': PLAYBOOK}
        db.session.commit()
        assert repair.plan(cid)['changes'] == []


def test_a_label_carrying_the_title_separator_is_refused_at_load(tmp_path, monkeypatch):
    import json
    from playbooks import definitions
    from playbooks.definitions import load_vertical, reset_cache, PlaybookConfigError
    src = json.load(open(os.path.join(definitions.PLAYBOOKS_DIR, 'saas_premium.json'), encoding='utf-8'))
    src['playbooks'][0]['label'] = 'Seat true-down — save'
    (tmp_path / 'saas_premium.json').write_text(json.dumps(src), encoding='utf-8')
    monkeypatch.setattr(definitions, 'PLAYBOOKS_DIR', str(tmp_path))
    reset_cache()
    try:
        with pytest.raises(PlaybookConfigError, match='label must not contain'):
            load_vertical('saas_premium')
    finally:
        reset_cache()


def test_health_counts_agree_with_list_interventions(tenant):
    cid, aid, node_id = tenant
    with app.app_context():
        from playbooks.governance import health_counts, list_interventions
        from playbooks.definitions import governance
        row = Intervention.query.filter_by(customer_id=cid, playbook_id=PLAYBOOK).one()
        row.sent_at = datetime.utcnow() - timedelta(days=governance()['stuck_after_days'] + 1)
        db.session.commit()
        listed = list_interventions(cid)
        assert row.id in listed['stuck'] and listed['interventions'][0]['delivery_problem']
        h = health_counts()
        assert set(h) == {'total', 'by_state', 'stuck', 'delivery_problems'} and set(h['by_state']) == set(governance()['states'])
        assert h['total'] == sum(h['by_state'].values()) >= 1 and h['by_state']['sent'] >= 1
        assert h['stuck'] >= 1 and h['delivery_problems'] >= 1
        row.sent_at = datetime.utcnow()
        db.session.commit()
        assert health_counts()['stuck'] == h['stuck'] - 1
