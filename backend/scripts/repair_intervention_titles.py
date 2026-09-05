"""
One-time repair for INTERVENTION node titles written before the playbook labels changed form
(2026-09-05: 'Escalation — executive response' became 'Escalation: executive response', because
journeys.narrative._strip_suffix cuts a title at the first ' — ' and the narrative then read
"playbook 'escalation'"). For every INTERVENTION node the title is recomputed from the CURRENT
label of properties['playbook_id'] for the tenant's vertical (a playbook that no longer exists
falls back to its id humanised) through playbooks.governance.intervention_title — the helper
approve writes with, so the two cannot drift — and the affected journeys are rebuilt. A second
run finds nothing to change.

    python scripts/repair_intervention_titles.py [--customer-id N]           dry run: node id / old / new
    python scripts/repair_intervention_titles.py [--customer-id N] --apply   write + rebuild the journeys
"""
import argparse
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def plan(customer_id: Optional[int] = None) -> dict:
    """{'nodes': n seen, 'changes': [(node, old, new)], 'skipped': [(node, reason)]} — reads only."""
    from extensions import db
    from models import Account, ContextNode
    from playbooks.governance import NODE_TYPE, intervention_title, playbook_def
    q = ContextNode.query.filter_by(node_type=NODE_TYPE)
    if customer_id is not None:
        q = q.filter_by(customer_id=int(customer_id))
    out = {'nodes': 0, 'changes': [], 'skipped': []}
    for n in q.order_by(ContextNode.node_id).all():
        out['nodes'] += 1
        props = n.properties or {}
        playbook_id = props.get('playbook_id') or n.node_subtype
        acct = db.session.get(Account, n.account_id) if n.account_id else None
        if not playbook_id or acct is None:
            out['skipped'].append((n, 'no playbook_id on the node' if not playbook_id else f'account {n.account_id} not found'))
            continue
        try:
            label = playbook_def(n.customer_id, playbook_id, props.get('action_class'))['label']
        except ValueError as e:          # the tenant has no vertical (utils.vertical_registry fails closed)
            out['skipped'].append((n, str(e)))
            continue
        new = intervention_title(label, acct.account_name)
        if new != (n.title or ''):
            out['changes'].append((n, n.title, new))
    return out


def apply(changes: List[tuple]) -> dict:
    """Write the new titles, then rebuild the touched journeys without re-evaluating playbooks. Returns per-customer rebuild counts."""
    from extensions import db
    from journeys.wizard_a import run_wizard_a
    by_customer: dict = {}
    for node, _old, new in changes:
        node.title = new
        by_customer.setdefault(node.customer_id, set()).add(node.account_id)
    db.session.commit()
    return {cid: run_wizard_a(cid, sorted(aids), evaluate_playbooks=False).get('processed', 0) for cid, aids in by_customer.items()}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--customer-id', type=int, default=None, help='only this tenant (default: every tenant)')
    ap.add_argument('--apply', action='store_true', help='write the titles and rebuild the journeys (default: dry run)')
    args = ap.parse_args(argv)
    from mcp_server.common import get_flask_app
    with get_flask_app().app_context():
        p = plan(args.customer_id)
        for node, old, new in p['changes']:
            print(f"node {node.node_id} customer {node.customer_id} account {node.account_id}: {old!r} → {new!r}")
        for node, reason in p['skipped']:
            print(f"node {node.node_id} customer {node.customer_id} skipped: {reason}")
        touched = {(n.customer_id, n.account_id) for n, _, _ in p['changes']}
        print(f"intervention nodes: {p['nodes']} · titles {'would be ' if not args.apply else ''}changed: {len(p['changes'])} "
              f"· skipped: {len(p['skipped'])} · accounts touched: {len(touched)}")
        if args.apply and p['changes']:
            for cid, n in apply(p['changes']).items():
                print(f"  customer {cid}: {n} journeys rebuilt")
    return 0


if __name__ == '__main__':
    sys.exit(main())
