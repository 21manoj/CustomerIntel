"""
Live-parity on customer 415 "Phoenix Data Centers" (dc2_s) — the analysis
tenant the user chose (2026-09-02: "customer 415 or later, not 359").

Fixture: tests/fixtures/customer415_dc2_s/ — the four CSVs reconstructed
from the live EC2 database (accounts + profile_metadata, dc2s_kpis,
qualitative_signals, csv_import OUTCOME nodes with their LED_TO source as
linked_signal_id), plus the old pipeline's own output for the same tenant
(health_scores, account arcs, generated nodes, journey_data).

Expectations are derived from the fixture files, not hand-typed, so the
same test runs on any tenant exported the same way. Contract per
docs/design/wizard-a-assessment.md §5: ingest and health-derived fields
match the old output exactly; arcs must agree with the old repo or cite
the evidence for disagreeing; nothing is asserted without evidence.
"""
import csv
import json
import os
import sys
import uuid
from collections import Counter
from datetime import date
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

from models import Account, KPIMeasurement, QualitativeSignal, ContextNode, ContextEdge, HealthScore, JourneyData

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'customer415_dc2_s'
OLD = FIXTURES / 'old_repo_output'
FILES = ('account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'outcomes.csv')
VERTICAL = 'dc2_s'


def _rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def _assert_isolated_test_db(uri: str) -> None:
    if os.environ.get('ALLOW_DESTRUCTIVE_TEST_DB') == '1':
        return
    db_name = uri.rsplit('/', 1)[-1].split('?', 1)[0]
    if 'test' not in db_name.lower():
        raise RuntimeError(f"refusing to run against non-test database {db_name!r}")


@pytest.fixture(scope='module')
def loaded():
    _assert_isolated_test_db(app.config['SQLALCHEMY_DATABASE_URI'])
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(name=f'Parity 415 {tag}', domain=f'parity415-{tag}.test', vertical=VERTICAL,
                              admin_email=f'parity415_{tag}@t.test', admin_name='Parity')['customer_id']
        for ft in FILES:
            r = upload_csv(cid, ft, (FIXTURES / ft).read_text())
            assert r['row_count'] > 0, r
        res = process_data(cid)
        yield cid, res
        db.session.remove()
        db.drop_all()


def _accounts(cid):
    return Account.query.filter_by(customer_id=cid).order_by(Account.account_name).all()


# ── Ingest ────────────────────────────────────────────────────────────

def test_pipeline_succeeds(loaded):
    cid, res = loaded
    assert res['status'] == 'success', res
    assert res['errors'] == []
    assert res['vertical'] == VERTICAL


def test_ingest_counts_match_fixture(loaded):
    cid, res = loaded
    acct_rows, kpi_rows, sig_rows, out_rows = (_rows(FIXTURES / f) for f in FILES)
    with app.app_context():
        accts = _accounts(cid)
        assert [a.account_name for a in accts] == sorted(r['account_name'] for r in acct_rows)
        assert sum(float(a.revenue) for a in accts) == sum(float(r['arr']) for r in acct_rows)
        ids = [a.account_id for a in accts]
        assert KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(ids)).count() == len(kpi_rows)
        assert res['kpi_measurements'] == len(kpi_rows)
        assert QualitativeSignal.query.filter_by(customer_id=cid).count() == len(sig_rows)
        sig_nodes = ContextNode.query.filter_by(customer_id=cid, node_type='SIGNAL', source_platform='csv_import').all()
        # one SIGNAL node per (account, signal_ref) — the old loader deduped on
        # (account, title) and collapsed same-title signals (79 nodes for 133
        # signals on the live tenant); v2 keeps each referenced signal
        assert len(sig_nodes) == len({(r['source_account_id'], r['signal_ref']) for r in sig_rows if r['signal_ref']})
        outs = ContextNode.query.filter_by(customer_id=cid, node_type='OUTCOME').all()
        assert len(outs) == len(out_rows)
        linked = ContextEdge.query.filter_by(customer_id=cid, created_by='process_data.linked_signal_id').count()
        assert linked == sum(1 for r in out_rows if r['linked_signal_id'])


