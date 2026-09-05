"""
Full disclosure + the bulk communications lane, as a user would use them:
  * create_customer requires a declared data_origin; unknown values are refused
  * the origin and its disclosure appear on every read surface (portfolio, journey, Ask AI, /health)
  * declare_data_origin changes it with an audited reason
  * import_communications resolves accounts by external id / id / name, reports duplicates and
    unknown accounts, processes through the engine, rebuilds journeys; HTTP route pinned
"""
import json
import os
import sys
import uuid
from datetime import date, datetime
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
from models import Account, HealthScore, JourneyData, QualitativeSignal, ContextNode   # noqa: E402
import utils.health_thresholds as ht                      # noqa: E402


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        res = create_customer(name=f'Origin {tag}', domain=f'origin-{tag}.test', vertical='saas_premium',
                              admin_email=f'o_{tag}@t.test', admin_name='O', data_origin='synthetic_demo')
        cid = res['customer_id']
        a = Account(customer_id=cid, account_name='Northwind Analytics', revenue=900_000, vertical='saas_premium',
                    external_account_id='ACC-NW', profile_metadata={'primary_champion_name': 'Elena Rossi', 'renewal_date': '2026-09-01'})
        db.session.add(a); db.session.flush()
        for m, s in [(date(2026, 1, 1), 78), (date(2026, 2, 1), 74)]:
            db.session.add(HealthScore(account_id=a.account_id, measurement_month=m, health_score=s, kpi_only_score=s, health_status=ht.classify(s)))
        db.session.commit()
        yield cid, a.account_id, res
        db.session.remove()
        db.drop_all()


def test_create_customer_requires_a_known_origin_and_returns_the_disclosure(tenant):
    cid, aid, res = tenant
    assert res['data_origin'] == 'synthetic_demo' and 'SYNTHETIC DEMO DATA' in res['disclosure']
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_onboarding import create_customer
    with pytest.raises(ToolError, match='data_origin must be one of'):
        create_customer(name='X', domain=f'x-{uuid.uuid4().hex[:6]}.test', vertical='saas_premium',
                        admin_email=f'x_{uuid.uuid4().hex[:6]}@t.test', admin_name='X', data_origin='made_up')


def test_import_resolves_accounts_dedups_and_processes(tenant, monkeypatch):
    cid, aid, _ = tenant
    from mcp_server.cs_pulse_onboarding import import_communications
    comms = [
        {'source_account_id': 'ACC-NW', 'source_type': 'crm_activity', 'signal_type': 'champion_departure',
         'text': 'Elena Rossi accepted a role elsewhere; last day March 6', 'occurred_at': '2026-02-20T10:00:00Z',
         'participants': [{'name': 'Elena Rossi', 'role': 'VP Data'}], 'source_ref': 'crm:evt:1'},
        {'account_name': 'northwind analytics', 'source_type': 'email', 'text': 'Sending the notes as promised.', 'occurred_at': '2026-02-21T09:00:00Z'},
        {'account_id': aid, 'source_type': 'email', 'text': 'Sending the notes as promised.', 'occurred_at': '2026-02-22T09:00:00Z'},   # duplicate
        {'source_account_id': 'ACC-NOPE', 'source_type': 'email', 'text': 'who?', 'occurred_at': '2026-02-22T09:00:00Z'},             # unknown account
        {'source_account_id': 'ACC-NW', 'source_type': 'transcript', 'text': 'call transcript without consent', 'occurred_at': '2026-02-23T09:00:00Z'},  # rejected
    ]
    out = import_communications(cid, comms)
    assert out['received'] == 5 and out['queued'] == 2 and out['duplicates'] == 1
    assert out['unknown_accounts'] == [{'index': 3, 'ref': 'ACC-NOPE'}]
    assert out['rejected'] and 'consent' in out['rejected'][0]['error']
    assert out['processed']['processed'] == 2 and out['processed']['journeys_rebuilt'] == 1
    with app.app_context():
        n = ContextNode.query.filter_by(account_id=aid, node_type='SIGNAL', node_subtype='champion_departure').one()
        assert n.source_ref == 'crm:evt:1' and (n.properties or {}).get('stakeholder_name') == 'Elena Rossi'
        assert JourneyData.query.filter_by(account_id=aid).one().journey_json['arc']['arc_type'] == 'exec_sponsor_change'
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match='at most'):
        import_communications(cid, [{}] * 501)


