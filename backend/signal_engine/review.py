"""
Human verification of evidence (Google G4: "practitioner confirmation").

    review_signal(customer_id, signal_id, decision, subtype=None, node_id=None, note=None, reviewer=None)

  accept      the evidence stands; requires_review cleared; full weight on the journey
  reject      the evidence is wrong / not evidence; the node STAYS (audit) but is
              excluded from the journey (episodes, series, arcs)
  reclassify  the model picked the wrong subtype; node re-typed to a taxonomy
              subtype, role / polarity / urgency re-derived, original kept

Every decision writes a SignalReview row (history) and sets
properties['review'] on the node (current state), then rebuilds the
account's journey so the change is visible immediately.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

DECISIONS = ('accept', 'reject', 'reclassify')
URGENCY_ORDER = ('low', 'medium', 'high', 'critical')


def _nodes_for(sig) -> List:
    from models import ContextNode
    return (ContextNode.query.filter_by(source_event_id=sig.signal_id, node_type='SIGNAL')
            .order_by(ContextNode.node_id).all())


def _apply_reclassify(node, subtype: str, taxonomy) -> dict:
    """Re-type one node; returns what changed."""
    from signal_engine.pipeline import reconcile_sentiment
    from signal_engine.urgency import classify_structural_urgency, resolve_effective_urgency
    from signal_engine import settings
    role = taxonomy.signal_role(subtype)
    props = dict(node.properties or {})
    before = {'subtype': node.node_subtype, 'role': props.get('role')}
    pol = taxonomy.role_polarity(role)
    raw = props.get('raw_sentiment_score')
    if raw is None:
        try:
            raw = float(props.get('sentiment_score'))
        except (TypeError, ValueError):
            raw = None
    score, conflict, raw = reconcile_sentiment(pol, raw)
    band = settings.get('storage', 'sentiment_label_band')
    structural = classify_structural_urgency(role)
    props.update({
        'role': role, 'sentiment_score': str(round(score, 2)), 'raw_sentiment_score': raw, 'polarity_conflict': conflict,
        'sentiment': 'positive' if score > band else 'negative' if score < -band else 'neutral',
        'structural_urgency': structural,
        'effective_urgency': resolve_effective_urgency(structural, props.get('urgency_score'), props.get('escalation_probability')),
        'classification_basis': 'human_reclassified', 'original_subtype': before['subtype'],
    })
    node.node_subtype = subtype
    node.properties = props
    return before


def review_signal(customer_id: int, signal_id: str, decision: str, *, subtype: Optional[str] = None,
                  node_id: Optional[int] = None, note: Optional[str] = None, reviewer: Optional[str] = None,
                  rebuild: bool = True) -> dict:
    from extensions import db
    from models import QualitativeSignal, SignalReview
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_vertical_for_customer

    decision = (decision or '').strip().lower()
    if decision not in DECISIONS:
        raise ValueError(f'decision must be one of {DECISIONS}')
    sig = QualitativeSignal.query.filter_by(signal_id=signal_id).first()
    if not sig or int(sig.customer_id) != int(customer_id):
        raise ValueError(f'signal {signal_id} not found for customer {customer_id}')
    nodes = _nodes_for(sig)
    if not nodes:
        raise ValueError(f'signal {signal_id} has no evidence node yet (still queued?)')
    if node_id is not None:
        nodes = [n for n in nodes if n.node_id == int(node_id)]
        if not nodes:
            raise ValueError(f'node {node_id} does not belong to signal {signal_id}')
    taxonomy = get_taxonomy(get_vertical_for_customer(customer_id))
    if decision == 'reclassify':
        subtype = (subtype or '').strip().lower()
        if not taxonomy.signal_role(subtype):
            raise ValueError(f'{subtype!r} is not a subtype in this tenant\'s vocabulary')
        if len(nodes) > 1:
            raise ValueError('signal has several evidence nodes — pass node_id to reclassify one of them')

    was_flagged = bool(sig.requires_review)
    stamp = datetime.utcnow().isoformat()
    audits, changed = [], []
    for n in nodes:
        props = dict(n.properties or {})
        before = {'subtype': n.node_subtype, 'role': props.get('role')}
        if decision == 'reclassify':
            before = _apply_reclassify(n, subtype, taxonomy)
            props = dict(n.properties)
        props['review'] = {'status': {'accept': 'accepted', 'reject': 'rejected', 'reclassify': 'reclassified'}[decision],
                           'at': stamp, 'by': reviewer, 'note': note,
                           **({'from_subtype': before['subtype'], 'to_subtype': subtype} if decision == 'reclassify' else {})}
        props['requires_review'] = False
        n.properties = props
        a = SignalReview(customer_id=sig.customer_id, account_id=sig.account_id, signal_id=sig.signal_id, node_id=n.node_id,
                         decision=decision, from_subtype=before['subtype'],
                         to_subtype=(subtype if decision == 'reclassify' else None), was_flagged=was_flagged,
                         note=note, reviewer=reviewer)
        db.session.add(a)
        audits.append(a)
        changed.append({'node_id': n.node_id, 'subtype': n.node_subtype, 'role': n.properties.get('role'),
                        'effective_urgency': n.properties.get('effective_urgency'), 'review': n.properties['review']['status']})

    # row-level state: cleared once every node of the signal has a decision
    all_nodes = _nodes_for(sig)
    if all(((x.properties or {}).get('review') or {}).get('status') for x in all_nodes):
        sig.requires_review = False
    live = [x for x in all_nodes if ((x.properties or {}).get('review') or {}).get('status') != 'rejected']
    sig.effective_urgency = max((x.properties.get('effective_urgency') or 'low' for x in live),
                                key=URGENCY_ORDER.index, default=None)
    db.session.commit()

    rebuilt = 0
    if rebuild:
        from journeys.wizard_a import run_wizard_a
        try:
            rebuilt = run_wizard_a(sig.customer_id, [sig.account_id]).get('processed', 0)
        except Exception as e:  # pragma: no cover
            logger.warning('journey rebuild after review failed: %s', e)
            db.session.rollback()
    logger.info('review: signal=%s decision=%s nodes=%s by=%s', signal_id, decision, [c['node_id'] for c in changed], reviewer)
    return {'signal_id': signal_id, 'account_id': sig.account_id, 'decision': decision, 'nodes': changed,
            'audit_ids': [a.id for a in audits], 'requires_review': bool(sig.requires_review), 'journeys_rebuilt': rebuilt}


def review_history(customer_id: int, account_id: Optional[int] = None, signal_id: Optional[str] = None, limit: int = 100) -> List[dict]:
    from models import SignalReview
    q = SignalReview.query.filter_by(customer_id=int(customer_id))
    if account_id:
        q = q.filter_by(account_id=int(account_id))
    if signal_id:
        q = q.filter_by(signal_id=signal_id)
    return [{'id': r.id, 'signal_id': r.signal_id, 'node_id': r.node_id, 'account_id': r.account_id, 'decision': r.decision,
             'from_subtype': r.from_subtype, 'to_subtype': r.to_subtype, 'was_flagged': r.was_flagged, 'note': r.note,
             'reviewer': r.reviewer, 'at': r.created_at.isoformat() if r.created_at else None}
            for r in q.order_by(SignalReview.id.desc()).limit(limit).all()]
