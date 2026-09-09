"""
clone_customer: deep-copy a demo/synthetic tenant into a brand-new one, re-keyed
to new ids, with no shared foreign keys with the source. Real DB execution (not
mocks), matching the pattern in test_tier2_create_customer.py / test_adapters.py.

customer_id 5 in the read-only scoping investigation's reference database
(customerintel_aurelia_dev, a leftover per-worktree dev DB) is a real 6-account
demo tenant ("Lumen Workflows") -- confirmed still present via direct psql
(6 accounts, 81 context_nodes, 2 context_edges, 6 journey_data rows, 3
interventions, 1 wizard_run, 0 health_scores/forecast_runs/weight_calibrations).
It is useful as the shape reference this test's fixture is built from, but it
is NOT used directly here: it doesn't exist in the DATABASE_URL this suite runs
against (customerintel_test, or whatever *_test database is configured) or in
customerintel_dev, and even where it does exist it isn't a '*_test'-named
database -- running a destructive suite against it would violate this repo's
own safety convention (_assert_isolated_test_db below; see project memory,
feedback_destructive_test_fixture: a prior destructive-fixture bug caused two
real prod data-loss incidents). It also doesn't exercise every edge case this
suite needs (that tenant has zero ContextEdge.superseded_by usage and zero
ForecastRun/WeightCalibration rows). So _build_rich_source() constructs an
equivalent-shaped but self-contained fixture instead, deliberately hitting the
trickiest id-remapping cases directly: an INTERVENTION-type ContextNode whose
properties embed an intervention_id + trigger_episode_ids and whose
source_event_id is 'intervention:{id}'; a superseded ContextEdge; a
JourneyData.journey_json with 'sig:'/'hs:' episode-id strings, a raw
evidence_node_ids list, and an embedded forecast.run_id; a ForecastRun +
AccountForecast pair (forecast_json embeds 'cites' episode ids and its own
run_id); and a WeightCalibration with a supersession chain and an account_id
nested inside its impact block.
"""
import os
import sys
import uuid
from datetime import datetime, date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from extensions import db


def _make_app():
    _app = Flask(__name__)
    _app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test'
    )
    db.init_app(_app)
    return _app


app = _make_app()

# clone_customer opens its own app context via _get_flask_app() (a module-level
# singleton in mcp_server/common.py) -- point that singleton at this same test
# app/DB rather than letting it build its own from DATABASE_URL a second time.
import mcp_server.common as _common
_common._flask_app = app

from models import (Customer, CustomerConfig, User, Account, ContextNode, ContextEdge, HealthScore,
                    JourneyData, Intervention, ForecastRun, AccountForecast, WeightCalibration,
                    QualitativeSignal, SignalReview, FeatureToggle, ProcessRun, CsvUpload, CsvUploadStaging,
                    KPIMeasurement, CustomerApiKey, LLMUsageLog)
from fastmcp.exceptions import ToolError


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_clone_customer.py refuses to run against database {db_name!r} — its name doesn't contain 'test'."
        )


@pytest.fixture(scope='module', autouse=True)
def _setup_db():
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    _assert_isolated_test_db(db_uri)
    with app.app_context():
        db.create_all()
    yield
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _unique_domain(prefix='clonesrc'):
    return f'{prefix}-{uuid.uuid4().hex[:8]}.test'


def _dt(day=15):
    return datetime(2026, 1, day, 10, 0, 0)