def test_disclosure_on_every_surface(tenant):
    cid, aid, _ = tenant
    from mcp_server.cs_pulse_onboarding import list_journeys, get_journey, ask
    port = list_journeys(cid)
    assert port['data_origin'] == 'synthetic_demo' and port['synthetic'] is True and 'SYNTHETIC' in port['disclosure']
    j = get_journey(cid, aid)
    assert j['data_origin'] == 'synthetic_demo' and j['synthetic'] is True and j['label'] == 'Synthetic demo data'
    a = ask(cid, 'why is Northwind at risk?', account_id=aid)
    assert a['synthetic'] is True and a['answer'].startswith('[Synthetic demo data] ')


def test_declare_data_origin_is_audited_and_changes_the_surface(tenant):
    cid, aid, _ = tenant
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_onboarding import declare_data_origin, list_journeys
    from mcp_server import audit
    with pytest.raises(ToolError, match='reason is required'):
        declare_data_origin(cid, 'real', '')
    out = declare_data_origin(cid, 'real', 'first live CRM feed connected 2026-09-05')
    assert out['previous'] == 'synthetic_demo' and out['data_origin'] == 'real' and out['synthetic'] is False
    assert list_journeys(cid)['synthetic'] is False
    with app.app_context():
        rows = audit.query(cid, tool='declare_data_origin')
        assert rows and 'synthetic_demo -> real: first live CRM feed' in rows[0]['detail']
    declare_data_origin(cid, 'synthetic_demo', 'restore for the rest of the tests')


def test_backtest_label_follows_the_declared_origin(tenant):
    cid, aid, _ = tenant
    from evals.lead_time_backtest import run_backtest
    with app.app_context():
        assert run_backtest(cid, assert_real=True)['evidence_label'] != 'measured'      # synthetic_demo


def test_health_and_import_route_pinned():
    from signal_engine.http import ROUTES
    assert '/api/signals/import' in ROUTES


def test_signals_csv_new_shape_aliases_and_taxonomy_warning(tenant):
    """The signals file is the structured lane: new column names, aliases for the old ones, an unknown
    signal_type is a WARNING (extracted from content), use_case and stakeholder_email flow through."""
    cid, aid, _ = tenant
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data
    csv_text = (
        "source_account_id,occurred_at,signal_type,content,source_platform,source_ref,stakeholder_name,stakeholder_title,stakeholder_email,use_case,source_type\n"
        "ACC-NW,2026-03-02T09:00:00,budget_pressure,Finance asked us to justify the renewal line by line,gainsight,GS-1,Tom Becker,Executive Sponsor,tom@northwind.com,Analytics rollout,crm_activity\n"
        "ACC-NW,2026-03-03T09:00:00,some_legacy_code,Support asked about the sandbox refresh schedule,zendesk,ZD-2,,,,,ticket\n"
    )
    r = upload_csv(cid, 'signals.csv', csv_text)                         # alias filename
    assert r['status'] == 'success' and r['canonical_filename'] == 'enhanced_qualitative_signals.csv'
    assert any('some_legacy_code (1)' in w and 'extractor' in w for w in r['warnings']), r['warnings']
    out = process_data(cid)
    assert any(s.startswith('signals_queued_2') for s in out['steps_completed']), out['steps_completed']
    with app.app_context():
        typed = QualitativeSignal.query.filter_by(customer_id=cid, source_ref='GS-1').one()
        assert typed.signal_type == 'budget_pressure' and typed.use_case == 'Analytics rollout' and typed.source_type == 'crm_activity'
        assert typed.stakeholder_roles[0]['email'] == 'tom@northwind.com'
        assert typed.cg_node_id is not None, (out['steps_completed'], out['errors'])
        n = db.session.get(ContextNode, typed.cg_node_id)
        assert n is not None and n.source_event_id == 'GS-1' and n.source_ref == 'GS-1'     # the source system's id is the event id when known
        assert n.properties['use_case'] == 'Analytics rollout' and n.properties['classification_basis'] == 'declared_subtype'
        legacy = QualitativeSignal.query.filter_by(customer_id=cid, source_ref='ZD-2').one()
        assert legacy.source_type == 'ticket' and legacy.cg_node_id                # extracted (stub), not dropped


