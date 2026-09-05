"""
Outcome logging (P16 item 7) — the decision record the whole measurement
chain hangs on: renewal, contraction, churn, expansion, refresh. Without
recorded outcomes there is no realized NRR, no lead-time backtest on real
data, no "measured impact", and the narrative writes "renewal passed, no
outcome recorded".

    log_outcome(customer_id, account_id, outcome_type, occurred_at, revenue=None, note=None,
                linked_signal_ids=None, decided_by=None, source_type='manual', source_ref=None)

- outcome_type must be in the tenant taxonomy's revenue buckets (lost /
  expansion / protected / pipeline / at_risk vocabulary) — nothing invented.
- occurred_at is the DECISION date, not the logging date.
- idempotent on (account, type, decision date, revenue): 'exists', not a copy.
- writes an observed OUTCOME node (tier 1, full confidence when a note or a
  decider is given; the unearned-confidence clamp applies otherwise), LED_TO
  edges from the signals it names, then rebuilds the account's journey.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

LOGGED_BY = 'log_outcome'


def _parse_when(value) -> datetime:
    if isinstance(value, datetime):
        return value
    v = str(value or '').strip().replace('Z', '+00:00')
    if not v:
        raise ValueError('occurred_at (the decision date) is required')
    d = datetime.fromisoformat(v)
    return d.replace(tzinfo=None) if d.tzinfo else d


def _find_signal_nodes(customer_id: int, account_id: int, refs: List[str]) -> dict:
    """ref → SIGNAL node. Accepts our signal ids, source refs, or node ids."""
    from models import ContextNode
    out = {}
    nodes = ContextNode.query.filter_by(customer_id=customer_id, account_id=account_id, node_type='SIGNAL').all()
    for ref in refs:
        r = str(ref).strip()
        for n in nodes:
            p = n.properties or {}
            if r in (n.source_event_id, n.source_ref, p.get('signal_id'), str(n.node_id)):
                out[r] = n
                break
    return out


def log_outcome(customer_id: int, account_id: int, outcome_type: str, occurred_at, *, revenue=None,
                note: Optional[str] = None, linked_signal_ids: Optional[List[str]] = None,
                decided_by: Optional[str] = None, source_type: str = 'manual', source_ref: Optional[str] = None,
                rebuild: bool = True) -> dict:
    from extensions import db
    from models import Account, ContextNode, ContextEdge
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_vertical_for_customer
    from utils.context_graph_invariants import clamp_unearned_confidence

    acct = db.session.get(Account, int(account_id))
    if not acct or int(acct.customer_id) != int(customer_id):
        raise ValueError(f'account {account_id} does not belong to customer {customer_id}')
    outcome_type = (outcome_type or '').strip().lower()
    taxonomy = get_taxonomy(get_vertical_for_customer(customer_id))
    bucket = taxonomy.revenue_bucket(outcome_type)
    if not bucket:
        allowed = {b: sorted(s) for b, s in taxonomy.revenue_bucket_map.items()}
        raise ValueError(f'{outcome_type!r} is not an outcome type in this tenant\'s vocabulary; allowed: {allowed}')
    when = _parse_when(occurred_at)
    try:
        rev = float(revenue) if revenue not in (None, '') else None
    except (TypeError, ValueError):
        raise ValueError('revenue must be a number')
    if rev is not None and bucket in ('lost', 'at_risk') and rev > 0:
        rev = -rev                                # a loss is recorded as negative movement, whichever sign the caller used

    # idempotent on the decision itself
    for n in ContextNode.query.filter_by(customer_id=customer_id, account_id=acct.account_id, node_type='OUTCOME',
                                         node_subtype=outcome_type).all():
        same_day = n.occurred_at and n.occurred_at.date() == when.date()
        same_rev = (n.revenue_impact is None and rev is None) or (n.revenue_impact is not None and rev is not None and abs(float(n.revenue_impact) - rev) < 0.005)
        if same_day and same_rev:
            return {'status': 'exists', 'node_id': n.node_id, 'account_id': acct.account_id, 'outcome_type': outcome_type,
                    'bucket': bucket, 'occurred_at': n.occurred_at.isoformat()}

    label = outcome_type.replace('_', ' ')
    props = {
        'evidence': (note or '').strip() or (f'logged by {decided_by}' if decided_by else ''),
        'decided_by': decided_by, 'logged_via': LOGGED_BY, 'bucket': bucket, 'evidence_tier': 'observed',
        'linked_signal_ids': [str(x) for x in (linked_signal_ids or [])], 'logged_at': datetime.utcnow().isoformat(),
    }
    conf, props, tier, clamped = clamp_unearned_confidence('OUTCOME', source_type, source_ref, 1.0, props, 1)
    node = ContextNode(
        customer_id=customer_id, account_id=acct.account_id, node_type='OUTCOME', node_subtype=outcome_type,
        source='observed', title=f'{label[:1].upper()}{label[1:]} — {acct.account_name}',
        revenue_impact=rev, revenue_impact_type=outcome_type, properties=props, tier=tier, confidence=conf,
        occurred_at=when, source_platform=source_type, source_event_id=f'outcome:{uuid.uuid4().hex[:12]}',
        source_ref=source_ref,
    )
    db.session.add(node)
    db.session.flush()

    linked, unresolved = [], []
    found = _find_signal_nodes(customer_id, acct.account_id, props['linked_signal_ids'])
    for ref in props['linked_signal_ids']:
        sn = found.get(ref)
        if not sn:
            unresolved.append(ref)
            continue
        db.session.add(ContextEdge(
            customer_id=customer_id, from_node_id=sn.node_id, to_node_id=node.node_id, edge_type='LED_TO',
            weight=1.0, confidence=1.0, source_platform=source_type, created_by=LOGGED_BY,
            properties={'evidence': f'linked by {decided_by or "the logger"} when the outcome was recorded',
                        'evidence_tier': 'observed', 'derivation': 'human_linked'},
        ))
        linked.append(sn.node_id)
    db.session.commit()

    rebuilt = 0
    if rebuild:
        from journeys.wizard_a import run_wizard_a
        try:
            rebuilt = run_wizard_a(customer_id, [acct.account_id]).get('processed', 0)
        except Exception as e:  # pragma: no cover
            logger.warning('journey rebuild after outcome failed: %s', e)
            db.session.rollback()
    logger.info('outcome logged: customer=%s account=%s %s %s rev=%s by=%s', customer_id, acct.account_id, outcome_type,
                when.date(), rev, decided_by)
    return {'status': 'logged', 'node_id': node.node_id, 'account_id': acct.account_id, 'outcome_type': outcome_type,
            'bucket': bucket, 'occurred_at': when.isoformat(), 'revenue': rev, 'confidence': conf, 'evidence_clamped': clamped,
            'linked_signal_node_ids': linked, 'unresolved_signal_refs': unresolved, 'journeys_rebuilt': rebuilt}


def outcome_vocabulary(customer_id: int) -> dict:
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_vertical_for_customer
    t = get_taxonomy(get_vertical_for_customer(customer_id))
    return {b: sorted(s) for b, s in t.revenue_bucket_map.items()}
