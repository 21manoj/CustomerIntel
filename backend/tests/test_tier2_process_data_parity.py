"""
Tier 2A-3 live-parity regression: the real load-driver output for
customer 359 (datacenter_v1 — 12 accounts, 8112 KPI rows, 55 signals, 40
outcomes) run through the new pipeline must reproduce exactly what the old
repo's _process_data_impl produced from the same files (diffed 2026-09-01:
zero delta on every ingest-produced row; only the deliberate created_by
rename differed). Pinned here so the check survives the old repo.

Fixture note: these files carry `account_id`, not `source_account_id` —
the load-driver only ever went through the old repo's REST upload path
and never met upload_csv's strict schema. upload_csv now accepts the
alias (utils/csv_upload._COLUMN_ALIASES), so this goes through the real
tool with strict validation, the same way a load-driver run would.
"""
import os
import sys
import uuid
from collections import Counter
from pathlib import Path

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
    Account, KPIMeasurement, QualitativeSignal, ContextNode, ContextEdge, CsvUploadStaging, HealthScore,
)

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'customer359_datacenter_v1'
FILES = ('account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'outcomes.csv')


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(
            f"test_tier2_process_data_parity.py refuses to run against database "
            f"{db_name!r} — its name doesn't contain 'test'."
        )


@pytest.fixture(scope='module')
def loaded():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(
            name=f'Parity 359 {tag}', domain=f'parity359-{tag}.test', vertical='datacenter_v1',
            admin_email=f'parity359_{tag}@t.test', admin_name='Parity',
        )['customer_id']
        for ft in FILES:
            r = upload_csv(cid, ft, (FIXTURES / ft).read_text())
            assert r['row_count'] > 0, r
        res = process_data(cid)
        yield cid, res
        db.session.remove()
        db.drop_all()


def _accounts(cid):
    return Account.query.filter_by(customer_id=cid).order_by(Account.account_name).all()


def test_pipeline_succeeds_and_consumes_staging(loaded):
    cid, res = loaded
    assert res['status'] == 'success', res
    assert res['errors'] == []
    assert res['accounts'] == 12
    assert res['kpi_measurements'] == 8112
    with app.app_context():
        assert CsvUploadStaging.query.filter_by(customer_id=cid).count() == 0


def test_accounts(loaded):
    cid, _ = loaded
    with app.app_context():
        accts = _accounts(cid)
        assert [a.account_name for a in accts] == [
            'Apex Compute', 'Cirrus AI', 'Helix Compute', 'Meridian AI', 'Nova Foundry',
            'Orion Models', 'Pacific Dataworks', 'Quantum Labs', 'Stellar Inference',
            'Titan Hyperscale Labs', 'Vector Dynamics', 'Zenith Training',
        ]
        assert sum(float(a.revenue) for a in accts) == 50_000_000.0
        assert all(a.vertical == 'datacenter_v1' for a in accts)
        assert all(a.external_account_id for a in accts)
        assert [len(a.profile_metadata['products']) for a in accts] == [4, 5, 4, 5, 3, 3, 4, 4, 3, 4, 5, 4]
        # 'product_adoption' is written by the Stage 2b back-fill (Tier 2A-4).
        # The old repo's back-fill ran on this data too but never persisted
        # the key — it mutated the loaded JSON dict in place and reassigned
        # the same object, which SQLAlchemy's by-value change detection
        # treats as unchanged (verified: NULL on all 12 accounts there).
        assert sorted(accts[0].profile_metadata) == [
            'cloud_provider', 'contract_end', 'contract_start', 'csm_email', 'csm_manager',
            'csm_name', 'deployment_type', 'employee_count', 'primary_champion_engagement_score',
            'product_adoption', 'products', 'renewal_date', 'tech_stack', 'tier',
        ]
        # blank champion/sponsor cells never became the string 'nan'
        assert not any(str(v) == 'nan' for a in accts for v in a.profile_metadata.values())


def test_kpis(loaded):
    cid, _ = loaded
    with app.app_context():
        ids = [a.account_id for a in _accounts(cid)]
        kpis = KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(ids)).all()
        assert len(kpis) == 8112
        assert Counter(k.account_id for k in kpis) == {i: 676 for i in ids}
        assert len({k.kpi_code for k in kpis}) == 38
        assert round(sum(float(k.value) for k in kpis), 2) == 982536.6
        assert all(k.target is not None for k in kpis)


def test_signals(loaded):
    cid, _ = loaded
    with app.app_context():
        sigs = QualitativeSignal.query.filter_by(customer_id=cid).all()
        assert len(sigs) == 55
        assert all(s.signal_id.startswith(f'c{cid}_') for s in sigs)   # tenant-scoped ids
        assert len({s.signal_id for s in sigs}) == 55
        assert all(s.sentiment_score is not None for s in sigs)
        assert all(s.stakeholder_roles for s in sigs)
        assert not any(str(r.get('name')) == 'nan' for s in sigs for r in s.stakeholder_roles)