def _build_rich_source(data_origin='synthetic_demo'):
    """Builds one small, structurally rich tenant directly via the ORM (not through
    create_customer/upload_csv/process_data) so the fixture can hit exactly the
    id-remapping edge cases clone_customer needs to get right. Returns a dict of
    everything a test might want to assert against (ids, not ORM objects, since
    objects would be detached across app-context boundaries)."""
    with app.app_context():
        domain = _unique_domain()
        customer = Customer(customer_name='Clone Source Co', domain=domain, email=f'admin@{domain}',
                            vertical='saas_premium', data_origin=data_origin)
        db.session.add(customer)
        db.session.flush()
        cid = customer.customer_id

        # A live credential that must NEVER be copied to a clone.
        config = CustomerConfig(customer_id=cid, vertical='saas_premium',
                                openai_api_key_encrypted='SHOULD-NEVER-BE-CLONED',
                                pillar_weights={'P1': 0.5, 'P2': 0.5}, weights_origin='vertical_default')
        db.session.add(config)

        # A source user + key + usage row -- none of these three should ever appear on the clone.
        src_user = User(customer_id=cid, user_name='Source Admin',
                        email=f'srcadmin_{uuid.uuid4().hex[:6]}@{domain}', role='admin',
                        allowed_customer_ids=[cid])
        db.session.add(src_user)
        db.session.flush()
        db.session.add(CustomerApiKey(customer_id=cid, created_by=src_user.user_id, key_prefix='csp_read_test',
                                      key_hash='x' * 64, name='source key', scopes=['read', 'write']))
        db.session.add(LLMUsageLog(customer_id=cid, module='test', model='claude-test', tokens_in=10, tokens_out=5,
                                   cost_estimate_usd=0.01, success=True))

        # FeatureToggles: one with a live webhook secret, one with live Slack wiring, one plain.
        db.session.add(FeatureToggle(customer_id=cid, feature_name='playbooks', enabled=True, config={
            'webhook_url': 'https://real-customer-n8n.example.com/hook',
            'webhook_secret': 'super-secret-signing-key',
            'slack_webhook_url': 'https://hooks.slack.com/services/REAL/SECRET/TOKEN',
            'automation_level': 1, 'disabled_playbooks': ['some_playbook'],
        }))
        db.session.add(FeatureToggle(customer_id=cid, feature_name='context_graph', enabled=True,
                                     config={'story_arcs': True}))

        accounts = []
        for i in range(3):
            a = Account(customer_id=cid, account_name=f'Account {i}', revenue=100000 * (i + 1),
                       vertical='saas_premium', account_status='active')
            db.session.add(a)
            accounts.append(a)
        db.session.flush()
        a0, a1, a2 = accounts

        db.session.add(FeatureToggle(customer_id=cid, feature_name='signal_engine', enabled=True, config={
            'slack_team_id': 'T0REALWORKSPACE', 'slack_channel_map': {'C0REALCHANNEL': a0.account_id},
        }))

        # ContextNodes.
        sig_node = ContextNode(customer_id=cid, account_id=a0.account_id, node_type='SIGNAL',
                               node_subtype='champion_departure', source='observed', tier=2,
                               title='Champion left', properties={'role': 'champion_change'}, occurred_at=_dt(1))
        db.session.add(sig_node)
        db.session.flush()

        outcome_node = ContextNode(customer_id=cid, account_id=a1.account_id, node_type='OUTCOME',
                                   node_subtype='expansion', source='observed', tier=1, title='Expanded',
                                   properties={'evidence': 'order form'}, revenue_impact=50000,
                                   revenue_impact_type='expansion', occurred_at=_dt(3))
        db.session.add(outcome_node)
        db.session.flush()

        # An Intervention, proposed/approved/sent against the SIGNAL node above.
        iv = Intervention(
            customer_id=cid, account_id=a0.account_id, playbook_id='champion_departure_sponsor_rebuild',
            playbook_version='1', action_class='notify', approval_mode='human', state='sent',
            trigger_key='placeholder', trigger_episode_ids=[f'sig:{sig_node.node_id}'],
            trigger_node_ids=[sig_node.node_id], trigger_roles=['champion_change'],
            trigger_quote='deputy is covering', expected_outcome_types=['protected'],
            expected_window_days=30, exposure_revenue=a0.revenue,
            proposed_by='key:abcd1234', approved_by='key:abcd1234', approved_by_key_id=999999,
            delivery={'status': 'delivered', 'url_host': 'real-customer-n8n.example.com', 'attempts': 1},
        )
        db.session.add(iv)
        db.session.flush()

        # The INTERVENTION-type ContextNode written when the payload was sent
        # (mirrors playbooks/governance.py's approve()) -- properties embed the
        # intervention_id and the (already-string) trigger_episode_ids; source_event_id
        # is the 'intervention:{id}' micro-format.
        intervention_node = ContextNode(
            customer_id=cid, account_id=a0.account_id, node_type='INTERVENTION',
            node_subtype=iv.playbook_id, source='observed', tier=1, title='Playbook executed',
            properties={'intervention_id': iv.id, 'playbook_id': iv.playbook_id,
                       'trigger_episode_ids': [f'sig:{sig_node.node_id}'], 'outcome_node_id': outcome_node.node_id,
                       'delivery_status': 'delivered'},
            source_platform='cs_pulse_playbooks', source_event_id=f'intervention:{iv.id}', occurred_at=_dt(2),
        )
        db.session.add(intervention_node)
        db.session.flush()
        iv.node_id = intervention_node.node_id
        iv.outcome_node_id = outcome_node.node_id

        # ContextEdges: one plain, one that supersedes it (superseded_by chain).
        edge1 = ContextEdge(customer_id=cid, from_node_id=sig_node.node_id, to_node_id=intervention_node.node_id,
                            edge_type='LED_TO', weight=1.0, confidence=1.0,
                            properties={'evidence': 'cited by intervention', 'episode_id': f'sig:{sig_node.node_id}'},
                            occurred_at=_dt(1))
        db.session.add(edge1)
        db.session.flush()
        edge2 = ContextEdge(customer_id=cid, from_node_id=sig_node.node_id, to_node_id=outcome_node.node_id,
                            edge_type='LED_TO', weight=1.0, confidence=0.8, properties={}, occurred_at=_dt(3))
        db.session.add(edge2)
        db.session.flush()
        edge1.superseded_by = edge2.edge_id

        # HealthScore for a0, tied to a ProcessRun + CsvUpload (cross-referenced ids).
        proc = ProcessRun(run_id=f'pd_{uuid.uuid4().hex[:16]}', customer_id=cid, vertical='saas_premium',
                          mode='auto', status='success', counts={'accounts': 3})
        db.session.add(proc)
        db.session.flush()
        upload = CsvUpload(customer_id=cid, file_type='kpi_measurements.csv', sha256='f' * 64, row_count=10,
                           byte_count=1000, process_run_id=proc.id)
        db.session.add(upload)
        db.session.flush()
        proc.upload_ids = [upload.id]

        hs = HealthScore(account_id=a0.account_id, measurement_month=date(2026, 1, 1), health_score=72,
                         health_status='healthy', kpi_only_score=72, input_upload_id=upload.id,
                         process_run_id=proc.id)
        db.session.add(hs)
        db.session.flush()

        km = KPIMeasurement(account_id=a0.account_id, kpi_code='P1-KPI1', value=90, pillar='P1',
                            upload_id=upload.id, measured_at=_dt(1))
        db.session.add(km)

        staging = CsvUploadStaging(customer_id=cid, file_type='outcomes.csv', csv_content='account,val\nA,1\n',
                                   row_count=1, upload_id=upload.id)
        db.session.add(staging)

        qs = QualitativeSignal(signal_id='qs-1', customer_id=cid, account_id=a0.account_id,
                               signal_date=date(2026, 1, 1), signal_type='crm', content='champion left',
                               cg_node_id=sig_node.node_id)
        db.session.add(qs)

        sr = SignalReview(customer_id=cid, account_id=a0.account_id, signal_id='qs-1', node_id=sig_node.node_id,
                          decision='accept', was_flagged=False)
        db.session.add(sr)

        # ForecastRun + AccountForecast: forecast_json embeds 'cites' episode ids and its own run_id.
        fr = ForecastRun(run_id=f'wizard_d_{uuid.uuid4().hex[:12]}', customer_id=cid, vertical='saas_premium',
                         generator_version='1.0', horizon_days=180, as_of=_dt(3),
                         basis_counts={'prior': 1}, labels={'n': 0}, portfolio={'accounts': 1, 'arr': 300000.0},
                         config_snapshot={}, accounts=1)
        db.session.add(fr)
        db.session.flush()
        af = AccountForecast(
            run_id=fr.run_id, customer_id=cid, account_id=a0.account_id, as_of=_dt(3), basis='prior',
            p_retain=0.8, p_retain_low=0.6, p_retain_high=0.9, p_expand=0.1, p_expand_low=0.0, p_expand_high=0.2,
            arr=a0.revenue, expected_arr_end=a0.revenue, expected_arr_low=a0.revenue, expected_arr_high=a0.revenue,
            n_labels=0, forecast_json={'run_id': fr.run_id, 'cites': [f'sig:{sig_node.node_id}'],
                                       'inputs': {'interventions_in_flight': [f'int:{intervention_node.node_id}']}},
        )
        db.session.add(af)

        # WeightCalibration with a supersession chain and an account_id nested in impact.
        wc1 = WeightCalibration(customer_id=cid, vertical='saas_premium', state='superseded', method_version='1.0',
                                outcome_node_ids=[outcome_node.node_id],
                                impact={'accounts': [{'account_id': a0.account_id, 'account_name': a0.account_name}]},
                                recompute={'run_id': proc.run_id, 'mode': 'auto'})
        db.session.add(wc1)
        db.session.flush()
        wc2 = WeightCalibration(customer_id=cid, vertical='saas_premium', state='approved', method_version='1.0',
                                outcome_node_ids=[outcome_node.node_id],
                                impact={'accounts': [{'account_id': a0.account_id, 'account_name': a0.account_name}]})
        db.session.add(wc2)
        db.session.flush()
        wc1.superseded_by = wc2.id

        # JourneyData: hand-built journey_json exercising every embedded-id shape found
        # in the real journeys/journey_builder.py output.
        journey_json = {
            'version': '3.0', 'account_id': a0.account_id, 'account_name': a0.account_name,
            'arc': {'state': 'classified', 'arc_type': 'exec_sponsor_change',
                   'supporting_episode_ids': [f'sig:{sig_node.node_id}']},
            'episodes': [
                {'episode_id': f'sig:{sig_node.node_id}', 'kind': 'signal', 'evidence_node_ids': [sig_node.node_id]},
                {'episode_id': f'int:{intervention_node.node_id}', 'kind': 'intervention', 'evidence_node_ids': [intervention_node.node_id]},
                {'episode_id': f'out:{outcome_node.node_id}', 'kind': 'outcome', 'evidence_node_ids': [outcome_node.node_id]},
                {'episode_id': f'hs:{hs.health_score_id}', 'kind': 'health_transition'},
            ],
            'leading_vs_trailing': {'series': [{'month': '2026-01-01', 'contributing_episode_ids': [f'sig:{sig_node.node_id}']}]},
            'counterfactual_hooks': [{'episode_id': f'int:{intervention_node.node_id}',
                                     'outcomes_after': [{'episode_id': f'out:{outcome_node.node_id}', 'bucket': 'expansion'}]}],
            'forecast': {'run_id': fr.run_id, 'cites': [f'sig:{sig_node.node_id}']},
            'narrative': {'cited_episode_ids': [f'sig:{sig_node.node_id}', f'hs:{hs.health_score_id}']},
        }
        jd = JourneyData(customer_id=cid, account_id=a0.account_id, journey_json=journey_json,
                         total_weeks=4, journey_pattern='exec_sponsor_change', generator_version='3.0')
        db.session.add(jd)

        db.session.commit()

        return {
            'customer_id': cid, 'domain': domain, 'account_ids': [a.account_id for a in accounts],
            'a0': a0.account_id, 'a1': a1.account_id,
            'sig_node_id': sig_node.node_id, 'outcome_node_id': outcome_node.node_id,
            'intervention_node_id': intervention_node.node_id, 'intervention_id': iv.id,
            'edge1_id': edge1.edge_id, 'edge2_id': edge2.edge_id,
            'health_score_id': hs.health_score_id, 'process_run_id': proc.id, 'process_run_run_id': proc.run_id,
            'csv_upload_id': upload.id, 'forecast_run_run_id': fr.run_id, 'wc1_id': wc1.id, 'wc2_id': wc2.id,
        }


