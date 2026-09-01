"""
Context Graph Invariants — data-quality validators that catch systemic bugs.

Runs as a non-blocking audit after Wizard A and after context graph ingest.
Each invariant is a pure function that takes customer_id and returns a list
of Violations. Violations are logged as WARN; they never raise or roll back.

Built in response to an external investigation that surfaced 7 platform-level
data bugs (April 2026, customer 385 / Relay Healthcare / Nimbus Logistics):
- OUTCOME→OUTCOME edges (backwards causality)
- Positive signals linked as causes of churn outcomes
- Orphan OUTCOME nodes with revenue impact
- confidence > 1.0
- account_status not reconciled with lifecycle events
- Duplicate churn outcomes
- Churn-averted coexisting with churn-lost on same account
- Revenue bucket mislabels

Any new invariant: add to INVARIANTS_REGISTRY at the bottom.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════
# Violation schema
# ═════════════════════════════════════════════════════════════════════


@dataclass
class Violation:
    """A single data-quality violation found by an invariant check."""

    invariant_id: str  # e.g. 'I1'
    invariant_name: str
    severity: Literal['error', 'warning']
    customer_id: int
    message: str
    account_id: Optional[int] = None
    node_ids: List[int] = field(default_factory=list)
    edge_ids: List[int] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ═════════════════════════════════════════════════════════════════════
# Canonical polarity + arc taxonomy (single source of truth)
# ═════════════════════════════════════════════════════════════════════


NEGATIVE_OUTCOME_SUBTYPES = {
    'churn_lost', 'contraction', 'renewal_at_risk', 'revenue_at_risk',
    'escalation', 'intervention_outcome',
}

POSITIVE_OUTCOME_SUBTYPES = {
    'expansion_closed', 'new_logo', 'revenue_protected',
    'playbook_outcome', 'recovery_milestone', 'churn_averted',
}

NEGATIVE_SIGNAL_SUBTYPES = {
    'champion_loss', 'engagement_drop', 'usage_decline', 'kpi_decline',
    'escalation', 'support_escalation', 'executive_escalation',
    'critical_incident', 'competitive_eval', 'silent_churn',
    'budget_cut', 'contract_dispute', 'downgrade_request',
    'arc_detection', 'crisis_event', 'integration_stall', 'churn_signal',
}

POSITIVE_SIGNAL_SUBTYPES = {
    'kpi_recovery', 'usage_spike', 'champion_reengagement',
    'executive_engagement', 'expansion_signal', 'csm_intervention',
    'feature_adoption_push', 'deployment_improvement',
    'recovery_milestone', 'qbr_positive', 'qbr_alignment',
    'adoption_growth', 'seasonal_peak', 'capacity_add',
    'health_improvement',
}

CAUSAL_EDGE_TYPES = {'CAUSED_BY', 'LED_TO', 'TRIGGERED', 'INDICATES', 'RESULTED_IN'}

# Canonical arc taxonomy — any arc_type value not in this set is invalid.
# Kept narrow intentionally: extend via explicit review, not by accretion.
CANONICAL_ARC_TYPES = {
    'crisis_recovery', 'expansion_champion', 'champion_loss',
    'silent_churn', 'infrastructure_decay', 'stalled_deployment',
    'competitive_displacement', 'land_and_expand', 'seasonal_surge',
    'exec_sponsor_change', 'stable_healthy', 'new_onboarding',
    # Legacy / trajectory-classifier vocabulary — allowed during migration.
    # Remove these entries once wizard_a_journey_db.py emits canonical values.
    'crisis', 'improving', 'recovery', 'declining', 'stable',
}

# Revenue buckets + polarity-ambiguous sets are loaded from config/taxonomy_base.json
# (+ optional per-vertical overlays) via utils.taxonomy_loader. The JSON is the
# source of truth; edits there do not require a Python change.
# See docs/governance/AI_GOVERNANCE_FRAMEWORK.md and policy_taxonomy_runtime_auto_fix.md.
#
# These module-level constants preserve the prior public API surface — downstream
# call sites (including tests) continue to reference them directly.
try:
    from utils.taxonomy_loader import get_taxonomy as _get_taxonomy
    _TAX = _get_taxonomy()
    REVENUE_BUCKET_MAP = {bk: set(subs) for bk, subs in _TAX.revenue_bucket_map.items()}
    POLARITY_AMBIGUOUS_SUBTYPES = set(_TAX.polarity_ambiguous_outcome_subtypes)
    POLARITY_AMBIGUOUS_SIGNAL_SUBTYPES = set(_TAX.polarity_ambiguous_signal_subtypes)
except Exception as _tax_err:
    # Fail-safe fallback for boot paths where config/ is not yet available.
    # validate_all_at_boot() in the loader will have surfaced any real error;
    # this fallback exists only to keep import-time failures from cascading.
    import logging as _boot_logging
    _boot_logging.getLogger(__name__).error(
        '[taxonomy] falling back to hardcoded taxonomy — loader raised: %s', _tax_err,
    )
    REVENUE_BUCKET_MAP = {
        'at_risk': {
            'renewal_at_risk', 'revenue_at_risk', 'renewal_uncertainty',
            'churn_risk', 'engagement_decline', 'partial_recovery',
            'capacity_constraint', 'partner_friction',
        },
        'lost': {'churn_lost', 'contraction'},
        'expansion': {
            'expansion_closed', 'new_logo', 'revenue_expanded', 'revenue_growth',
        },
        'pipeline': {
            'expansion_approved', 'expansion_opportunity',
        },
        'protected': {
            'revenue_protected', 'churn_averted',
            'renewal_secured', 'escalation_resolved',
        },
    }
    POLARITY_AMBIGUOUS_SUBTYPES = {'playbook_outcome', 'intervention_outcome'}
    POLARITY_AMBIGUOUS_SIGNAL_SUBTYPES = {'arc_detection'}


# ═════════════════════════════════════════════════════════════════════
# Individual invariant functions
# ═════════════════════════════════════════════════════════════════════


def invariant_i1_no_outcome_to_outcome(customer_id: int) -> List[Violation]:
    """I1: No OUTCOME→OUTCOME causal edges (backwards causality).

    An OUTCOME is a realized result. It can't be the CAUSE of another OUTCOME
    in the same account without an intermediate DECISION or SIGNAL.
    """
    from models import ContextEdge, ContextNode, db  # noqa: PLC0415

    from_alias = db.aliased(ContextNode)
    to_alias = db.aliased(ContextNode)

    rows = (
        db.session.query(ContextEdge, from_alias, to_alias)
        .join(from_alias, ContextEdge.from_node_id == from_alias.node_id)
        .join(to_alias, ContextEdge.to_node_id == to_alias.node_id)
        .filter(
            ContextEdge.customer_id == customer_id,
            ContextEdge.edge_type.in_(CAUSAL_EDGE_TYPES),
            from_alias.node_type == 'OUTCOME',
            to_alias.node_type == 'OUTCOME',
        )
        .all()
    )

    violations: List[Violation] = []
    for edge, fn, tn in rows:
        violations.append(Violation(
            invariant_id='I1',
            invariant_name='no_outcome_to_outcome_edges',
            severity='error',
            customer_id=customer_id,
            account_id=tn.account_id,
            node_ids=[fn.node_id, tn.node_id],
            edge_ids=[edge.edge_id],
            message=(
                f'OUTCOME→OUTCOME edge: "{fn.title[:50]}" ({fn.node_subtype}) '
                f'--{edge.edge_type}--> "{tn.title[:50]}" ({tn.node_subtype}) '
                f'via {edge.source_platform}'
            ),
            details={
                'from_subtype': fn.node_subtype,
                'to_subtype': tn.node_subtype,
                'edge_type': edge.edge_type,
                'source_platform': edge.source_platform,
            },
        ))
    return violations


def invariant_i2_polarity_consistency(customer_id: int) -> List[Violation]:
    """I2: No positive-signal → negative-outcome causal edges (and vice versa).

    kpi_recovery cannot CAUSE churn_lost. executive_engagement cannot cause
    a renewal_at_risk. Polarity must match through causal chains.
    """
    from models import ContextEdge, ContextNode, db  # noqa: PLC0415

    from_alias = db.aliased(ContextNode)
    to_alias = db.aliased(ContextNode)

    rows = (
        db.session.query(ContextEdge, from_alias, to_alias)
        .join(from_alias, ContextEdge.from_node_id == from_alias.node_id)
        .join(to_alias, ContextEdge.to_node_id == to_alias.node_id)
        .filter(
            ContextEdge.customer_id == customer_id,
            ContextEdge.edge_type.in_(CAUSAL_EDGE_TYPES),
            from_alias.node_type == 'SIGNAL',
            to_alias.node_type == 'OUTCOME',
        )
        .all()
    )

    violations: List[Violation] = []
    for edge, fn, tn in rows:
        fs = fn.node_subtype or ''
        ts = tn.node_subtype or ''
        # Skip edges where either end is polarity-ambiguous — those take
        # polarity from revenue_impact_type (outcome side) or arc_type
        # (signal side), not from the subtype alone.
        if ts in POLARITY_AMBIGUOUS_SUBTYPES:
            continue
        if fs in POLARITY_AMBIGUOUS_SIGNAL_SUBTYPES:
            continue
        mismatch = None
        if ts in NEGATIVE_OUTCOME_SUBTYPES and fs in POSITIVE_SIGNAL_SUBTYPES:
            mismatch = 'positive_signal_to_negative_outcome'
        elif ts in POSITIVE_OUTCOME_SUBTYPES and fs in NEGATIVE_SIGNAL_SUBTYPES:
            mismatch = 'negative_signal_to_positive_outcome'
        if not mismatch:
            continue
        violations.append(Violation(
            invariant_id='I2',
            invariant_name='polarity_consistency',
            severity='error',
            customer_id=customer_id,
            account_id=tn.account_id,
            node_ids=[fn.node_id, tn.node_id],
            edge_ids=[edge.edge_id],
            message=(
                f'Polarity mismatch ({mismatch}): '
                f'{fs} --{edge.edge_type}--> {ts} via {edge.source_platform}'
            ),
            details={'mismatch_type': mismatch, 'source_platform': edge.source_platform},
        ))
    return violations


def invariant_i3_no_orphan_revenue_outcomes(customer_id: int) -> List[Violation]:
    """I3: Every OUTCOME node with revenue_impact has ≥1 inbound causal edge.

    Orphan revenue nodes are audit-indefensible ("we lost $500K here but we
    can't point to why"). Either provenance exists or the node shouldn't.
    """
    from sqlalchemy import exists  # noqa: PLC0415

    from models import ContextEdge, ContextNode, db  # noqa: PLC0415

    has_inbound = (
        db.session.query(ContextEdge.to_node_id)
        .filter(
            ContextEdge.customer_id == customer_id,
            ContextEdge.to_node_id == ContextNode.node_id,
            ContextEdge.edge_type.in_(CAUSAL_EDGE_TYPES),
        )
        .exists()
    )

    orphans = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.revenue_impact.isnot(None),
            ~has_inbound,
        )
        .all()
    )

    violations: List[Violation] = []
    for n in orphans:
        violations.append(Violation(
            invariant_id='I3',
            invariant_name='no_orphan_revenue_outcomes',
            severity='error',
            customer_id=customer_id,
            account_id=n.account_id,
            node_ids=[n.node_id],
            message=(
                f'Orphan OUTCOME ${float(n.revenue_impact or 0):,.0f}: '
                f'"{n.title[:70]}" ({n.node_subtype}) has zero inbound causal edges'
            ),
            details={
                'revenue_impact': float(n.revenue_impact or 0),
                'node_subtype': n.node_subtype,
                'revenue_impact_type': n.revenue_impact_type,
            },
        ))
    return violations


def invariant_i4_confidence_bounds(customer_id: int) -> List[Violation]:
    """I4: All confidence values ∈ [0, 1] — on both top-level fields and
    inside properties JSONB blobs.

    Confidence > 1 is mathematically invalid and leaks into ROI math.
    """
    from models import ContextEdge, ContextNode  # noqa: PLC0415

    violations: List[Violation] = []

    # Top-level confidence on nodes.
    bad_nodes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            (ContextNode.confidence < 0) | (ContextNode.confidence > 1),
        )
        .all()
    )
    for n in bad_nodes:
        violations.append(Violation(
            invariant_id='I4',
            invariant_name='confidence_bounds',
            severity='error',
            customer_id=customer_id,
            account_id=n.account_id,
            node_ids=[n.node_id],
            message=(
                f'Node {n.node_id} confidence={n.confidence} (out of [0,1]): '
                f'"{n.title[:60]}"'
            ),
            details={
                'field': 'context_nodes.confidence',
                'value': float(n.confidence) if n.confidence is not None else None,
            },
        ))

    # Top-level confidence on edges.
    bad_edges = (
        ContextEdge.query
        .filter(
            ContextEdge.customer_id == customer_id,
            (ContextEdge.confidence < 0) | (ContextEdge.confidence > 1),
        )
        .all()
    )
    for e in bad_edges:
        violations.append(Violation(
            invariant_id='I4',
            invariant_name='confidence_bounds',
            severity='error',
            customer_id=customer_id,
            edge_ids=[e.edge_id],
            message=f'Edge {e.edge_id} confidence={e.confidence} (out of [0,1])',
            details={
                'field': 'context_edges.confidence',
                'value': float(e.confidence) if e.confidence is not None else None,
                'source_platform': e.source_platform,
            },
        ))

    # properties.confidence on nodes (JSONB).
    nodes_with_props = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.properties.isnot(None),
        )
        .all()
    )
    for n in nodes_with_props:
        props = n.properties or {}
        if not isinstance(props, dict):
            continue
        v = props.get('confidence')
        try:
            vf = float(v)
        except (TypeError, ValueError):
            continue
        if vf < 0 or vf > 1:
            violations.append(Violation(
                invariant_id='I4',
                invariant_name='confidence_bounds',
                severity='warning',
                customer_id=customer_id,
                account_id=n.account_id,
                node_ids=[n.node_id],
                message=(
                    f'Node {n.node_id} properties.confidence={vf} (out of [0,1]): '
                    f'"{n.title[:60]}"'
                ),
                details={'field': 'context_nodes.properties.confidence', 'value': vf},
            ))
    return violations


def invariant_i5_arc_type_enum(customer_id: int) -> List[Violation]:
    """I5: Every arc_type value is in the canonical enum."""
    from models import ContextEdge, ContextNode  # noqa: PLC0415

    violations: List[Violation] = []

    # Nodes — arc_type may live in properties.arc_type OR as a column.
    nodes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.properties.isnot(None),
        )
        .all()
    )
    for n in nodes:
        props = n.properties or {}
        if not isinstance(props, dict):
            continue
        arc = props.get('arc_type')
        if arc is None:
            continue
        if arc not in CANONICAL_ARC_TYPES:
            violations.append(Violation(
                invariant_id='I5',
                invariant_name='arc_type_enum',
                severity='warning',
                customer_id=customer_id,
                account_id=n.account_id,
                node_ids=[n.node_id],
                message=f'Node {n.node_id} arc_type={arc!r} not in canonical enum',
                details={'arc_type': arc, 'field': 'context_nodes.properties.arc_type'},
            ))

    # Edges — arc_type in properties as well.
    edges = (
        ContextEdge.query
        .filter(
            ContextEdge.customer_id == customer_id,
            ContextEdge.properties.isnot(None),
        )
        .all()
    )
    for e in edges:
        props = e.properties or {}
        if not isinstance(props, dict):
            continue
        arc = props.get('arc_type')
        if arc is None:
            continue
        if arc not in CANONICAL_ARC_TYPES:
            violations.append(Violation(
                invariant_id='I5',
                invariant_name='arc_type_enum',
                severity='warning',
                customer_id=customer_id,
                edge_ids=[e.edge_id],
                message=f'Edge {e.edge_id} arc_type={arc!r} not in canonical enum',
                details={'arc_type': arc, 'field': 'context_edges.properties.arc_type'},
            ))
    return violations


def invariant_i6_churned_no_future_expansion(customer_id: int) -> List[Violation]:
    """I6: Churned accounts cannot have future-dated expansion_closed outcomes."""
    from models import Account, ContextNode  # noqa: PLC0415

    churned_accts = (
        Account.query
        .filter_by(customer_id=customer_id, account_status='churned')
        .all()
    )
    if not churned_accts:
        return []

    violations: List[Violation] = []
    for acct in churned_accts:
        # Use created_at as churn-time proxy if there's no explicit field.
        churn_time_proxy = acct.updated_at or acct.created_at
        bad = (
            ContextNode.query
            .filter(
                ContextNode.customer_id == customer_id,
                ContextNode.account_id == acct.account_id,
                ContextNode.node_type == 'OUTCOME',
                ContextNode.revenue_impact_type == 'expansion_closed',
            )
            .all()
        )
        for n in bad:
            if churn_time_proxy and n.occurred_at and n.occurred_at > churn_time_proxy:
                violations.append(Violation(
                    invariant_id='I6',
                    invariant_name='churned_no_future_expansion',
                    severity='error',
                    customer_id=customer_id,
                    account_id=acct.account_id,
                    node_ids=[n.node_id],
                    message=(
                        f'Churned account {acct.account_name!r} has expansion_closed '
                        f'OUTCOME at {n.occurred_at} after churn at ~{churn_time_proxy}'
                    ),
                    details={
                        'account_name': acct.account_name,
                        'expansion_occurred_at': str(n.occurred_at),
                        'churn_proxy_at': str(churn_time_proxy),
                    },
                ))
    return violations


def invariant_i8_churn_lost_reconciled(customer_id: int) -> List[Violation]:
    """I8: Every churn_lost OUTCOME ⇒ Account.account_status = 'churned'."""
    from models import Account, ContextNode  # noqa: PLC0415

    churn_outcomes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.node_subtype == 'churn_lost',
        )
        .all()
    )
    if not churn_outcomes:
        return []

    acct_ids = {n.account_id for n in churn_outcomes if n.account_id}
    accts = {a.account_id: a for a in
             Account.query.filter(Account.account_id.in_(acct_ids)).all()}

    violations: List[Violation] = []
    for n in churn_outcomes:
        acct = accts.get(n.account_id)
        if not acct:
            continue
        if (acct.account_status or '').lower() != 'churned':
            violations.append(Violation(
                invariant_id='I8',
                invariant_name='churn_lost_reconciled',
                severity='error',
                customer_id=customer_id,
                account_id=acct.account_id,
                node_ids=[n.node_id],
                message=(
                    f'Account {acct.account_name!r} has churn_lost OUTCOME '
                    f'but account_status={acct.account_status!r} (expected churned)'
                ),
                details={
                    'account_name': acct.account_name,
                    'current_status': acct.account_status,
                },
            ))
    return violations


def invariant_i9_no_duplicate_lifecycle_outcomes(customer_id: int) -> List[Violation]:
    """I9: At most one churn_lost OUTCOME per account (no double-churn)."""
    from sqlalchemy import func  # noqa: PLC0415

    from models import Account, ContextNode, db  # noqa: PLC0415

    rows = (
        db.session.query(
            ContextNode.account_id,
            func.count(ContextNode.node_id).label('cnt'),
            func.array_agg(ContextNode.node_id).label('nids'),
        )
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.node_subtype == 'churn_lost',
        )
        .group_by(ContextNode.account_id)
        .having(func.count(ContextNode.node_id) > 1)
        .all()
    )

    violations: List[Violation] = []
    for account_id, cnt, nids in rows:
        acct = Account.query.filter_by(account_id=account_id).first()
        violations.append(Violation(
            invariant_id='I9',
            invariant_name='no_duplicate_lifecycle_outcomes',
            severity='error',
            customer_id=customer_id,
            account_id=account_id,
            node_ids=list(nids or []),
            message=(
                f'Account {acct.account_name if acct else account_id!r} has {cnt} '
                f'churn_lost OUTCOME nodes (expected ≤1)'
            ),
            details={'account_name': acct.account_name if acct else None, 'count': cnt},
        ))
    return violations


def invariant_i10_no_averted_with_lost(customer_id: int) -> List[Violation]:
    """I10: A single account cannot have BOTH churn_averted and churn_lost OUTCOMEs.

    Either the churn was averted (stayed) OR lost (churned). Both flags on the
    same account means one of them is stale/wrong — a narrative contradiction.
    """
    from sqlalchemy import func  # noqa: PLC0415

    from models import Account, ContextNode, db  # noqa: PLC0415

    subtype_sets_by_account = (
        db.session.query(
            ContextNode.account_id,
            func.array_agg(func.distinct(ContextNode.node_subtype)).label('subs'),
        )
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.node_subtype.in_(['churn_lost', 'churn_averted']),
        )
        .group_by(ContextNode.account_id)
        .all()
    )

    violations: List[Violation] = []
    for account_id, subs in subtype_sets_by_account:
        if not subs or len(set(subs)) < 2:
            continue  # only one of the two, fine
        acct = Account.query.filter_by(account_id=account_id).first()
        bad_nodes = (
            ContextNode.query
            .filter(
                ContextNode.customer_id == customer_id,
                ContextNode.account_id == account_id,
                ContextNode.node_subtype.in_(['churn_lost', 'churn_averted']),
            )
            .all()
        )
        violations.append(Violation(
            invariant_id='I10',
            invariant_name='no_averted_with_lost',
            severity='error',
            customer_id=customer_id,
            account_id=account_id,
            node_ids=[n.node_id for n in bad_nodes],
            message=(
                f'Account {acct.account_name if acct else account_id!r} has both '
                f'churn_averted AND churn_lost OUTCOMEs — mutually exclusive'
            ),
            details={
                'account_name': acct.account_name if acct else None,
                'subtypes': sorted(set(subs)),
            },
        ))
    return violations


def invariant_i11_revenue_bucket_consistency(customer_id: int) -> List[Violation]:
    """I11: revenue_impact_type, when set, matches the node_subtype's
    polarity bucket.

    This field is stored as a fine-grained tag (e.g. 'revenue_protected')
    not as a bucket key ('protected'). A violation means the tag's polarity
    disagrees with the subtype's polarity — e.g. a churn_lost subtype
    tagged 'expansion_closed' or 'revenue_protected' (polarity flip).

    Both the bucket keys and the subtype values are accepted as valid tags.
    """
    from models import ContextNode  # noqa: PLC0415

    # All recognised values (bucket keys + subtype names).
    all_subtypes: set = set()
    for subs in REVENUE_BUCKET_MAP.values():
        all_subtypes.update(subs)
    all_valid_tags = set(REVENUE_BUCKET_MAP.keys()) | all_subtypes

    # Which bucket does each subtype belong to?
    subtype_to_bucket: Dict[str, str] = {}
    for bucket, subs in REVENUE_BUCKET_MAP.items():
        for s in subs:
            subtype_to_bucket[s] = bucket

    # Which bucket does each tag map to? If the tag IS a bucket key, trivial.
    tag_to_bucket = dict(subtype_to_bucket)
    for b in REVENUE_BUCKET_MAP:
        tag_to_bucket[b] = b

    nodes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.revenue_impact_type.isnot(None),
            ContextNode.node_subtype.isnot(None),
        )
        .all()
    )

    violations: List[Violation] = []
    for n in nodes:
        sub = n.node_subtype
        tag = n.revenue_impact_type

        # Generic wrapper subtypes don't have an intrinsic polarity bucket —
        # skip the cross-check. Their polarity comes from the tag.
        if sub in POLARITY_AMBIGUOUS_SUBTYPES:
            continue

        sub_bucket = subtype_to_bucket.get(sub)
        tag_bucket = tag_to_bucket.get(tag)

        # Unknown tag or unknown subtype — flag as "unrecognised"
        if tag not in all_valid_tags:
            violations.append(Violation(
                invariant_id='I11',
                invariant_name='revenue_bucket_consistency',
                severity='warning',
                customer_id=customer_id,
                account_id=n.account_id,
                node_ids=[n.node_id],
                message=(
                    f'Node {n.node_id} revenue_impact_type={tag!r} is not a '
                    f'recognized bucket or subtype'
                ),
                details={'subtype': sub, 'tag': tag, 'known_tags': sorted(all_valid_tags)},
            ))
            continue

        # Both known — check polarity bucket matches.
        if sub_bucket and tag_bucket and sub_bucket != tag_bucket:
            violations.append(Violation(
                invariant_id='I11',
                invariant_name='revenue_bucket_consistency',
                severity='error',
                customer_id=customer_id,
                account_id=n.account_id,
                node_ids=[n.node_id],
                message=(
                    f'Node {n.node_id} subtype={sub!r} (bucket={sub_bucket!r}) '
                    f'tagged as revenue_impact_type={tag!r} (bucket={tag_bucket!r}) '
                    f'— polarity mismatch'
                ),
                details={
                    'subtype': sub,
                    'tag': tag,
                    'subtype_bucket': sub_bucket,
                    'tag_bucket': tag_bucket,
                },
            ))
    return violations


def invariant_i12_account_status_health_consistency(customer_id: int) -> List[Violation]:
    """I12: Account.account_status should not be 'at_risk' if health_score >= 70.

    account_status is either (a) an independent business field that should
    never contradict health, or (b) a stale artifact that should be removed.
    Either way, a healthy (≥70) account tagged at_risk is a contradiction.
    """
    from models import Account, HealthScore, db  # noqa: PLC0415
    from sqlalchemy import func  # noqa: PLC0415

    # Latest health score per account
    latest_hs_subq = (
        db.session.query(
            HealthScore.account_id,
            func.max(HealthScore.measurement_month).label('mm'),
        )
        .group_by(HealthScore.account_id)
        .subquery()
    )
    latest_scores = (
        db.session.query(HealthScore.account_id, HealthScore.health_score)
        .join(
            latest_hs_subq,
            (HealthScore.account_id == latest_hs_subq.c.account_id)
            & (HealthScore.measurement_month == latest_hs_subq.c.mm),
        )
        .all()
    )
    score_map = {aid: float(s or 0) for aid, s in latest_scores}

    accts = Account.query.filter_by(customer_id=customer_id).all()
    violations: List[Violation] = []
    for a in accts:
        status = (a.account_status or '').lower()
        score = score_map.get(a.account_id)
        if score is None:
            continue
        # Don't flag churned accounts — that's a legitimate terminal state.
        if status == 'churned':
            continue
        if score >= 70 and status == 'at_risk':
            violations.append(Violation(
                invariant_id='I12',
                invariant_name='account_status_health_consistency',
                severity='warning',
                customer_id=customer_id,
                account_id=a.account_id,
                message=(
                    f'Account {a.account_name!r} health={score:.1f} (healthy) '
                    f'but account_status={status!r}'
                ),
                details={
                    'account_name': a.account_name,
                    'health_score': score,
                    'account_status': a.account_status,
                },
            ))
        elif score < 50 and status == 'active':
            violations.append(Violation(
                invariant_id='I12',
                invariant_name='account_status_health_consistency',
                severity='warning',
                customer_id=customer_id,
                account_id=a.account_id,
                message=(
                    f'Account {a.account_name!r} health={score:.1f} (critical) '
                    f'but account_status={status!r} (expected at_risk or intervention)'
                ),
                details={
                    'account_name': a.account_name,
                    'health_score': score,
                    'account_status': a.account_status,
                },
            ))
    return violations


# ═════════════════════════════════════════════════════════════════════
# Registry + public API
# ═════════════════════════════════════════════════════════════════════


# ── Definitive lifecycle subtypes that Wizard B's NRR math summs. ──
# Any duplication here double-counts revenue in NRR / CFO dashboards.
DEFINITIVE_LIFECYCLE_SUBTYPES = {
    'churn_lost', 'contraction', 'expansion_closed', 'new_logo',
}


def invariant_i13_no_duplicate_definitive_lifecycle(customer_id: int) -> List[Violation]:
    """I13: At most one OUTCOME per (account, definitive lifecycle subtype).

    Wizard B's NRR math iterates ContextNode where node_type='OUTCOME' and
    node_subtype in {churn_lost, contraction, expansion_closed, new_logo},
    summing revenue_impact. If two paths create the same logical event
    (e.g., scenario_manifest.generate_outcomes_csv AND
    _create_lifecycle_outcomes BOTH fire for one expansion event, or
    LLM enrichment adds a third copy), the account contributes 2× or 3×
    its actual ARR change to the portfolio NRR.

    This invariant catches cross-source duplicates by ignoring
    source_platform — it's specifically a Wizard-B correctness guard.
    Related to I9 but broader: I9 handles only churn_lost.
    """
    from sqlalchemy import func  # noqa: PLC0415

    from models import Account, ContextNode, db  # noqa: PLC0415

    rows = (
        db.session.query(
            ContextNode.account_id,
            ContextNode.node_subtype,
            func.count(ContextNode.node_id).label('cnt'),
            func.array_agg(ContextNode.node_id).label('nids'),
            func.array_agg(ContextNode.source_platform).label('sources'),
            func.sum(ContextNode.revenue_impact).label('total_impact'),
        )
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.node_subtype.in_(DEFINITIVE_LIFECYCLE_SUBTYPES),
        )
        .group_by(ContextNode.account_id, ContextNode.node_subtype)
        .having(func.count(ContextNode.node_id) > 1)
        .all()
    )

    violations: List[Violation] = []
    for account_id, subtype, cnt, nids, sources, total_impact in rows:
        acct = Account.query.filter_by(account_id=account_id).first()
        violations.append(Violation(
            invariant_id='I13',
            invariant_name='no_duplicate_definitive_lifecycle',
            severity='error',
            customer_id=customer_id,
            account_id=account_id,
            node_ids=list(nids or []),
            message=(
                f'Account {acct.account_name if acct else account_id!r} has {cnt} '
                f'{subtype!r} OUTCOME nodes from sources {list(sources or [])} — '
                f'Wizard B will double-count ${float(total_impact or 0):,.0f}'
            ),
            details={
                'account_name': acct.account_name if acct else None,
                'subtype': subtype,
                'count': cnt,
                'sources': list(sources or []),
                'combined_revenue_impact': float(total_impact or 0),
            },
        ))
    return violations


def invariant_i14_revenue_sign_matches_polarity(customer_id: int) -> List[Violation]:
    """I14: revenue_impact sign must match subtype polarity.

    churn_lost / contraction must be negative (or zero).
    expansion_closed / new_logo / revenue_protected / churn_averted /
    revenue_growth / expansion_approved must be positive (or zero).

    Catches LLM / ingest errors where "lost $1M" gets stored as +1,000,000
    flipping the sign in NRR math.
    """
    from models import ContextNode  # noqa: PLC0415

    MUST_BE_NEGATIVE = {'churn_lost', 'contraction'}
    MUST_BE_POSITIVE = {
        'expansion_closed', 'new_logo', 'revenue_protected', 'churn_averted',
        'revenue_growth', 'expansion_approved', 'revenue_expanded',
        'renewal_secured',
    }

    nodes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type == 'OUTCOME',
            ContextNode.node_subtype.in_(MUST_BE_NEGATIVE | MUST_BE_POSITIVE),
            ContextNode.revenue_impact.isnot(None),
        )
        .all()
    )

    violations: List[Violation] = []
    for n in nodes:
        impact = float(n.revenue_impact or 0)
        sub = n.node_subtype
        if sub in MUST_BE_NEGATIVE and impact > 0:
            violations.append(Violation(
                invariant_id='I14',
                invariant_name='revenue_sign_matches_polarity',
                severity='error',
                customer_id=customer_id,
                account_id=n.account_id,
                node_ids=[n.node_id],
                message=(
                    f'Node {n.node_id} subtype={sub!r} has positive revenue_impact '
                    f'${impact:,.0f} — should be negative (loss)'
                ),
                details={'subtype': sub, 'revenue_impact': impact,
                         'expected': 'negative'},
            ))
        elif sub in MUST_BE_POSITIVE and impact < 0:
            violations.append(Violation(
                invariant_id='I14',
                invariant_name='revenue_sign_matches_polarity',
                severity='error',
                customer_id=customer_id,
                account_id=n.account_id,
                node_ids=[n.node_id],
                message=(
                    f'Node {n.node_id} subtype={sub!r} has negative revenue_impact '
                    f'${impact:,.0f} — should be positive (gain)'
                ),
                details={'subtype': sub, 'revenue_impact': impact,
                         'expected': 'positive'},
            ))
    return violations


def invariant_i15_no_duplicate_signal_outcome_pair(customer_id: int) -> List[Violation]:
    """I15: no same-title SIGNAL+OUTCOME pair on the same account on the same day.

    Catches the Wizard A template pattern where a single causal event
    (e.g. "Jul 31: KPI metrics declining") is written both as a SIGNAL
    node and as an OUTCOME node with identical/near-identical title.
    Inflates node counts, confuses buyers drilling into the graph, and
    causes double-counting in "evidence count" metrics downstream.

    Severity: warning (structural pattern, no revenue-math impact).
    """
    from models import ContextNode  # noqa: PLC0415
    from collections import defaultdict

    # Wizard A templates write the same causal event as a signal node and
    # a narrative outcome node; the titles share a long prefix but differ
    # in their tail (e.g. "Account health trending upward: +12 points" vs
    # "Account health trending upward +12 points over 4 weeks; recovery...").
    # Use prefix-overlap detection: if the shorter normalized title is a
    # prefix of the longer (>= 30 chars), treat as match.
    import re as _re
    def _normalize(t: Optional[str]) -> str:
        if not t:
            return ''
        s = t.strip().lower()
        s = _re.sub(r'[^\w\s]+', ' ', s)
        s = _re.sub(r'\s+', ' ', s).strip()
        return s

    def _is_prefix_match(a: str, b: str, min_overlap: int = 30) -> bool:
        if not a or not b:
            return False
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if len(short) < min_overlap:
            return short == long_  # exact-match fallback for short titles
        return long_.startswith(short)

    rows = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.node_type.in_(('SIGNAL', 'OUTCOME')),
        )
        .all()
    )

    # Bucket first by (account_id, day) — O(N) — then pair-check within bucket.
    day_buckets: Dict[tuple, Dict[str, List]] = defaultdict(lambda: {'SIGNAL': [], 'OUTCOME': []})
    for n in rows:
        if not n.occurred_at:
            continue
        day_buckets[(n.account_id, n.occurred_at.date())][n.node_type].append(n)

    violations: List[Violation] = []
    for (aid, day), byt in day_buckets.items():
        signals = byt['SIGNAL']
        outcomes = byt['OUTCOME']
        if not signals or not outcomes:
            continue
        # Only narrative-only outcomes trigger — revenue-bearing are exempt.
        narrative_outcomes = [o for o in outcomes if o.revenue_impact is None]
        if not narrative_outcomes:
            continue
        matched_pairs: List[tuple] = []
        for s in signals:
            s_norm = _normalize(s.title)
            for o in narrative_outcomes:
                o_norm = _normalize(o.title)
                if _is_prefix_match(s_norm, o_norm):
                    matched_pairs.append((s, o))
        if not matched_pairs:
            continue
        node_ids = list({p[0].node_id for p in matched_pairs} | {p[1].node_id for p in matched_pairs})
        example_signal, example_outcome = matched_pairs[0]
        violations.append(Violation(
            invariant_id='I15',
            invariant_name='no_duplicate_signal_outcome_pair',
            severity='warning',
            customer_id=customer_id,
            account_id=aid,
            node_ids=node_ids,
            message=(
                f'Account {aid} day {day}: {len(matched_pairs)} SIGNAL+narrative-OUTCOME '
                f'prefix-duplicate pair(s) — Wizard A template duplicate. '
                f'Example: "{_normalize(example_signal.title)[:60]}" <-> '
                f'"{_normalize(example_outcome.title)[:60]}"'
            ),
            details={
                'day': day.isoformat(),
                'pair_count': len(matched_pairs),
                'signal_count': len(signals),
                'narrative_outcome_count': len(narrative_outcomes),
            },
        ))
    return violations


def invariant_i17_no_reverse_time_causal_edges(customer_id: int) -> List[Violation]:
    """I17: causal edges must respect temporal ordering.

    For edge types in CAUSAL_EDGE_TYPES (CAUSED_BY, LED_TO, TRIGGERED,
    RESULTED_IN, INDICATES), the from_node's occurred_at MUST be <=
    to_node's occurred_at. A cause cannot occur AFTER its effect.

    The canonical violator is Wizard A's arc_detection signal: it fires
    at customer-creation time (today) and back-emits LED_TO edges to
    pre-existing outcomes from weeks/months earlier. Mechanically
    backward in time.

    Apr 20 demo fix shipped a display-layer filter in
    get_context_graph_mermaid + get_account_graph_summary to hide these
    edges from buyer-facing surfaces. This invariant adds Layer C audit
    detection so the underlying data-quality issue surfaces in audits
    even if display filters hide it from dashboards.

    Severity: warning (structural, not revenue-math-breaking).
    """
    from models import ContextEdge, ContextNode, db  # noqa: PLC0415

    from_alias = db.aliased(ContextNode)
    to_alias = db.aliased(ContextNode)

    rows = (
        db.session.query(ContextEdge, from_alias, to_alias)
        .join(from_alias, ContextEdge.from_node_id == from_alias.node_id)
        .join(to_alias, ContextEdge.to_node_id == to_alias.node_id)
        .filter(
            ContextEdge.customer_id == customer_id,
            ContextEdge.edge_type.in_(CAUSAL_EDGE_TYPES),
            from_alias.occurred_at.isnot(None),
            to_alias.occurred_at.isnot(None),
            from_alias.occurred_at > to_alias.occurred_at,
        )
        .all()
    )

    violations: List[Violation] = []
    for edge, fn, tn in rows:
        time_delta = (fn.occurred_at - tn.occurred_at).days
        violations.append(Violation(
            invariant_id='I17',
            invariant_name='no_reverse_time_causal_edges',
            severity='warning',
            customer_id=customer_id,
            account_id=tn.account_id,
            node_ids=[fn.node_id, tn.node_id],
            edge_ids=[edge.edge_id],
            message=(
                f'Reverse-time causal edge: "{(fn.title or "")[:40]}" ({fn.occurred_at.date()}) '
                f'--{edge.edge_type}--> "{(tn.title or "")[:40]}" ({tn.occurred_at.date()}) — '
                f'cause occurs {time_delta} days AFTER effect (producer: {edge.source_platform})'
            ),
            details={
                'from_subtype': fn.node_subtype,
                'to_subtype': tn.node_subtype,
                'edge_type': edge.edge_type,
                'from_date': fn.occurred_at.date().isoformat(),
                'to_date': tn.occurred_at.date().isoformat(),
                'delta_days': time_delta,
                'source_platform': edge.source_platform,
            },
        ))
    return violations


def invariant_i16_churned_not_in_at_risk(customer_id: int) -> List[Violation]:
    """I16: no account should appear as both churned AND at-risk.

    Churned is a terminal state; at-risk is a live-intervention state.
    A tool returning an account in both outputs for the same query is a
    contract bug that inflates at-risk ARR rollups by the churned amount
    (the `get_at_risk_accounts` Apr 20 regression that prompted this
    invariant).

    Detection: any Account where `account_status='churned'` AND current
    health_score < at_risk threshold (which would trigger at-risk inclusion
    under any tool that doesn't explicitly filter churned).
    """
    from models import Account, HealthScore, db  # noqa: PLC0415
    import utils.health_thresholds as ht  # noqa: PLC0415

    violations: List[Violation] = []
    at_risk_threshold = ht.healthy_min()

    churned_accounts = (
        Account.query
        .filter(
            Account.customer_id == customer_id,
            db.func.lower(Account.account_status) == 'churned',
        )
        .all()
    )

    for acct in churned_accounts:
        latest = (
            HealthScore.query
            .filter_by(account_id=acct.account_id)
            .order_by(
                HealthScore.measurement_month.desc(),
                HealthScore.calculated_at.desc(),
            )
            .first()
        )
        if latest is None or latest.health_score is None:
            continue
        score = float(latest.health_score)
        if score < at_risk_threshold:
            violations.append(Violation(
                invariant_id='I16',
                invariant_name='churned_not_in_at_risk',
                severity='warning',
                customer_id=customer_id,
                account_id=acct.account_id,
                node_ids=[],
                message=(
                    f'Account {acct.account_name!r} is churned AND health '
                    f'{score:.1f} < {at_risk_threshold} — any tool that '
                    f'returns at-risk-by-health without filtering '
                    f'account_status=churned will double-count this ARR '
                    f'(${float(acct.revenue or 0):,.0f}) across both buckets.'
                ),
                details={
                    'account_name': acct.account_name,
                    'health_score': score,
                    'arr': float(acct.revenue or 0),
                    'account_status': acct.account_status,
                },
            ))
    return violations


INVARIANTS_REGISTRY: Dict[str, Callable[[int], List[Violation]]] = {
    'I1': invariant_i1_no_outcome_to_outcome,
    'I2': invariant_i2_polarity_consistency,
    'I3': invariant_i3_no_orphan_revenue_outcomes,
    'I4': invariant_i4_confidence_bounds,
    'I5': invariant_i5_arc_type_enum,
    'I6': invariant_i6_churned_no_future_expansion,
    'I8': invariant_i8_churn_lost_reconciled,
    'I9': invariant_i9_no_duplicate_lifecycle_outcomes,
    'I10': invariant_i10_no_averted_with_lost,
    'I11': invariant_i11_revenue_bucket_consistency,
    'I12': invariant_i12_account_status_health_consistency,
    'I13': invariant_i13_no_duplicate_definitive_lifecycle,
    'I14': invariant_i14_revenue_sign_matches_polarity,
    'I15': invariant_i15_no_duplicate_signal_outcome_pair,
    'I16': invariant_i16_churned_not_in_at_risk,
    'I17': invariant_i17_no_reverse_time_causal_edges,
    # I7 (MCP param contract) is a pure code check — lives in tests, not here.
}


def run_all_invariants(customer_id: int) -> List[Violation]:
    """Run every invariant and return the flat list of violations."""
    all_violations: List[Violation] = []
    for inv_id, fn in INVARIANTS_REGISTRY.items():
        try:
            vs = fn(customer_id)
            all_violations.extend(vs)
        except Exception as e:
            logger.warning(
                'Invariant %s failed to execute for customer %d: %s',
                inv_id, customer_id, e,
            )
    return all_violations


def run_invariant(invariant_id: str, customer_id: int) -> List[Violation]:
    """Run a single invariant by ID."""
    fn = INVARIANTS_REGISTRY.get(invariant_id)
    if not fn:
        raise ValueError(f'Unknown invariant: {invariant_id!r}')
    return fn(customer_id)


# ═════════════════════════════════════════════════════════════════════
# Pre-commit validators — called from upsert_node/upsert_edge to reject
# invariant-violating rows BEFORE they land in the DB. Post-commit audit
# (run_all_invariants) is the backstop; these are the gate.
# ═════════════════════════════════════════════════════════════════════


def validate_edge_pre_commit(
    from_node_type: Optional[str],
    from_node_subtype: Optional[str],
    to_node_type: Optional[str],
    to_node_subtype: Optional[str],
    edge_type: str,
    source_platform: str,
    from_occurred_at=None,
    to_occurred_at=None,
) -> tuple:
    """Return (is_valid, reject_reason). Reject reason is None when valid.

    Enforces:
      I1 — no OUTCOME→OUTCOME causal edges
      I2 — no polarity-mismatched signal→outcome edges
      I17 — no reverse-time causal edges (cause.occurred_at > effect.occurred_at).
            Added Apr 21 2026 to reduce accumulation of mechanically-backward
            edges that surface in buyer-visible causal chains.

    Non-causal edge types (INVOLVES, RELATED_TO, etc.) pass through.
    Temporal check is skipped when either occurred_at is None (legacy data /
    unknown timestamp — validator can't prove direction).
    """
    if edge_type not in CAUSAL_EDGE_TYPES:
        return True, None

    # I17: reverse-time causal edge.
    if from_occurred_at is not None and to_occurred_at is not None:
        if from_occurred_at > to_occurred_at:
            return False, (
                f'I17: reverse-time causal edge rejected '
                f'({from_node_subtype} at {from_occurred_at} '
                f'--{edge_type}--> {to_node_subtype} at {to_occurred_at} '
                f'via {source_platform})'
            )

    # I1: Block OUTCOME→OUTCOME causal edges.
    if from_node_type == 'OUTCOME' and to_node_type == 'OUTCOME':
        return False, (
            f'I1: OUTCOME→OUTCOME edge rejected '
            f'({from_node_subtype} --{edge_type}--> {to_node_subtype} '
            f'via {source_platform})'
        )

    # I2: Polarity mismatch on signal→outcome. Skip when either end is
    # polarity-ambiguous (polarity comes from arc_type or revenue_impact_type
    # tag, not the subtype alone).
    if from_node_type == 'SIGNAL' and to_node_type == 'OUTCOME':
        fs = from_node_subtype or ''
        ts = to_node_subtype or ''
        if ts in POLARITY_AMBIGUOUS_SUBTYPES:
            return True, None
        if fs in POLARITY_AMBIGUOUS_SIGNAL_SUBTYPES:
            return True, None
        if ts in NEGATIVE_OUTCOME_SUBTYPES and fs in POSITIVE_SIGNAL_SUBTYPES:
            return False, (
                f'I2: positive-signal→negative-outcome rejected '
                f'({fs} --{edge_type}--> {ts} via {source_platform})'
            )
        if ts in POSITIVE_OUTCOME_SUBTYPES and fs in NEGATIVE_SIGNAL_SUBTYPES:
            return False, (
                f'I2: negative-signal→positive-outcome rejected '
                f'({fs} --{edge_type}--> {ts} via {source_platform})'
            )

    return True, None


def clamp_confidence(value: Optional[float]) -> Optional[float]:
    """I4: Clamp confidence to [0, 1]. Returns None unchanged (field nullable)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, v))


def sanitize_properties_confidence(
    properties: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """I4: If properties.confidence exists and is out of [0,1], clamp it."""
    if not isinstance(properties, dict):
        return properties
    v = properties.get('confidence')
    if v is None:
        return properties
    try:
        f = float(v)
    except (TypeError, ValueError):
        return properties
    if 0.0 <= f <= 1.0:
        return properties
    # Clamp without mutating the caller's dict.
    new = dict(properties)
    new['confidence'] = max(0.0, min(1.0, f))
    logger.warning(
        '[invariants] I4 clamp: properties.confidence %s → %s',
        f, new['confidence'],
    )
    return new


# ─────────────────────────────────────────────────────────────────────
# I3' — Unearned-confidence clamp (April 2026)
# ─────────────────────────────────────────────────────────────────────
# Complements I3 (post-hoc orphan detection) with a *write-time* clamp.
# An OUTCOME node that arrives without any of (a) properties.evidence,
# (b) source_ref pointing at a specific external record, has no basis
# to claim Tier-1 status or high confidence. Rather than rejecting the
# write (scope 2, two-pass transactional — not yet), we DOWNGRADE:
#   - confidence capped at 0.3 (below every user-facing threshold:
#     node-storage 0.55, human-review 0.70, auto-execute 0.85, and
#     MOD-004 aggregation filter 0.50 — see get_revenue_at_risk)
#   - tier forced to 2 (Tier-1 status is earned via evidence, not asserted)
#   - properties.evidence_clamped = True (marker for audit + UI)
#
# The node still lands (same ID, same subtype, same revenue_impact) so
# no producer breaks. Only its *claim strength* is honestly downgraded.
# The MOD-004 read-side filter then excludes it from CFO/CRO aggregates.
#
# Why 0.3: every decision threshold in the codebase is >= 0.4. Picking
# 0.3 puts unearned claims demonstrably below every filter. Picking 0.5
# would sit on the MOD-004 filter boundary (fragile to >= vs > drift).

_UNEARNED_CLAMP_FLOOR = 0.3
_UNEARNED_CLAMP_TIER = 2


def _has_evidence(properties: Optional[Dict[str, Any]]) -> bool:
    """True iff properties carries a non-empty evidence field or list."""
    if not isinstance(properties, dict):
        return False
    ev = properties.get('evidence')
    if isinstance(ev, str) and ev.strip():
        return True
    if isinstance(ev, (list, tuple)) and any(
        isinstance(x, str) and x.strip() for x in ev
    ):
        return True
    ev_list = properties.get('evidence_list')
    if isinstance(ev_list, (list, tuple)) and any(
        isinstance(x, str) and x.strip() for x in ev_list
    ):
        return True
    return False


def _has_specific_source_ref(source_ref: Optional[str]) -> bool:
    """True iff source_ref points at a specific external record (non-empty)."""
    if not source_ref or not isinstance(source_ref, str):
        return False
    return bool(source_ref.strip())


def clamp_unearned_confidence(
    node_type: Optional[str],
    source_platform: Optional[str],
    source_ref: Optional[str],
    confidence: Optional[float],
    properties: Optional[Dict[str, Any]],
    tier: Optional[int],
) -> tuple:
    """Apply the unearned-confidence clamp to an OUTCOME write.

    An OUTCOME without evidence (neither properties.evidence nor a specific
    source_ref) is downgraded:
      - confidence capped at 0.3
      - tier forced to 2
      - properties['evidence_clamped'] = True

    Only clamps OUTCOME nodes. SIGNAL / DECISION / STAKEHOLDER pass through.

    Returns (confidence, properties, tier, was_clamped) — caller applies.
    """
    if node_type != 'OUTCOME':
        return confidence, properties, tier, False

    if _has_evidence(properties) or _has_specific_source_ref(source_ref):
        # Earned claim — pass through untouched.
        return confidence, properties, tier, False

    # Unearned — clamp.
    try:
        cur_conf = float(confidence) if confidence is not None else 1.0
    except (TypeError, ValueError):
        cur_conf = 1.0
    new_conf = min(cur_conf, _UNEARNED_CLAMP_FLOOR)

    new_props = dict(properties) if isinstance(properties, dict) else {}
    new_props['evidence_clamped'] = True
    new_props['evidence_clamped_reason'] = (
        f'OUTCOME written via {source_platform or "unknown"} with no '
        f'properties.evidence and no source_ref — confidence clamped '
        f'from {cur_conf} to {new_conf}, tier forced to {_UNEARNED_CLAMP_TIER}'
    )

    return new_conf, new_props, _UNEARNED_CLAMP_TIER, True


# ─────────────────────────────────────────────────────────────────────
# I3'-E — Unearned-confidence clamp, extended to edges (WS-2 2f, Aug 2026)
# ─────────────────────────────────────────────────────────────────────
# Extends I3' (above) from OUTCOME nodes to causal-type edges. Full
# reasoning + citations: docs/WS2_2F_2G_SCOPING.md §1.5-1.6 and §3 Q3 —
# this comment is a pointer, not a restatement.
#
# Scope: causal edge types only (CAUSAL_EDGE_TYPES) — mirrors I2's own
# non-causal exclusion (see validate_edge_pre_commit above). INVOLVES,
# BELONGS_TO, BENCHMARKED_BY, SOURCED_FROM never make an evidentiary
# claim and are out of scope.
#
# Trigger ("unearned") — BOTH conditions:
#   1. evidence_tier (properties['evidence_tier'] — not a real column)
#      is 'inferred', 'unknown', OR ABSENT ENTIRELY. Absence is
#      deliberately IN SCOPE, not exempted (§3 Q3 decision): grepping
#      every edge writer found ~100% of the live population
#      (llm_enrichment, wizard_a, llm_inference, signal_analyst,
#      urgent_signal_scanner) never stamps evidence_tier at all —
#      exempting absence would make this invariant check almost nothing.
#   2. No evidence content: properties.evidence / evidence_list is
#      empty/absent, AND `derivation` (if present) does not resolve to
#      a system.external logged fact per utils/edge_factory.py's
#      vocabulary — system.self.* derivations (AUTO_TRIGGER_DERIVATION,
#      CLOSE_LINK_DERIVATION) are the platform reacting to its own
#      inference/trigger and do NOT count as evidence.
#
# Rollout: SHADOW MODE ONLY. EDGE_CLAMP_ENFORCE defaults to False —
# given this fires on ~3,287 edges' worth of writers going forward, the
# write path computes and logs what WOULD be clamped and stamps a
# non-authoritative properties['would_be_clamped'] marker, without
# touching the real confidence / evidence_tier / evidence_clamped
# fields, until volume is confirmed safe. Flipping EDGE_CLAMP_ENFORCE to
# True is the ONLY step needed to go from shadow to enforce — see
# utils/context_graph.py's upsert_edge() for the dispatch.
#
# Unlike node-side I3', ContextEdge has no `tier` column (models.py) to
# force — the only real field enforcement can mutate is `confidence`
# (and only when it isn't already None; EdgeFactory-written inferred
# edges are NULL confidence by design — nothing to lower).
#
# Unregistered by design, mirroring I3' itself: NOT added to
# INVARIANTS_REGISTRY, NOT part of the post-commit audit sweep (same
# precedent as I7, "lives in tests, not here").

EDGE_CLAMP_ENFORCE = False  # SHADOW MODE. The ONLY change needed to move
# I3'-E from shadow-mode (log + non-authoritative marker only) to full
# enforcement (real confidence clamp + real evidence_clamped stamp) is
# flipping this to True, once shadow-mode volume is confirmed safe per
# docs/WS2_2F_2G_SCOPING.md §3 Q3. Do not add any other gate.

_EDGE_UNEARNED_TIERS = {'inferred', 'unknown', None, ''}


def _edge_derivation_is_system_external(derivation: Optional[str]) -> bool:
    """True iff `derivation` resolves to a system.external logged fact.

    Per utils/edge_factory.py's vocabulary: system.self.* derivations
    (AUTO_TRIGGER_DERIVATION, CLOSE_LINK_DERIVATION) are the platform
    reacting to its own prior inference/trigger — NOT evidence.
    system.external.* would be a genuinely external logged fact (an SoR
    sync, a recorded trigger condition) and DOES count as evidence. No
    writer stamps system.external.* yet — it's named in
    edge_factory.py's docstring as the counterpart category, not a
    value any writer emits today — so this check is forward-compatible
    with the day one does, not dead code for a hypothetical.
    """
    if not isinstance(derivation, str):
        return False
    return derivation.startswith('system.external')


def check_unearned_confidence_edge(
    edge_type: str,
    evidence_tier: Optional[str],
    derivation: Optional[str],
    properties: Optional[Dict[str, Any]],
    confidence: Optional[float],
) -> tuple:
    """Check (does NOT apply) whether a causal edge write trips I3'-E.

    Pure function — never mutates `properties`, never reads
    EDGE_CLAMP_ENFORCE. The caller (upsert_edge) decides, based on that
    flag, whether to stamp a shadow marker or perform the real mutation.

    Returns (would_clamp, would_be_confidence, reason):
      - would_clamp: bool
      - would_be_confidence: the confidence value enforcement WOULD
        write (None if confidence is already None — an EdgeFactory
        inferred edge has nothing to lower)
      - reason: human-readable explanation, or None when would_clamp is
        False

    Non-causal edge types always pass through unchanged — only
    CAUSAL_EDGE_TYPES make an evidentiary claim (mirrors I2's scope).
    """
    if edge_type not in CAUSAL_EDGE_TYPES:
        return False, confidence, None

    tier = evidence_tier.strip().lower() if isinstance(evidence_tier, str) else evidence_tier
    if tier not in _EDGE_UNEARNED_TIERS:
        # An explicit non-inferred/unknown tier (e.g. 'observed', 'asserted')
        # — already earned, not this invariant's concern.
        return False, confidence, None

    if _has_evidence(properties) or _edge_derivation_is_system_external(derivation):
        # Earned claim — pass through untouched.
        return False, confidence, None

    # Unearned — compute what enforcement WOULD do (does not apply it).
    try:
        cur_conf = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        cur_conf = None
    would_be_conf = None if cur_conf is None else min(cur_conf, _UNEARNED_CLAMP_FLOOR)

    tier_display = evidence_tier if evidence_tier is not None else '<absent>'
    reason = (
        f'{edge_type} edge has evidence_tier={tier_display!r} and no '
        f'evidence content (derivation={derivation!r}) — confidence '
        f'would move from {cur_conf} to {would_be_conf}'
    )
    return True, would_be_conf, reason


def log_violations_summary(violations: List[Violation], customer_id: int) -> Dict[str, Any]:
    """Log a one-line WARN summary + per-invariant counts. Return the summary dict."""
    if not violations:
        logger.info('[context_graph_invariants] customer=%d: clean (0 violations)', customer_id)
        return {'customer_id': customer_id, 'total': 0, 'by_invariant': {}}

    by_inv: Dict[str, int] = {}
    for v in violations:
        by_inv[v.invariant_id] = by_inv.get(v.invariant_id, 0) + 1

    logger.warning(
        '[context_graph_invariants] customer=%d: %d violations — %s',
        customer_id, len(violations),
        ', '.join(f'{k}:{v}' for k, v in sorted(by_inv.items())),
    )
    # Log up to 5 example violations per invariant.
    for inv_id, _cnt in sorted(by_inv.items()):
        examples = [v for v in violations if v.invariant_id == inv_id][:5]
        for ex in examples:
            logger.warning(
                '  [%s] %s (account=%s, nodes=%s, edges=%s)',
                ex.invariant_id, ex.message, ex.account_id, ex.node_ids, ex.edge_ids,
            )

    return {
        'customer_id': customer_id,
        'total': len(violations),
        'by_invariant': by_inv,
        'timestamp': datetime.utcnow().isoformat(),
    }