def test_context_nodes(loaded):
    cid, _ = loaded
    with app.app_context():
        nodes = ContextNode.query.filter(
            ContextNode.customer_id == cid,
            ContextNode.source_platform.in_(['csv_import', 'account_details_extraction']),
        ).all()
        by_type = {t: [n for n in nodes if n.node_type == t] for t in ('SIGNAL', 'STAKEHOLDER', 'OUTCOME')}
        assert {t: len(v) for t, v in by_type.items()} == {'SIGNAL': 55, 'STAKEHOLDER': 24, 'OUTCOME': 40}

        assert all(n.tier == 2 and float(n.confidence) == 1.0 for n in by_type['SIGNAL'])
        assert len({n.source_event_id for n in by_type['SIGNAL']}) == 55

        assert Counter(n.node_subtype for n in by_type['STAKEHOLDER']) == {'csm': 12, 'cs_manager': 12}
        assert all(n.tier == 1 and n.source_platform == 'account_details_extraction' for n in by_type['STAKEHOLDER'])

        # No evidence column in this outcomes.csv → every OUTCOME is I3'-clamped
        assert all(n.tier == 2 and float(n.confidence) == 0.3 for n in by_type['OUTCOME'])
        assert all(n.properties.get('evidence_clamped') is True for n in by_type['OUTCOME'])
        assert Counter(n.node_subtype for n in by_type['OUTCOME']) == {
            'renewal_secured': 12, 'revenue_protected': 7, 'expansion_approved': 5,
            'engagement_decline': 5, 'renewal_uncertainty': 5, 'capacity_constraint': 2,
            'churn_averted': 2, 'revenue_at_risk': 2,
        }
        assert sum(float(n.revenue_impact) for n in by_type['OUTCOME']) == 3_280_000.0


def _expected_health_rows():
    import csv
    with open(FIXTURES / 'expected_health_scores_old_repo.csv') as f:
        return list(csv.DictReader(f))


def test_health_scores_match_old_repo(loaded):
    """Tier 2A-4 parity: the old repo's full pipeline wrote 72 HealthScore
    rows for this data (12 accounts x Nov 2025..Apr 2026); score, status
    and pillar breakdown must match exactly. change_from_last_month is
    the one column that must NOT match: the old raw-SQL UPDATE subtracted
    the row's own score from itself, so it was 0.00 on every row of the
    fixture (see calculate_health_scores docstring); here it must equal
    the real month-over-month delta."""
    import json
    from datetime import date
    cid, res = loaded
    assert any(s == 'health_scores_auto_72_written' for s in res['steps_completed']), res['steps_completed']
    expected = _expected_health_rows()
    assert len(expected) == 72
    with app.app_context():
        names = {a.account_id: a.account_name for a in _accounts(cid)}
        got = {
            (names[hs.account_id], hs.measurement_month): hs
            for hs in HealthScore.query.filter(HealthScore.account_id.in_(list(names))).all()
        }
        assert len(got) == 72
        prev_by_acct = {}
        for row in expected:
            key = (row['account_name'], date.fromisoformat(row['measurement_month']))
            hs = got[key]
            assert float(hs.health_score) == float(row['health_score']), key
            assert hs.health_status == row['health_status'], key
            assert hs.contributing_pillars == json.loads(row['contributing_pillars']), key
            assert hs.kpi_only_score == hs.health_score
            assert row['change_from_last_month'] in ('', '0.00')   # the old bug, on record
            prev = prev_by_acct.get(row['account_name'])
            if prev is None:
                assert hs.change_from_last_month is None, key
            else:
                assert float(hs.change_from_last_month) == round(float(hs.health_score) - prev, 2), key
            prev_by_acct[row['account_name']] = float(hs.health_score)


def test_account_status_synced_like_old_repo(loaded):
    """Item 28 sync produced this split in the old repo for the same data."""
    cid, _ = loaded
    with app.app_context():
        got = {a.account_name: a.account_status for a in _accounts(cid)}
        assert got == {
            'Apex Compute': 'active', 'Cirrus AI': 'at_risk', 'Helix Compute': 'at_risk',
            'Meridian AI': 'at_risk', 'Nova Foundry': 'at_risk', 'Orion Models': 'at_risk',
            'Pacific Dataworks': 'active', 'Quantum Labs': 'at_risk', 'Stellar Inference': 'active',
            'Titan Hyperscale Labs': 'at_risk', 'Vector Dynamics': 'active', 'Zenith Training': 'active',
        }


def test_adoption_backfill_uses_p6_for_datacenter(loaded):
    cid, _ = loaded
    with app.app_context():
        for a in _accounts(cid):
            latest = HealthScore.query.filter_by(account_id=a.account_id).order_by(
                HealthScore.measurement_month.desc()).first()
            assert a.profile_metadata['product_adoption'] == round(float(latest.contributing_pillars['P6']), 1)