@pytest.fixture(scope='module')
def source():
    return _build_rich_source()


def _clone(source_customer_id, **kw):
    from mcp_server.cs_pulse_onboarding import clone_customer
    domain = kw.pop('domain', None) or _unique_domain('clone')
    return clone_customer(source_customer_id=source_customer_id, name=kw.pop('name', 'Clone Target Co'),
                          domain=domain, **kw)


class TestCloneCustomerBasics:
    def test_creates_new_customer_synthetic_demo_and_fresh_admin(self, source):
        result = _clone(source['customer_id'])
        assert result['scope'] == 'customer'
        assert result['customer_id'] != source['customer_id']
        assert result['data_origin'] == 'synthetic_demo'
        assert result['cloned_from_customer_id'] == source['customer_id']
        assert result['expires_at'] is None

        with app.app_context():
            new_customer = db.session.get(Customer, result['customer_id'])
            assert new_customer.data_origin == 'synthetic_demo'   # forced, even though source already was
            assert new_customer.domain != source['domain']

            admin = db.session.get(User, result['admin_user_id'])
            assert admin.role == 'admin'
            assert admin.email == result['admin_email']
            # never the source's own admin email
            src_admin = User.query.filter_by(customer_id=source['customer_id'], user_name='Source Admin').first()
            assert admin.email != src_admin.email
            assert User.query.filter_by(customer_id=result['customer_id']).count() == 1   # minted, not copied

            assert result.get('api_key'), 'clone must mint its own fresh key'
            assert CustomerApiKey.query.filter_by(customer_id=result['customer_id']).count() == 1
            assert LLMUsageLog.query.filter_by(customer_id=result['customer_id']).count() == 0

    def test_ttl_minutes_sets_expires_at_and_omitting_leaves_it_null(self, source):
        no_ttl = _clone(source['customer_id'])
        assert no_ttl['expires_at'] is None
        with app.app_context():
            assert db.session.get(Customer, no_ttl['customer_id']).expires_at is None

        # Compared against this process's own utcnow() (matching clone_customer's own
        # datetime.utcnow() + timedelta), not Customer.created_at: that column is a
        # DB server_default=func.now(), and this Postgres server's session timezone
        # is America/Los_Angeles, not UTC -- a pre-existing, unrelated characteristic
        # of every server_default=func.now() column in this schema, not something
        # clone_customer introduces. Diffing expires_at against created_at would be
        # comparing two different clocks and fail spuriously.
        before = datetime.utcnow()
        with_ttl = _clone(source['customer_id'], ttl_minutes=60)
        after = datetime.utcnow()
        assert with_ttl['expires_at'] is not None
        expires_dt = datetime.fromisoformat(with_ttl['expires_at'])
        assert before + timedelta(minutes=59) <= expires_dt <= after + timedelta(minutes=61)
        with app.app_context():
            c = db.session.get(Customer, with_ttl['customer_id'])
            assert c.expires_at is not None

    def test_new_admin_email_never_collides_and_domain_must_be_free(self, source):
        r1 = _clone(source['customer_id'])
        with pytest.raises(ToolError):
            # reusing r1's own domain must be refused, same as create_customer
            _clone(source['customer_id'], domain=r1['domain'])

    def test_refuses_to_clone_a_real_tenant_even_with_no_key(self):
        real_source = _build_rich_source(data_origin='real')
        with pytest.raises(ToolError, match='not synthetic'):
            _clone(real_source['customer_id'])


