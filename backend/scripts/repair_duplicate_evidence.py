"""
One-time repair for evidence nodes materialised twice (the drain race fixed in
signal_engine.pipeline.process_pending on 2026-09-05): among SIGNAL nodes with the
same (account, source_event_id, subtype, occurred_at, title) keep the lowest id,
re-point signal rows and edges to it, delete the twins, rebuild the journeys.

    python scripts/repair_duplicate_evidence.py [--dry-run]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    from mcp_server.common import get_flask_app
    from extensions import db
    from models import ContextNode, ContextEdge, QualitativeSignal
    from journeys.wizard_a import run_wizard_a
    with get_flask_app().app_context():
        groups = {}
        for n in ContextNode.query.filter_by(node_type='SIGNAL').order_by(ContextNode.node_id).all():
            key = (n.account_id, n.source_event_id, n.node_subtype, n.occurred_at, n.title)
            groups.setdefault(key, []).append(n)
        dupes = {k: v for k, v in groups.items() if len(v) > 1}
        touched = set()
        removed = 0
        for key, nodes in dupes.items():
            keep, twins = nodes[0], nodes[1:]
            for t in twins:
                if not args.dry_run:
                    QualitativeSignal.query.filter_by(cg_node_id=t.node_id).update({'cg_node_id': keep.node_id})
                    ContextEdge.query.filter_by(from_node_id=t.node_id).update({'from_node_id': keep.node_id})
                    ContextEdge.query.filter_by(to_node_id=t.node_id).update({'to_node_id': keep.node_id})
                    db.session.delete(t)
                removed += 1
            touched.add((keep.customer_id, keep.account_id))
        if not args.dry_run:
            db.session.commit()
        print(f"duplicate groups: {len(dupes)} · twin nodes {'would be ' if args.dry_run else ''}removed: {removed} · accounts touched: {len(touched)}")
        if not args.dry_run and touched:
            by_c = {}
            for cid, aid in touched:
                by_c.setdefault(cid, set()).add(aid)
            for cid, aids in by_c.items():
                r = run_wizard_a(cid, aids)
                print(f"  customer {cid}: {r['processed']} journeys rebuilt")


if __name__ == '__main__':
    main()
