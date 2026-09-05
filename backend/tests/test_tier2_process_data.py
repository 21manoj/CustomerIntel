"""
Tier 2A-3 checkpoint: process_data's CSV-ingest half, end to end against a
real Postgres DB — create_customer → upload_csv x N → process_data →
assert on the actual rows. Covers the canonical 4-CSV registration plus
every context-graph file type, the staging-consumed contract, idempotent
re-runs, and the item-37a / I3' / item-38 behaviors the old repo's tests
pinned with static source checks.
"""
import os
import sys
import uuid

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

import mcp_server.common as _common
_common._flask_app = app

from models import (
    Customer, CustomerConfig, Account, KPIMeasurement, QualitativeSignal,
    ContextNode, ContextEdge, CsvUploadStaging,
)
from fastmcp.exceptions import ToolError


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_process_data.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


# ── Fixtures shaped like the load-driver's real output for datacenter_v1
#    (customer359), trimmed to 2 accounts. Column names satisfy
#    config/csv_schemas.json's required sets (source_account_id, not the
#    load-driver's account_id — the resolver accepts both).

ACCOUNT_DETAILS_CSV = (
    "source_account_id,account_name,industry,region,arr,csm_name,csm_email,csm_manager,"
    "executive_sponsor,primary_champion_name,primary_champion_title,tier,employee_count,"
    "products,contract_end,renewal_date\n"
    'ACC-1,Titan Hyperscale Labs,Telecommunications,North America,8200000,Sarah Rivera,'
    'sarah.rivera@x.com,Sam Rivera,,Riley Foster,Director of IT,Enterprise,4592,'
    '"[{""name"": ""Managed Kubernetes"", ""category"": ""platform"", ""arr"": 2267637}]",'
    '2026-06-15,2026-06-15\n'
    'ACC-2,Meridian AI,AI/ML,North America,4100000,Alex Chen,alex.chen@x.com,Sam Rivera,'
    'Dana Wu,,,Mid-Market,509,,2026-05-20,2026-05-20\n'
)

KPI_CSV = (
    "source_account_id,kpi_code,kpi_name,pillar,measured_at,value,target,weight,status\n"
    "ACC-1,P1-KPI1,Realized $/GPU-hour,P1,2025-11-01,1.22,2.5,0.25,critical\n"
    "ACC-1,P1-KPI1,Realized $/GPU-hour,P1,2025-12-01,1.30,2.5,0.25,critical\n"
    "ACC-1,P1-KPI1,Realized $/GPU-hour,P1,2025-12-01,9.99,2.5,0.25,critical\n"  # in-file dup
    "ACC-2,P1-KPI1,Realized $/GPU-hour,P1,2025-11-01,2.10,,0.25,warning\n"     # blank target
    "ACC-2,P2-KPI1,Uptime,P2,2025-11-01,99.9,99.95,0.3,ok\n"
)

SIGNALS_CSV = (
    "signal_id,source_account_id,signal_date,signal_type,content,sentiment,sentiment_score,"
    "stakeholder_name,stakeholder_title,signal_ref\n"
    "narrative_sig_1,ACC-1,2025-12-01,critical_incident,Critical service incident reported,"
    "negative,-0.66,Riley Foster,Director of IT,narrative_sig_1\n"
    "narrative_sig_2,ACC-1,2025-12-08,support_escalation,Support ticket escalated,"
    "negative,-0.71,,,narrative_sig_2\n"
    "narrative_sig_3,ACC-2,2025-12-08,nps,NPS 9 from admin,positive,,Dana Wu,,\n"  # no ref, unknown subtype → still evidence (extracted/unclassified)
)

OUTCOMES_CSV = (
    "source_account_id,outcome_date,outcome_type,title,revenue_value,evidence,linked_signal_id\n"
    "ACC-1,2026-03-01,revenue_at_risk,Revenue at Risk — Titan,-4100000.0,,narrative_sig_1\n"
    "ACC-1,2026-03-08,capacity_constraint,Capacity Issues — Titan,-1230000.0,"
    "Ticket #4412 + exec email,narrative_sig_1\n"
    "ACC-2,2026-03-01,expansion,Expansion — Meridian,300000,,narrative_sig_999\n"  # dangling ref
)