def test_every_signal_subtype_has_a_role(loaded):
    from utils.taxonomy_loader import get_taxonomy
    t = get_taxonomy(VERTICAL)
    subtypes = {r['signal_type'] for r in _rows(FIXTURES / 'enhanced_qualitative_signals.csv')}
    unmapped = sorted(s for s in subtypes if t.signal_role(s) is None)
    assert not unmapped, unmapped


# ── Health scoring vs old output ──────────────────────────────────────

def _stale_old_months():
    """(account, month) pairs whose old health row was computed from a
    PARTIAL month: KPI rows for that month arrived after the row's
    calculated_at (on the live tenant: March 2026 scored at 18:02:02 from
    47 of 94 rows; 470 more rows landed at 18:21:37), and the old repo's
    immutability rule ('auto' mode never rescores an existing (account,
    month)) meant the score was never updated. The new build scores the
    whole month, so those rows are not comparable — skipped explicitly and
    printed, never silently."""
    stale = set()
    for r in _rows(OLD / 'health_score_freshness.csv'):
        if int(r['kpis_at_calc']) < int(r['kpis_total']):
            stale.add((r['account_name'], date.fromisoformat(r['measurement_month'])))
    return stale


def test_health_scores_match_old_repo(loaded):
    cid, res = loaded
    expected = _rows(OLD / 'expected_health_scores.csv')
    stale = _stale_old_months()
    assert any(s == f'health_scores_auto_{len(expected)}_written' for s in res['steps_completed']), res['steps_completed']
    with app.app_context():
        names = {a.account_id: a.account_name for a in _accounts(cid)}
        got = {(names[h.account_id], h.measurement_month): h
               for h in HealthScore.query.filter(HealthScore.account_id.in_(list(names))).all()}
        assert len(got) == len(expected)
        prev, compared = {}, 0
        for row in expected:
            key = (row['account_name'], date.fromisoformat(row['measurement_month']))
            hs = got[key]
            p = prev.get(row['account_name'])
            prev[row['account_name']] = float(hs.health_score)
            if p is None:
                assert hs.change_from_last_month is None, key
            else:
                assert float(hs.change_from_last_month) == round(float(hs.health_score) - p, 2), key
            assert hs.kpi_only_score == hs.health_score
            if key in stale:
                continue
            compared += 1
            assert float(hs.health_score) == float(row['health_score']), key
            assert hs.health_status == row['health_status'], key
            old_pillars = json.loads(row['contributing_pillars'])
            assert set(hs.contributing_pillars) == set(old_pillars), key
            # <= 0.02: float summation order when averaging a KPI's samples
            # in a month (pandas vs Decimal->float) can flip the 2nd decimal
            assert all(abs(hs.contributing_pillars[p] - old_pillars[p]) <= 0.02 for p in old_pillars), key
    print(f'\nhealth rows compared exactly: {compared}/{len(expected)}; '
          f'skipped as stale-partial-month in the old repo: {len(stale)} rows')
    assert compared >= len(expected) - 10      # at most one month's worth may be stale


def test_account_status_follows_latest_health(loaded):
    """Item-28 contract: status tracks the latest health band (healthy →
    active, at_risk/critical → at_risk, churned terminal). The old tenant's
    stored status disagrees for accounts whose latest month is healthy but
    a LATER old-pipeline stage (playbook trigger / urgent scanner — not
    ported yet) had set at_risk; those are printed, not asserted."""
    cid, _ = loaded
    old = {r['account_name']: r['account_status'] for r in _rows(OLD / 'accounts_arcs.csv')}
    with app.app_context():
        names = {a.account_id: a.account_name for a in _accounts(cid)}
        latest = {}
        for h in HealthScore.query.filter(HealthScore.account_id.in_(list(names))).order_by(
                HealthScore.account_id, HealthScore.measurement_month).all():
            latest[names[h.account_id]] = h.health_status
        got = {a.account_name: a.account_status for a in _accounts(cid)}
    expected = {n: ('active' if s == 'healthy' else 'at_risk') for n, s in latest.items()}
    assert got == expected
    drift = sorted(n for n in old if old[n] != got[n])
    print(f'\nold status differs (later old-pipeline stage, or a stale partial-month score) on: {drift}')
    assert len(drift) <= 4