def test_account_details_use_cases_and_installed_base_fields(tenant):
    cid, aid, _ = tenant
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data, list_journeys, get_journey
    from utils.csv_ingest import parse_use_cases
    assert parse_use_cases('DR failover; LLM training') == [{'name': 'DR failover'}, {'name': 'LLM training'}]
    assert parse_use_cases('[{"name":"DR failover","status":"planned","target_date":"2026-06-30"},"Reporting"]') == \
        [{'name': 'DR failover', 'status': 'planned', 'target_date': '2026-06-30'}, {'name': 'Reporting'}]
    assert parse_use_cases('not json [') == [{'name': 'not json ['}]
    acc = ("source_account_id,account_name,industry,region,arr,use_cases,purchase_date,refresh_date,contract_value,contract_type\n"
           "ACC-NW,Northwind Analytics,Software,NA,900000,\"[{\"\"name\"\": \"\"DR failover\"\", \"\"status\"\": \"\"planned\"\"}]\",2024-05-01,2027-05-01,1200000,hardware\n")
    r = upload_csv(cid, 'account_details.csv', acc)
    assert r['status'] == 'success' and not any('use_cases' in w for w in (r.get('warnings') or []))
    process_data(cid)
    with app.app_context():
        a = Account.query.filter_by(customer_id=cid, external_account_id='ACC-NW').one()
        pm = a.profile_metadata
        assert pm['use_cases'] == [{'name': 'DR failover', 'status': 'planned'}] and pm['contract_type'] == 'hardware'
        assert str(pm['refresh_date']).startswith('2027-05-01') and pm['contract_value'] in (1200000, 1200000.0)
    row = next(r for r in list_journeys(cid)['journeys'] if r['account_id'] == aid)
    assert row['use_cases'][0]['name'] == 'DR failover' and row['contract_type'] == 'hardware'
    j = get_journey(cid, aid)
    assert j['account']['use_cases'][0]['name'] == 'DR failover' and j['account']['refresh_date']
    from signal_engine.enrichment import use_cases_block
    assert 'DR failover' in use_cases_block(j['account']['use_cases']) and use_cases_block([]) == '(none declared)'


def test_outcomes_csv_is_the_same_lane_as_log_outcome(tenant):
    """Aliases from the old file, revenue as magnitude with the bucket's sign, unknown type warned and
    stored without direction, several linked refs, installed-base types in buckets, re-upload → no copies."""
    cid, aid, _ = tenant
    from mcp_server.cs_pulse_onboarding import upload_csv, process_data
    from models import ContextEdge
    csv_text = (
        "source_account_id,outcome_date,outcome_type,title,revenue_value,evidence,linked_signal_id,decided_by,outcome_id,use_case\n"
        "ACC-NW,2026-04-30,contraction,Renewal at 60% of seats,560000,Order form 2026-04-30,crm:evt:1;GS-1,ae@t.test,SO-1,Analytics rollout\n"   # positive given → stored negative
        "ACC-NW,2026-05-15,refresh_won,Refresh order,-120000,,,ae@t.test,SO-2,\n"                                                     # negative given for a positive bucket → normalised + flagged
        "ACC-NW,2026-05-20,some_new_type,Something else,10,,,,SO-3,\n"                                                              # unknown → warned, stored, no direction
    )
    r = upload_csv(cid, 'outcomes.csv', csv_text)
    assert r['status'] == 'success' and any('some_new_type (1)' in w for w in (r.get('warnings') or [])), r.get('warnings')
    out = process_data(cid)
    assert 'outcomes_loaded_3' in out['steps_completed'] and 'outcome_edges_loaded_2' in out['steps_completed'], out['steps_completed']
    with app.app_context():
        outs = {n.source_ref: n for n in ContextNode.query.filter_by(account_id=aid, node_type='OUTCOME').all() if n.source_ref}
        c = outs['SO-1']
        assert float(c.revenue_impact) == -560000.0 and c.properties['bucket'] == 'lost' and c.properties['use_case'] == 'Analytics rollout'
        assert c.properties['decided_by'] == 'ae@t.test' and c.title == 'Renewal at 60% of seats' and c.properties['sign_normalised'] is False
        assert ContextEdge.query.filter_by(to_node_id=c.node_id, edge_type='LED_TO').count() == 2
        w = outs['SO-2']
        assert float(w.revenue_impact) == 120000.0 and w.properties['bucket'] == 'expansion' and w.properties['sign_normalised'] is True
        u = outs['SO-3']
        assert u.properties['unknown_type'] is True and u.properties['bucket'] is None
    r2 = upload_csv(cid, 'outcomes.csv', csv_text)
    out2 = process_data(cid)
    assert 'outcomes_loaded_0' in out2['steps_completed'], out2['steps_completed']        # source_ref idempotency
    with app.app_context():
        assert ContextNode.query.filter_by(account_id=aid, node_type='OUTCOME').count() >= 3