DECISIONS_CSV = (
    "source_account_id,decision_id,decision_date,title,decision_maker_role,chosen_option\n"
    "ACC-1,dec_1,2026-01-10,Escalate to exec sponsor,escalation,Weekly exec sync\n"
    "ACC-2,dec_2,2026-01-12,Renewal strategy,renewal_strategy,Multi-year offer\n"
)

STAKEHOLDERS_CSV = (
    "source_account_id,stakeholder_name,title,role,influence_score,email\n"
    "ACC-1,Riley Foster,Director of IT,champion,0.9,riley@x.com\n"   # dup of extracted champion
    "ACC-1,Jordan Lee,CFO,economic_buyer,0.8,jordan@x.com\n"
)

ENGAGEMENT_CSV = (
    "source_account_id,event_date,event_type,description,channel\n"
    "ACC-1,2026-01-05,qbr,Q4 business review,zoom\n"
)

PROFILES_CSV = (
    "source_account_id,arr,industry,employee_count,mrr\n"
    "ACC-2,4200000,AI/ML,520,350000\n"
)

BENCHMARKS_CSV = (
    "kpi_code,kpi_name,pillar,unit,p25,p50,p75,p90,source\n"
    "P1-KPI1,Realized $/GPU-hour,P1,$/gpu-hr,1.0,1.8,2.4,3.0,Gartner\n"
)

SIGNAL_EDGES_CSV = (
    "source_account_id,from_signal_ref,to_signal_ref,edge_type,weight,confidence,lag_days\n"
    "ACC-1,narrative_sig_1,narrative_sig_2,LED_TO,1.0,0.9,7\n"
    "ACC-1,narrative_sig_2,dec_1,LED_TO,0.8,0.7,\n"      # unprefixed decision ref
    "ACC-1,narrative_sig_1,missing_ref,LED_TO,1.0,,\n"   # unresolvable → skipped
)


@pytest.fixture(scope='module')
def customer_id():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        res = create_customer(data_origin='synthetic_test', 
            name=f'Process Test {tag}', domain=f'process-{tag}.test',
            vertical='datacenter_v1',
            admin_email=f'admin_{tag}@process.test', admin_name='Admin',
        )
        cid = res['customer_id']
        yield cid
        db.session.remove()
        db.drop_all()


def _upload_all(cid, files: dict):
    from mcp_server.cs_pulse_onboarding import upload_csv
    for ft, content in files.items():
        upload_csv(cid, ft, content)


def _nodes(cid, node_type, **kw):
    return ContextNode.query.filter_by(customer_id=cid, node_type=node_type, **kw).all()