class TestCloneCustomerRowCountsAndIdRemap:
    def test_row_counts_match_source(self, source):
        result = _clone(source['customer_id'])
        rows = result['cloned_rows']
        assert rows['accounts'] == 3
        assert rows['context_nodes'] == 3          # sig, outcome, intervention
        assert rows['context_edges'] == 2
        assert rows['interventions'] == 1
        assert rows['journey_data'] == 1
        assert rows['health_scores'] == 1
        assert rows['kpi_measurements'] == 1
        assert rows['qualitative_signals'] == 1
        assert rows['signal_reviews'] == 1
        assert rows['forecast_runs'] == 1
        assert rows['account_forecasts'] == 1
        assert rows['weight_calibrations'] == 2
        assert rows['process_runs'] == 1
        assert rows['csv_uploads'] == 1
        assert rows['csv_upload_staging'] == 1
        assert rows['feature_toggles'] == 3
        assert rows['customer_configs'] == 1
        assert rows['users'] == 1

    def test_no_shared_ids_with_source_across_every_table(self, source):
        result = _clone(source['customer_id'])
        new_cid = result['customer_id']
        with app.app_context():
            new_accounts = Account.query.filter_by(customer_id=new_cid).all()
            new_account_ids = {a.account_id for a in new_accounts}
            assert new_account_ids.isdisjoint(source['account_ids'])

            new_nodes = ContextNode.query.filter_by(customer_id=new_cid).all()
            new_node_ids = {n.node_id for n in new_nodes}
            assert new_node_ids.isdisjoint({source['sig_node_id'], source['outcome_node_id'], source['intervention_node_id']})

            new_edges = ContextEdge.query.filter_by(customer_id=new_cid).all()
            assert {e.edge_id for e in new_edges}.isdisjoint({source['edge1_id'], source['edge2_id']})
            for e in new_edges:
                # the actual spot check the brief asked for: edges point at NEW node ids, not the source's
                assert e.from_node_id in new_node_ids
                assert e.to_node_id in new_node_ids
                assert e.from_node_id != source['sig_node_id']
                assert e.to_node_id not in (source['intervention_node_id'], source['outcome_node_id'])

            new_hs = HealthScore.query.filter(HealthScore.account_id.in_(new_account_ids)).all()
            assert len(new_hs) == 1 and new_hs[0].health_score_id != source['health_score_id']
            assert new_hs[0].account_id in new_account_ids

            new_iv = Intervention.query.filter_by(customer_id=new_cid).first()
            assert new_iv.id != source['intervention_id']
            assert new_iv.account_id in new_account_ids
            assert new_iv.node_id in new_node_ids and new_iv.node_id != source['intervention_node_id']
            assert new_iv.outcome_node_id in new_node_ids and new_iv.outcome_node_id != source['outcome_node_id']
            assert new_iv.trigger_node_ids and new_iv.trigger_node_ids[0] in new_node_ids
            assert new_iv.trigger_node_ids[0] != source['sig_node_id']
            new_sig_id = next(n.node_id for n in new_nodes if n.node_type == 'SIGNAL')
            assert new_iv.trigger_episode_ids == [f'sig:{new_sig_id}']
            assert new_iv.approved_by_key_id is None    # the source key's id can never resolve on the clone

            new_intervention_node = next(n for n in new_nodes if n.node_type == 'INTERVENTION')
            assert new_intervention_node.properties['intervention_id'] == new_iv.id
            assert new_intervention_node.properties['trigger_episode_ids'] == [f'sig:{new_sig_id}']
            new_outcome_node_id = next(n.node_id for n in new_nodes if n.node_type == 'OUTCOME')
            assert new_intervention_node.properties['outcome_node_id'] == new_outcome_node_id
            assert new_intervention_node.source_event_id == f'intervention:{new_iv.id}'

            new_wcs = WeightCalibration.query.filter_by(customer_id=new_cid).order_by(WeightCalibration.id).all()
            assert len(new_wcs) == 2
            assert {w.id for w in new_wcs}.isdisjoint({source['wc1_id'], source['wc2_id']})
            superseded = next(w for w in new_wcs if w.state == 'superseded')
            approved = next(w for w in new_wcs if w.state == 'approved')
            assert superseded.superseded_by == approved.id
            assert superseded.impact['accounts'][0]['account_id'] in new_account_ids
            assert superseded.impact['accounts'][0]['account_id'] != source['a0']
            assert superseded.outcome_node_ids == [new_outcome_node_id]
            assert superseded.proposed_by_key_id is None and superseded.decided_by_key_id is None

            new_fr = ForecastRun.query.filter_by(customer_id=new_cid).first()
            assert new_fr.run_id != source['forecast_run_run_id']
            new_af = AccountForecast.query.filter_by(customer_id=new_cid).first()
            assert new_af.run_id == new_fr.run_id                     # the real FK: must point at the NEW run
            assert new_af.forecast_json['run_id'] == new_fr.run_id
            assert new_af.forecast_json['cites'] == [f'sig:{new_sig_id}']
            new_int_node_id = new_intervention_node.node_id
            assert new_af.forecast_json['inputs']['interventions_in_flight'] == [f'int:{new_int_node_id}']

            new_proc = ProcessRun.query.filter_by(customer_id=new_cid).first()
            assert new_proc.run_id != source['process_run_run_id']
            new_upload = CsvUpload.query.filter_by(customer_id=new_cid).first()
            assert new_upload.id != source['csv_upload_id']
            assert new_upload.process_run_id == new_proc.id           # cross-reference fixed up correctly
            assert new_proc.upload_ids == [new_upload.id]

            new_jd = JourneyData.query.filter_by(customer_id=new_cid).first()
            jj = new_jd.journey_json
            assert jj['account_id'] in new_account_ids and jj['account_id'] != source['a0']
            assert jj['arc']['supporting_episode_ids'] == [f'sig:{new_sig_id}']
            ep_by_kind = {e['kind']: e for e in jj['episodes']}
            assert ep_by_kind['signal']['episode_id'] == f'sig:{new_sig_id}'
            assert ep_by_kind['signal']['evidence_node_ids'] == [new_sig_id]
            assert ep_by_kind['intervention']['episode_id'] == f'int:{new_int_node_id}'
            assert ep_by_kind['outcome']['episode_id'] == f'out:{new_outcome_node_id}'
            new_hs_id = new_hs[0].health_score_id
            assert ep_by_kind['health_transition']['episode_id'] == f'hs:{new_hs_id}'
            assert jj['leading_vs_trailing']['series'][0]['contributing_episode_ids'] == [f'sig:{new_sig_id}']
            assert jj['counterfactual_hooks'][0]['episode_id'] == f'int:{new_int_node_id}'
            assert jj['counterfactual_hooks'][0]['outcomes_after'][0]['episode_id'] == f'out:{new_outcome_node_id}'
            assert jj['forecast']['run_id'] == new_fr.run_id
            assert jj['forecast']['cites'] == [f'sig:{new_sig_id}']
            assert set(jj['narrative']['cited_episode_ids']) == {f'sig:{new_sig_id}', f'hs:{new_hs_id}'}

            new_qs = QualitativeSignal.query.filter_by(customer_id=new_cid).first()
            assert new_qs.cg_node_id == new_sig_id
            new_sr = SignalReview.query.filter_by(customer_id=new_cid).first()
            assert new_sr.node_id == new_sig_id

    def test_scoped_correctly_no_cross_tenant_leakage(self, source):
        """Cloning customer B must never touch customer A's rows, and the clone's rows
        must be readable ONLY under the clone's own customer_id (tenant isolation)."""
        other = _build_rich_source()   # a second, unrelated source tenant
        with app.app_context():
            before_other_accounts = {a.account_id for a in Account.query.filter_by(customer_id=other['customer_id']).all()}
            before_source_accounts = {a.account_id for a in Account.query.filter_by(customer_id=source['customer_id']).all()}

        result = _clone(source['customer_id'])
        new_cid = result['customer_id']

        with app.app_context():
            # the untouched sibling tenant is byte-for-byte the same afterwards
            after_other_accounts = {a.account_id for a in Account.query.filter_by(customer_id=other['customer_id']).all()}
            assert after_other_accounts == before_other_accounts
            # the source itself is untouched (clone_customer never writes to the source)
            after_source_accounts = {a.account_id for a in Account.query.filter_by(customer_id=source['customer_id']).all()}
            assert after_source_accounts == before_source_accounts
            assert User.query.filter_by(customer_id=source['customer_id']).count() == 1   # still just the one source admin

            # nothing belonging to `other` leaked into the new clone
            new_accounts = {a.account_id for a in Account.query.filter_by(customer_id=new_cid).all()}
            assert new_accounts.isdisjoint(before_other_accounts)
            new_nodes = ContextNode.query.filter_by(customer_id=new_cid).all()
            assert all(n.account_id in new_accounts for n in new_nodes)


class TestCloneCustomerFeatureToggleSanitization:
    def test_playbooks_webhook_secret_and_url_stripped_but_policy_kept(self, source):
        result = _clone(source['customer_id'])
        with app.app_context():
            t = FeatureToggle.query.filter_by(customer_id=result['customer_id'], feature_name='playbooks').first()
            assert t is not None
            assert 'webhook_url' not in t.config
            assert 'webhook_secret' not in t.config
            assert 'slack_webhook_url' not in t.config
            assert t.config.get('automation_level') == 1              # policy-only fields survive
            assert t.config.get('disabled_playbooks') == ['some_playbook']

    def test_signal_engine_slack_wiring_stripped(self, source):
        result = _clone(source['customer_id'])
        with app.app_context():
            t = FeatureToggle.query.filter_by(customer_id=result['customer_id'], feature_name='signal_engine').first()
            assert t is not None
            assert t.config == {}

    def test_openai_key_never_copied(self, source):
        result = _clone(source['customer_id'])
        with app.app_context():
            cfg = CustomerConfig.query.filter_by(customer_id=result['customer_id']).first()
            assert cfg.openai_api_key_encrypted is None
            assert cfg.pillar_weights == {'P1': 0.5, 'P2': 0.5}        # non-secret config still copied


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
