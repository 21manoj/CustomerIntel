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