class TestGuards:
    def test_no_data_and_nothing_staged_errors(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        with pytest.raises(ToolError, match='No data found'):
            process_data(customer_id)

    def test_unknown_customer_errors(self):
        from mcp_server.cs_pulse_onboarding import process_data
        with pytest.raises(ToolError, match='not found'):
            process_data(999999999)

    def test_unset_vertical_fails_closed_not_dc2s(self):
        from mcp_server.cs_pulse_onboarding import process_data
        with app.app_context():
            tag = uuid.uuid4().hex[:8]
            c = Customer(customer_name='NoVertical', email=f'nv_{tag}@t.test', domain=f'nv-{tag}.test')
            db.session.add(c)
            db.session.flush()
            db.session.add(CustomerConfig(customer_id=c.customer_id, vertical=None))
            db.session.commit()
            cid = c.customer_id
        _upload_all(cid, {'account_details.csv': ACCOUNT_DETAILS_CSV})
        with pytest.raises(ToolError, match='Cannot resolve vertical'):
            process_data(cid)


class TestCanonicalRegistration:
    """The 4-CSV path the load-driver actually exercises."""

    def test_end_to_end(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        _upload_all(customer_id, {
            'account_details.csv': ACCOUNT_DETAILS_CSV,
            'kpi_measurements.csv': KPI_CSV,
            'enhanced_qualitative_signals.csv': SIGNALS_CSV,
            'outcomes.csv': OUTCOMES_CSV,
        })
        res = process_data(customer_id)
        assert res['status'] == 'success', res
        assert res['errors'] == []
        assert res['vertical'] == 'datacenter_v1'
        assert sorted(res['csv_files_processed']) == [
            'account_details.csv', 'enhanced_qualitative_signals.csv',
            'kpi_measurements.csv', 'outcomes.csv',
        ]
        assert 'staging_consumed' in res['steps_completed']

        with app.app_context():
            accts = {a.account_name: a for a in Account.query.filter_by(customer_id=customer_id).all()}
            assert set(accts) == {'Titan Hyperscale Labs', 'Meridian AI'}
            titan = accts['Titan Hyperscale Labs']
            assert float(titan.revenue) == 8200000
            assert titan.vertical == 'datacenter_v1'
            assert titan.external_account_id == 'ACC-1'    # persisted so later KPI-only uploads resolve
            pm = titan.profile_metadata
            assert pm['csm_name'] == 'Sarah Rivera'
            assert pm['attributes']['employee_count'] == 4592   # an extension now (folded, coerced to int); not a named field
            assert pm['products'][0]['name'] == 'Managed Kubernetes'
            assert 'executive_sponsor' not in pm           # blank cell → absent, never 'nan'

            # KPIs: 5 rows, 1 in-file duplicate → 4; blank target → NULL not 100
            kpis = KPIMeasurement.query.filter(
                KPIMeasurement.account_id.in_([a.account_id for a in accts.values()])).all()
            assert len(kpis) == 4
            assert res['kpi_measurements'] == 4
            meridian_p1 = next(k for k in kpis if k.account_id == accts['Meridian AI'].account_id
                               and k.kpi_code == 'P1-KPI1')
            assert meridian_p1.target is None
            assert float(meridian_p1.value) == 2.10

            # Signals: 3 rows, customer-scoped ids, blank score → NULL
            sigs = QualitativeSignal.query.filter_by(customer_id=customer_id).all()
            assert len(sigs) == 3
            assert all(s.signal_id.startswith(f'c{customer_id}_') for s in sigs)
            nps = next(s for s in sigs if s.signal_type == 'nps')
            assert nps.sentiment_score is None
            assert nps.stakeholder_roles == [{'name': 'Dana Wu', 'role': 'contact'}]
            esc = next(s for s in sigs if s.signal_type == 'support_escalation')
            assert esc.stakeholder_roles is None            # blank name → None, never [{'name':'nan'}]

            # Signals-first: every row is evidence — one node per signal, written by
            # the engine. Rows with a ref keep it as source_event_id; the unreferenced
            # 'nps' row (unknown subtype) went through extraction under its own id.
            sig_nodes = _nodes(customer_id, 'SIGNAL')
            assert len(sig_nodes) == 3
            assert {n.source_event_id for n in sig_nodes} >= {'narrative_sig_1', 'narrative_sig_2'}
            n1 = next(n for n in sig_nodes if n.source_event_id == 'narrative_sig_1')
            assert n1.properties['stakeholder_name'] == 'Riley Foster'
            assert n1.properties['classification_basis'] == 'declared_subtype' and n1.properties['role'] == 'infra_incident'
            assert n1.properties['effective_urgency'] and n1.source_platform == 'csv_import'
            n2 = next(n for n in sig_nodes if n.source_event_id == 'narrative_sig_2')
            assert 'stakeholder_name' not in n2.properties
            n3 = next(n for n in sig_nodes if n.source_event_id not in ('narrative_sig_1', 'narrative_sig_2'))
            assert n3.properties['classification_basis'] in ('llm_extraction', 'unclassified')

            # STAKEHOLDER nodes extracted from account_details profile fields:
            # Titan: champion + csm + cs_manager (sponsor blank) = 3
            # Meridian: executive_sponsor + csm + cs_manager (champion blank) = 3
            stk = _nodes(customer_id, 'STAKEHOLDER')
            assert len(stk) == 6
            titan_roles = {n.node_subtype for n in stk if n.account_id == titan.account_id}
            assert titan_roles == {'champion', 'csm', 'cs_manager'}
            champ = next(n for n in stk if n.node_subtype == 'champion')
            assert champ.title == 'Riley Foster (Director of IT)'
            assert champ.source_platform == 'account_details_extraction'

    def test_outcomes_clamped_and_linked(self, customer_id):
        """I3' clamp on evidence-less csv_import OUTCOMEs; item 37a LED_TO
        edges from linked_signal_id, dangling refs skipped."""
        with app.app_context():
            outs = {n.title: n for n in _nodes(customer_id, 'OUTCOME')}
            assert set(outs) == {'Revenue at Risk — Titan', 'Capacity Issues — Titan', 'Expansion — Meridian'}
            no_evidence = outs['Revenue at Risk — Titan']
            with_evidence = outs['Capacity Issues — Titan']
            assert float(no_evidence.confidence) < 1.0
            assert no_evidence.tier > 1
            assert float(with_evidence.confidence) == 1.0
            assert with_evidence.tier == 1
            assert float(no_evidence.revenue_impact) == -4100000.0
            assert no_evidence.source_event_id.startswith('outcome:')

            edges = ContextEdge.query.filter_by(
                customer_id=customer_id, created_by='process_data.linked_signal_id').all()
            assert len(edges) == 2                       # 2 resolvable refs, 1 dangling
            assert {e.to_node_id for e in edges} == {no_evidence.node_id, with_evidence.node_id}
            sig1 = next(n for n in _nodes(customer_id, 'SIGNAL') if n.source_event_id == 'narrative_sig_1')
            assert all(e.from_node_id == sig1.node_id for e in edges)
            assert all(e.edge_type == 'LED_TO' for e in edges)
            from utils.provenance import UNKNOWN
            from utils.edge_factory import CSV_IMPORT_DERIVATION
            assert all(e.properties['evidence_tier'] == UNKNOWN for e in edges)
            assert all(e.properties['derivation'] == CSV_IMPORT_DERIVATION for e in edges)

    def test_staging_consumed(self, customer_id):
        with app.app_context():
            assert CsvUploadStaging.query.filter_by(customer_id=customer_id).count() == 0

    def test_rerun_without_new_uploads_takes_db_path(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        res = process_data(customer_id)
        assert res['status'] == 'success'
        assert res['csv_files_processed'] is None
        assert any(s.startswith('data_already_in_db_2_accounts_4_kpis') for s in res['steps_completed'])

    def test_reupload_and_rerun_is_idempotent(self, customer_id):
        """Same 4 files again: zero new KPIs/signals/nodes/edges."""
        from mcp_server.cs_pulse_onboarding import process_data
        with app.app_context():
            before = (
                KPIMeasurement.query.count(),
                QualitativeSignal.query.filter_by(customer_id=customer_id).count(),
                ContextNode.query.filter_by(customer_id=customer_id).count(),
                ContextEdge.query.filter_by(customer_id=customer_id).count(),
            )
        _upload_all(customer_id, {
            'account_details.csv': ACCOUNT_DETAILS_CSV,
            'kpi_measurements.csv': KPI_CSV,
            'enhanced_qualitative_signals.csv': SIGNALS_CSV,
            'outcomes.csv': OUTCOMES_CSV,
        })
        res = process_data(customer_id)
        assert res['status'] == 'success', res
        assert 'kpis_loaded_0' in res['steps_completed']
        assert 'signals_queued_0_skipped_3' in res['steps_completed']          # re-upload: the engine already has them
        assert 'outcomes_loaded_0' in res['steps_completed']
        with app.app_context():
            after = (
                KPIMeasurement.query.count(),
                QualitativeSignal.query.filter_by(customer_id=customer_id).count(),
                ContextNode.query.filter_by(customer_id=customer_id).count(),
                ContextEdge.query.filter_by(customer_id=customer_id).count(),
            )
        assert after == before

    def test_incremental_kpi_upload_adds_only_new_rows(self, customer_id):
        """KPI-only upload after the accounts file was consumed: rows must
        still resolve via the persisted external_account_id."""
        from mcp_server.cs_pulse_onboarding import process_data
        _upload_all(customer_id, {'kpi_measurements.csv': KPI_CSV + "ACC-1,P1-KPI1,x,P1,2026-01-01,1.4,2.5,0.25,warning\n"})
        res = process_data(customer_id)
        assert res['status'] == 'success', res
        assert 'kpis_loaded_1' in res['steps_completed']
        assert res['kpi_measurements'] == 5


class TestContextGraphFiles:
    """Every non-canonical file type, plus item 38 linking against
    decisions.csv-sourced DECISION nodes."""

    def test_cg_files_load(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        _upload_all(customer_id, {
            'decisions.csv': DECISIONS_CSV,
            'stakeholders.csv': STAKEHOLDERS_CSV,
            'engagement_events.csv': ENGAGEMENT_CSV,
            'account_business_profiles.csv': PROFILES_CSV,
            'industry_benchmarks.csv': BENCHMARKS_CSV,
            'signal_edges.csv': SIGNAL_EDGES_CSV,
        })
        res = process_data(customer_id)
        assert res['status'] == 'success', res
        steps = res['steps_completed']
        assert 'decisions_loaded_2' in steps
        assert 'stakeholders_loaded_1' in steps          # Riley Foster deduped against extracted champion
        assert 'engagement_events_loaded_1' in steps
        assert 'profiles_loaded_1' in steps
        assert 'benchmarks_loaded_1' in steps
        assert 'edges_loaded_2' in steps                 # 1 unresolvable ref skipped
        assert 'staging_consumed' in steps

        with app.app_context():
            decs = {n.properties['decision_id']: n for n in _nodes(customer_id, 'DECISION')}
            assert decs['dec_1'].source_event_id == 'decision:dec_1'
            assert decs['dec_1'].node_subtype == 'escalation'

            stk = _nodes(customer_id, 'STAKEHOLDER')
            assert len(stk) == 7
            jordan = next(n for n in stk if n.title == 'Jordan Lee')
            assert jordan.node_subtype == 'economic_buyer'
            assert jordan.properties['influence_score'] == '0.8'

            # business profile MERGED into profile_metadata, not replacing it
            meridian = Account.query.filter_by(customer_id=customer_id, account_name='Meridian AI').first()
            assert meridian.profile_metadata['mrr'] == 350000
            assert meridian.profile_metadata['csm_name'] == 'Alex Chen'   # from account_details, preserved
            assert float(meridian.revenue) == 4200000

            bench = _nodes(customer_id, 'EXTERNAL_CONTEXT')[0]
            assert bench.properties['p50'] == '1.8'          # schema column names, not industry_p50
            assert bench.properties['benchmark_source'] == 'Gartner'

            se = ContextEdge.query.filter_by(customer_id=customer_id, created_by='process_data.signal_edges').all()
            assert len(se) == 2
            sig_by_ref = {n.source_event_id: n.node_id for n in _nodes(customer_id, 'SIGNAL') if n.source_event_id}
            e_sig = next(e for e in se if e.to_node_id == sig_by_ref['narrative_sig_2'])
            assert e_sig.from_node_id == sig_by_ref['narrative_sig_1']
            assert e_sig.lag_days == 7
            e_dec = next(e for e in se if e.to_node_id == decs['dec_1'].node_id)
            assert float(e_dec.confidence) == 0.7

            # the 37a LED_TO edges written earlier survived the signal_edges reload
            assert ContextEdge.query.filter_by(
                customer_id=customer_id, created_by='process_data.linked_signal_id').count() == 2

    def test_item38_stakeholder_decision_linking(self, customer_id):
        """executive_sponsor ↔ 'escalation' decision on ACC-1; champion ↔
        'renewal_strategy' on ACC-2 has no champion so no edge there;
        csm/cs_manager on ACC-1 match 'escalation' only for cs_manager."""
        with app.app_context():
            inv = ContextEdge.query.filter_by(customer_id=customer_id, edge_type='INVOLVES').all()
            nodes = {n.node_id: n for n in ContextNode.query.filter_by(customer_id=customer_id).all()}
            pairs = {(nodes[e.from_node_id].node_subtype, nodes[e.to_node_id].node_subtype) for e in inv}
            assert ('cs_manager', 'escalation') in pairs
            assert ('champion', 'escalation') not in pairs
            assert ('csm', 'escalation') not in pairs
            assert all(e.created_by == 'stakeholder_decision_linker' for e in inv)
            assert all(float(e.confidence) == 0.8 for e in inv)

    def test_cg_reupload_is_idempotent(self, customer_id):
        from mcp_server.cs_pulse_onboarding import process_data
        with app.app_context():
            before = (
                ContextNode.query.filter_by(customer_id=customer_id).count(),
                ContextEdge.query.filter_by(customer_id=customer_id).count(),
            )
        _upload_all(customer_id, {
            'decisions.csv': DECISIONS_CSV,
            'stakeholders.csv': STAKEHOLDERS_CSV,
            'engagement_events.csv': ENGAGEMENT_CSV,
            'industry_benchmarks.csv': BENCHMARKS_CSV,
            'signal_edges.csv': SIGNAL_EDGES_CSV,
        })
        res = process_data(customer_id)
        assert res['status'] == 'success', res
        assert 'decisions_loaded_0' in res['steps_completed']
        assert 'stakeholders_loaded_0' in res['steps_completed']
        assert 'engagement_events_loaded_0' in res['steps_completed']
        assert 'benchmarks_loaded_0' in res['steps_completed']
        with app.app_context():
            after = (
                ContextNode.query.filter_by(customer_id=customer_id).count(),
                ContextEdge.query.filter_by(customer_id=customer_id).count(),
            )
        assert after == before


class TestFailureKeepsStaging:
    def test_bad_file_keeps_staged_rows_and_reports_partial(self):
        """A phase-2 failure must not consume staging, and phase-1 data
        committed before it must survive."""
        from mcp_server.cs_pulse_onboarding import create_customer, process_data
        from utils import csv_ingest
        with app.app_context():
            tag = uuid.uuid4().hex[:8]
            cid = create_customer(data_origin='synthetic_test', 
                name=f'Fail {tag}', domain=f'fail-{tag}.test', vertical='datacenter_v1',
                admin_email=f'fail_{tag}@t.test', admin_name='A',
            )['customer_id']
        _upload_all(cid, {
            'account_details.csv': ACCOUNT_DETAILS_CSV,
            'kpi_measurements.csv': KPI_CSV,
            'outcomes.csv': OUTCOMES_CSV,
        })
        original = csv_ingest.load_outcomes
        csv_ingest.load_outcomes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            res = process_data(cid)
        finally:
            csv_ingest.load_outcomes = original
        assert res['status'] == 'partial'
        assert any('context_graph: boom' in e for e in res['errors'])
        assert 'staging_consumed' not in res['steps_completed']
        with app.app_context():
            assert CsvUploadStaging.query.filter_by(customer_id=cid).count() == 3
            assert Account.query.filter_by(customer_id=cid).count() == 2
            assert res['kpi_measurements'] == 4
        # retry succeeds and consumes
        res2 = process_data(cid)
        assert res2['status'] == 'success', res2
        with app.app_context():
            assert CsvUploadStaging.query.filter_by(customer_id=cid).count() == 0
            assert len(_nodes(cid, 'OUTCOME')) == 3


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