def _old_wizard_a():
    import csv
    import json
    d = FIXTURES / 'old_repo_wizard_a'
    arcs = {r['account_name']: r for r in csv.DictReader(open(d / 'accounts_arcs.csv'))}
    traj = {}
    for r in csv.DictReader(open(d / 'generated_nodes.csv')):
        if r['node_subtype'] == 'arc_detection':
            traj[r['account_name']] = json.loads(r['properties'])['arc_type']
    return arcs, traj


def test_wizard_a_v2_runs_and_writes_journeys(loaded):
    cid, res = loaded
    from models import JourneyData
    assert any(s.startswith('wizard_a_12_journeys_') for s in res['steps_completed']), res['steps_completed']
    assert res['wizard_a']['coverage']['classified'] + res['wizard_a']['coverage']['steady'] \
        + res['wizard_a']['coverage']['unclassified'] == 12
    with app.app_context():
        assert JourneyData.query.filter_by(customer_id=cid).count() == 12


def test_wizard_a_v2_vs_old_repo_cited_disagreements(loaded):
    """Tier 2A-5 parity contract (docs/design/wizard-a-assessment.md §5):
    not 'reproduce the old output' — the old output is wrong for a
    documented share of accounts — but: health-derived fields match
    exactly; no arc without evidence; every disagreement with the old arc
    is explainable from the journey's own evidence."""
    from models import JourneyData
    cid, _ = loaded
    old_arcs, old_traj = _old_wizard_a()
    with app.app_context():
        names = {a.account_id: a.account_name for a in _accounts(cid)}
        rows = {names[r.account_id]: r.journey_json for r in JourneyData.query.filter_by(customer_id=cid).all()}
    assert set(rows) == set(old_arcs)

    table = []
    for name, j in sorted(rows.items()):
        old = old_arcs[name]
        arc = j['arc']
        # 1. Same scores → the old trajectory classifier, kept as a feature, must agree exactly.
        assert j['summary']['trajectory'] == old_traj[name], (name, j['summary']['trajectory'], old_traj[name])
        # 2. No arc is asserted without cited episodes.
        if arc['state'] == 'classified':
            assert arc['supporting_episode_ids'], name
            assert arc['arc_type'] != 'competitive_displacement' or 'commercial_pressure' in arc['observed_roles'], name
            assert arc['arc_type'] != 'exec_sponsor_change' or 'champion_change' in arc['observed_roles'], name
        # 3. The old fallback (competitive_displacement @ 0.55) never survives as-is.
        if old['arc_type'] == 'competitive_displacement' and float(old['arc_confidence']) == 0.55:
            assert not (arc['arc_type'] == 'competitive_displacement' and not arc['supporting_episode_ids']), name
        table.append((name, old['arc_type'], float(old['arc_confidence']), arc['state'], arc['arc_type'],
                      ','.join(arc['observed_roles']), j['leading_vs_trailing']['lead_days']))

    # Printed for the record when run with -s: old arc vs v2 state/arc with the evidence roles.
    print('\n%-24s %-26s %-5s %-13s %-24s %s' % ('account', 'old_arc', 'conf', 'v2_state', 'v2_arc', 'roles | lead_days'))
    for r in table:
        print('%-24s %-26s %-5s %-13s %-24s %s | %s' % r)
    # Every account got a determinate state and the leading layer was written.
    assert all(r[3] in ('classified', 'steady', 'unclassified') for r in table)
    with app.app_context():
        assert HealthScore.query.join(Account, Account.account_id == HealthScore.account_id).filter(
            Account.customer_id == cid, HealthScore.qual_score.isnot(None)).count() > 0


def test_linked_signal_edges(loaded):
    """Item 37a: every one of the 40 outcomes carries a resolvable
    linked_signal_id → 40 LED_TO edges, all stamped unknown/unattributed."""
    cid, _ = loaded
    with app.app_context():
        edges = ContextEdge.query.filter_by(customer_id=cid, source_platform='csv_import').all()
        assert len(edges) == 40
        assert {e.edge_type for e in edges} == {'LED_TO'}
        assert {e.created_by for e in edges} == {'process_data.linked_signal_id'}
        from utils.provenance import UNKNOWN
        from utils.edge_factory import CSV_IMPORT_DERIVATION
        assert all(e.properties['evidence_tier'] == UNKNOWN for e in edges)
        assert all(e.properties['derivation'] == CSV_IMPORT_DERIVATION for e in edges)
        outcome_ids = {n.node_id for n in ContextNode.query.filter_by(customer_id=cid, node_type='OUTCOME').all()}
        assert {e.to_node_id for e in edges} == outcome_ids


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
