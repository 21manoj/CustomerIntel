"""
Regression test for the signal-drain race in signal_engine.pipeline.process_pending:
a SELECT ... FOR UPDATE SKIP LOCKED batch fetch, followed by one db.session.commit()
per signal instead of one for the whole batch, released the lock on the rest of the
batch as soon as the first signal committed -- letting a second, concurrently running
process_pending() call (the background worker racing an on-demand call) legally grab
and re-materialize the same not-yet-processed rows. Found on a bulk 109-signal import
2026-09-12: 89% of SIGNAL nodes for that tenant were exact duplicates.

Reproduced here with two real threads (their own Flask app context each, so each gets
its own DB connection/session -- the lock semantics this bug depends on only matter
across separate connections) racing over the same batch of pending signals, using
structured signals (a declared signal_type) so nothing depends on LLM enrichment.
"""
import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions import db

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app
app = get_flask_app()

from models import Account, ContextNode, QualitativeSignal


@pytest.fixture(scope='module')
def tenant():
    with app.app_context():
        db.create_all()
        from mcp_server.cs_pulse_onboarding import create_customer
        tag = uuid.uuid4().hex[:8]
        cid = create_customer(data_origin='synthetic_test', name=f'DrainRace {tag}', domain=f'drainrace-{tag}.test',
                              vertical='saas_premium', admin_email=f'dr_{tag}@t.test', admin_name='D')['customer_id']
        a = Account(customer_id=cid, account_name='Concurrent Drain Co', revenue=1_000_000, vertical='saas_premium',
                    external_account_id='concurrentdrain.test')
        db.session.add(a)
        db.session.commit()
        yield cid, a.account_id
        db.session.remove()
        db.drop_all()


def test_two_concurrent_drains_do_not_double_materialize_a_batch(tenant):
    cid, aid = tenant
    import signal_engine.pipeline as pipeline_mod
    import utils.vertical_registry as vertical_registry_mod

    # 5 structured signals -- a declared, known signal_type skips LLM enrichment entirely,
    # so this test is fast and deterministic regardless of ANTHROPIC_API_KEY.
    for i in range(5):
        pipeline_mod.ingest(cid, aid, 'ticket', f'Race test signal {i}',
                            signal_type='champion_departure', occurred_at=f'2026-04-0{i + 1}T00:00:00Z',
                            participants=[{'name': 'Test Person', 'role': 'VP'}])

    # process_pending() does `from utils.vertical_registry import get_vertical_for_customer`
    # INSIDE the function body (not a module-level import in pipeline.py), so the patch has
    # to live on utils.vertical_registry itself -- patching pipeline_mod's own attribute
    # would be invisible to that local import.
    real_get_vertical = vertical_registry_mod.get_vertical_for_customer
    call_count = {'n': 0}
    paused = threading.Event()
    release = threading.Event()

    def hooked_get_vertical(customer_id):
        call_count['n'] += 1
        if call_count['n'] == 2:           # about to start signal #2 -- signal #1 already committed
            paused.set()
            assert release.wait(timeout=5), 'second drain never ran -- test is broken, not the fix'
        return real_get_vertical(customer_id)

    vertical_registry_mod.get_vertical_for_customer = hooked_get_vertical
    second = {}
    try:
        def run_second_drain():
            assert paused.wait(timeout=5), 'first drain never reached signal #2'
            with app.app_context():
                second['res'] = pipeline_mod.process_pending(customer_id=cid, limit=50)
            release.set()

        t = threading.Thread(target=run_second_drain)
        t.start()
        with app.app_context():
            first = pipeline_mod.process_pending(customer_id=cid, limit=50)
        t.join(timeout=10)
    finally:
        vertical_registry_mod.get_vertical_for_customer = real_get_vertical

    assert not t.is_alive(), 'second drain thread hung'
    assert second['res']['processed'] == 0, second['res']          # nothing left for it to steal
    assert first['processed'] == 5, first

    with app.app_context():
        sigs = QualitativeSignal.query.filter(
            QualitativeSignal.customer_id == cid, QualitativeSignal.account_id == aid,
            QualitativeSignal.content.like('Race test signal%')).all()
        assert len(sigs) == 5
        node_ids = [s.cg_node_id for s in sigs]
        assert all(n is not None and n > 0 for n in node_ids), node_ids   # nothing stuck at the claim sentinel
        assert len(set(node_ids)) == 5                                    # no two signals collapsed onto one node

        nodes = ContextNode.query.filter(ContextNode.customer_id == cid, ContextNode.account_id == aid,
                                         ContextNode.node_type == 'SIGNAL').all()
        by_signal_id = {}
        for n in nodes:
            sid = (n.properties or {}).get('signal_id')
            by_signal_id.setdefault(sid, []).append(n.node_id)
        our_signal_ids = {s.signal_id for s in sigs}
        dupes = {sid: ids for sid, ids in by_signal_id.items() if sid in our_signal_ids and len(ids) > 1}
        assert not dupes, f'duplicate SIGNAL nodes for the same signal_id: {dupes}'