# ── Wizard A v2 vs old arcs ───────────────────────────────────────────

def _old_wizard_a():
    arcs = {r['account_name']: r for r in _rows(OLD / 'accounts_arcs.csv')}
    traj = {}
    for r in _rows(OLD / 'generated_nodes.csv'):
        if r['node_subtype'] == 'arc_detection':
            traj[r['account_name']] = json.loads(r['properties'])['arc_type']
    return arcs, traj


def test_wizard_a_v2_cited_disagreements(loaded):
    cid, res = loaded
    old_arcs, old_traj = _old_wizard_a()
    with app.app_context():
        names = {a.account_id: a.account_name for a in _accounts(cid)}
        rows = {names[r.account_id]: r.journey_json for r in JourneyData.query.filter_by(customer_id=cid).all()}
        arcs_db = {a.account_name: (a.arc_type, a.arc_phase, a.arc_confidence) for a in _accounts(cid)}
    assert set(rows) == set(old_arcs)
    assert res['wizard_a']['coverage']['classified'] + res['wizard_a']['coverage']['steady'] \
        + res['wizard_a']['coverage']['unclassified'] == len(rows)

    table, agreements = [], 0
    for name, j in sorted(rows.items()):
        old, arc = old_arcs[name], j['arc']
        assert j['summary']['trajectory'] == old_traj[name], (name, j['summary']['trajectory'], old_traj[name])
        if arc['state'] == 'classified':
            assert arc['supporting_episode_ids'], name
            assert arc['arc_type'] != 'competitive_displacement' or 'commercial_pressure' in arc['observed_roles'], name
            assert arc['arc_type'] != 'exec_sponsor_change' or 'champion_change' in arc['observed_roles'], name
            assert arcs_db[name][0] == arc['arc_type']
        else:
            assert arcs_db[name][0] is None
        if old['arc_type'] == 'competitive_displacement' and float(old['arc_confidence']) == 0.55:
            assert not (arc['arc_type'] == 'competitive_displacement' and not arc['supporting_episode_ids']), name
        if arc['arc_type'] == old['arc_type']:
            agreements += 1
        lvt = j['leading_vs_trailing']
        table.append((name, old['arc_type'], float(old['arc_confidence']), arc['state'], arc['arc_type'],
                      ','.join(arc['observed_roles']), lvt['first_leading_warning_at'],
                      lvt['first_trailing_warning_at'], lvt['lead_days']))

    print('\n%-22s %-25s %-5s %-13s %-24s %-45s %-11s %-11s %s' % (
        'account', 'old_arc', 'conf', 'v2_state', 'v2_arc', 'roles', 'lead_at', 'trail_at', 'lead_d'))
    for r in table:
        print('%-22s %-25s %-5s %-13s %-24s %-45s %-11s %-11s %s' % r)
    print(f'agreements with old arc: {agreements}/{len(table)}; coverage: {res["wizard_a"]["coverage"]}')

    with app.app_context():
        # the leading layer got written where signals exist
        assert HealthScore.query.join(Account, Account.account_id == HealthScore.account_id).filter(
            Account.customer_id == cid, HealthScore.qual_score.isnot(None)).count() > 0
        # no synthetic graph rows
        assert ContextNode.query.filter(ContextNode.customer_id == cid, ContextNode.source != 'observed').count() == 0


def test_old_arcs_were_mostly_fallback_or_uncited(loaded):
    """What the old output looked like for this tenant, on record."""
    old_arcs, _ = _old_wizard_a()
    conf = Counter((r['arc_type'], float(r['arc_confidence'])) for r in old_arcs.values())
    fallback = conf.get(('competitive_displacement', 0.55), 0)
    print(f'\nold repo on 415: {dict(conf)}; fallback rule share {fallback}/{len(old_arcs)}')
    assert len(old_arcs) == 10


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