def test_import_by_ref_maps_both_the_callers_ref_and_the_source_ref(tenant):
    cid, aid, _ = tenant
    from mcp_server.cs_pulse_onboarding import import_communications
    out = import_communications(cid, [{'ref': 'nw_comm_9', 'source_ref': 'crm:evt:9', 'source_account_id': 'ACC-NW', 'source_type': 'email',
                                       'text': 'Budget review scheduled for next quarter, all vendors in scope', 'occurred_at': '2026-03-30T09:00:00Z'}],
                                process_now=False)
    assert out['queued'] == 1 and out['by_ref']['nw_comm_9'] == out['by_ref']['crm:evt:9'] == out['signal_ids'][0]


def test_delete_customer_requires_confirmation_and_removes_everything():
    from fastmcp.exceptions import ToolError
    from mcp_server.cs_pulse_onboarding import create_customer, delete_customer
    from models import Customer, Account, JourneyData, ContextNode, QualitativeSignal
    from mcp_server import audit
    with app.app_context():
        tag = uuid.uuid4().hex[:8]
        res = create_customer(name=f'Gone {tag}', domain=f'gone-{tag}.test', vertical='saas_premium',
                              admin_email=f'g_{tag}@t.test', admin_name='G', data_origin='synthetic_test')
        cid = res['customer_id']
        a = Account(customer_id=cid, account_name='Bye Co', revenue=1, vertical='saas_premium', external_account_id='BYE')
        db.session.add(a); db.session.commit()
        aid = a.account_id
    from mcp_server.cs_pulse_onboarding import submit_signal
    submit_signal(cid, aid, 'A note', source_type='manual', signal_type='routine_review', occurred_at='2026-03-01T09:00:00Z')
    with pytest.raises(ToolError, match='confirm_domain'):
        delete_customer(cid, 'wrong.test', 'cleanup')
    with pytest.raises(ToolError, match='reason'):
        delete_customer(cid, f'gone-{tag}.test', '')
    out = delete_customer(cid, f'gone-{tag}.test', 'test cleanup')
    assert out['deleted_rows']['accounts'] == 1 and out['deleted_rows']['qualitative_signals'] == 1 and out['deleted_rows']['journey_data'] == 1
    with app.app_context():
        assert db.session.get(Customer, cid) is None
        assert Account.query.filter_by(customer_id=cid).count() == 0 and ContextNode.query.filter_by(customer_id=cid).count() == 0
        assert JourneyData.query.filter_by(customer_id=cid).count() == 0 and QualitativeSignal.query.filter_by(customer_id=cid).count() == 0
        rows = audit.query(cid, tool='delete_customer')
        assert rows and 'test cleanup' in rows[0]['detail']