def test_enrichment_error_releases_the_claim_for_retry(tenant):
    """A signal that fails enrichment must go back to cg_node_id=None (retryable), not
    get stuck at the claim sentinel forever."""
    cid, aid = tenant
    import signal_engine.pipeline as pipeline_mod

    r = pipeline_mod.ingest(cid, aid, 'email', 'Retry-on-error probe', occurred_at='2026-04-10T00:00:00Z')
    assert r['status'] == 'queued'

    # Same local-import caveat as above: process_pending() does
    # `from signal_engine.enrichment import enrich_signal` inside the function body, so the
    # patch has to live on signal_engine.enrichment itself.
    import signal_engine.enrichment as enrichment_mod
    real_enrich = enrichment_mod.enrich_signal
    enrichment_mod.enrich_signal = lambda **kwargs: {'error': 'forced failure for the test'}
    try:
        with app.app_context():
            res = pipeline_mod.process_pending(customer_id=cid, limit=50)
    finally:
        enrichment_mod.enrich_signal = real_enrich

    assert res['errors'] >= 1
    with app.app_context():
        sig = QualitativeSignal.query.filter_by(customer_id=cid, account_id=aid, signal_id=r['signal_id']).first()
        assert sig.cg_node_id is None, 'signal must stay retryable, not stuck at the claim sentinel'


def test_claims_left_by_a_killed_drain_are_released_at_worker_start(tenant):
    """process_pending claims its batch (cg_node_id = CLAIMED_SENTINEL) in one commit and then
    works through it. A process killed mid-batch leaves those rows claimed, and the pending
    query (cg_node_id IS NULL) never sees them again: the signals silently never become
    evidence. The worker releases them once at startup."""
    cid, aid = tenant
    import signal_engine.pipeline as pipeline_mod
    from signal_engine.worker import SignalEnrichmentWorker

    r = pipeline_mod.ingest(cid, aid, 'ticket', 'Stranded by a killed worker', signal_type='champion_departure',
                            occurred_at='2026-04-20T00:00:00Z', participants=[{'name': 'Test Person', 'role': 'VP'}])
    with app.app_context():
        sig = QualitativeSignal.query.filter_by(customer_id=cid, signal_id=r['signal_id']).first()
        done_node = (QualitativeSignal.query.filter(QualitativeSignal.customer_id == cid, QualitativeSignal.cg_node_id > 0)
                     .first())
        done_id, done_node_id = (done_node.id, done_node.cg_node_id) if done_node else (None, None)
        sig.cg_node_id = pipeline_mod.CLAIMED_SENTINEL          # what a kill -9 mid-batch leaves behind
        db.session.commit()
        # the bug: it is invisible to every later pass
        res = pipeline_mod.process_pending(customer_id=cid, limit=50)
        assert r['signal_id'] not in [x['signal_id'] for x in res['signals']]
        assert QualitativeSignal.query.filter_by(signal_id=r['signal_id']).first().cg_node_id == pipeline_mod.CLAIMED_SENTINEL

    assert SignalEnrichmentWorker().release_stale_claims() == 1

    with app.app_context():
        assert QualitativeSignal.query.filter_by(signal_id=r['signal_id']).first().cg_node_id is None
        if done_id is not None:                                  # already-materialised signals are untouched
            assert db.session.get(QualitativeSignal, done_id).cg_node_id == done_node_id
        res = pipeline_mod.process_pending(customer_id=cid, limit=50)
        assert [x['signal_id'] for x in res['signals']] == [r['signal_id']], res
        assert QualitativeSignal.query.filter_by(signal_id=r['signal_id']).first().cg_node_id > 0
    assert SignalEnrichmentWorker().release_stale_claims() == 0  # idempotent
