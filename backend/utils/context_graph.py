"""
Context Graph Query Helpers

Provides common graph traversal operations for the context graph.
All functions are guarded by the context_graph feature toggle.

Usage:
    from utils.context_graph import get_nodes, get_edges, traverse_2hop, get_revenue_at_risk, aggregate_revenue_across_accounts
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from extensions import db
from models import ContextNode, ContextEdge

logger = logging.getLogger(__name__)

# Dedicated logger for pre-commit rejection events. Ops tooling / SIEM can
# filter on this logger name to route alerts when an unknown subtype hits the
# invariant gate. Deduped per-process on (from_subtype, to_subtype, reason_key)
# to avoid alert spam when the same bad edge is retried many times.
_precommit_rejection_logger = logging.getLogger('cs_pulse.pre_commit_rejection')
_seen_rejections: set = set()

# Dedicated logger for unearned-confidence clamp events (I3' — OUTCOME written
# without evidence → confidence capped at 0.3, tier forced to 2). Ops tooling
# filters on this logger name to surface "producers emitting unearned OUTCOMEs"
# for investigation. Deduped per-process on (source_platform, subtype, impact).
_unearned_clamp_logger = logging.getLogger('cs_pulse.unearned_confidence_clamp')
_seen_unearned_clamps: set = set()

# Dedicated logger for the edge-side extension of the unearned-confidence
# clamp (I3'-E, WS-2 2f — SHADOW MODE, see context_graph_invariants.py's
# EDGE_CLAMP_ENFORCE). Deliberately a SEPARATE dedup set from the node-side
# one above — edges and nodes are different populations and sharing a set
# would let one starve the other's first-occurrence alert.
_edge_unearned_clamp_logger = logging.getLogger('cs_pulse.unearned_confidence_clamp_edge')
_seen_edge_unearned_clamps: set = set()


# ─── Node Queries ────────────────────────────────────────────────────────────

def get_nodes(
    account_id: int,
    node_type: Optional[str] = None,
    node_subtype: Optional[str] = None,
    tier: Optional[int] = None,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> List[ContextNode]:
    """
    Get context nodes for an account with optional filters.

    Args:
        account_id: Required — tenant isolation
        node_type: SIGNAL, STAKEHOLDER, DECISION, OUTCOME, EXTERNAL_CONTEXT
        node_subtype: e.g. kpi_change, ticket, champion_loss
        tier: 1=permanent, 2=decaying, 3=ephemeral
        since: Only nodes with occurred_at >= since
        limit: Max results (default 100)
    """
    q = ContextNode.query.filter_by(account_id=account_id)

    if node_type:
        q = q.filter_by(node_type=node_type)
    if node_subtype:
        q = q.filter_by(node_subtype=node_subtype)
    if tier is not None:
        q = q.filter_by(tier=tier)
    if since:
        q = q.filter(ContextNode.occurred_at >= since)

    # Exclude expired T2/T3 nodes
    now = datetime.utcnow()
    q = q.filter(
        db.or_(
            ContextNode.expires_at.is_(None),
            ContextNode.expires_at > now,
        )
    )

    return q.order_by(ContextNode.occurred_at.desc()).limit(limit).all()


def get_node_by_source(account_id: int, source_platform: str, source_event_id: str) -> Optional[ContextNode]:
    """Find a node by its external source ID (dedup check)."""
    return ContextNode.query.filter_by(
        account_id=account_id,
        source_platform=source_platform,
        source_event_id=source_event_id,
    ).first()


# ─── Edge Queries ────────────────────────────────────────────────────────────

def get_edges(
    node_id: int,
    direction: str = 'both',
    edge_type: Optional[str] = None,
) -> List[ContextEdge]:
    """
    Get edges connected to a node.

    Args:
        node_id: The node to query from
        direction: 'outgoing', 'incoming', or 'both'
        edge_type: Filter by edge type (CAUSED_BY, LED_TO, etc.)
    """
    filters_out = [ContextEdge.from_node_id == node_id]
    filters_in = [ContextEdge.to_node_id == node_id]

    if edge_type:
        filters_out.append(ContextEdge.edge_type == edge_type)
        filters_in.append(ContextEdge.edge_type == edge_type)

    if direction == 'outgoing':
        return ContextEdge.query.filter(*filters_out).all()
    elif direction == 'incoming':
        return ContextEdge.query.filter(*filters_in).all()
    else:
        outgoing = ContextEdge.query.filter(*filters_out).all()
        incoming = ContextEdge.query.filter(*filters_in).all()
        return outgoing + incoming


def get_pillars_with_observed_evidence(
    customer_id: int,
    account_ids: Optional[List[int]] = None,
) -> set:
    """Which pillar_codes a customer's context graph actually backs with
    real (OBSERVED-tier) evidence.

    Read-side counterpart to utils/edge_factory.py (write-only today, see
    its module docstring) and item 25's Po1 re-tiering fix in
    executive_dashboard_api.py's `_get_po1_benchmark_metrics` — that code
    needs to know, per pillar, whether a customer has grounded evidence
    before promoting a Power-of-1 metric's provenance tier above BENCHMARK.

    Nodes/edges opt into this by tagging themselves with which pillar they
    evidence via `properties['pillar_code']`, using the same P1..Pn
    vocabulary as PillarScore / KPIScore / outcome_roi_api's
    pillar_metric_map — this is NOT a second pillar taxonomy, just a
    reference to the one that already exists. A row with no pillar_code
    tag never counts for any pillar (fail closed).

    Evidence-tier resolution, per utils.provenance's observed/inferred/
    synthetic vocabulary (see that module's docstring for how this axis
    composes with utils.value_provenance's display tiers):
      - ContextNode: `properties['evidence_tier']` if explicitly stamped,
        else `provenance.normalize(node.source)` — covers the 'observed'
        literal written by real CSV-ingest paths (e.g.
        mcp_server/cs_pulse_onboarding.py) as well as the legacy
        'customer'/'system' values.
      - ContextEdge: `properties['evidence_tier']` if stamped (every edge
        written via edge_factory.create_inferred_edge() carries one).
        Edges have no `source` column to fall back to, and per
        provenance.normalize's fail-closed rule a missing value must never
        be read as the most-trusted one — so an untagged edge is simply
        not counted, regardless of source_platform.

    Args:
        customer_id: required — returns an empty set if falsy.
        account_ids: optional scope to a subset of the customer's
            accounts; omit to consider every account.

    Returns:
        set of pillar_code strings, e.g. {'P1', 'P4'}. Empty if
        customer_id is falsy or nothing qualifies.
    """
    from utils.provenance import OBSERVED, normalize as _normalize_source

    if not customer_id:
        return set()

    pillars: set = set()

    node_q = ContextNode.query.filter(ContextNode.customer_id == customer_id)
    if account_ids:
        node_q = node_q.filter(ContextNode.account_id.in_(account_ids))
    for node in node_q.all():
        props = node.properties or {}
        pillar_code = props.get('pillar_code')
        if not pillar_code:
            continue
        tier = props.get('evidence_tier') or _normalize_source(node.source)
        if tier == OBSERVED:
            pillars.add(pillar_code)

    edge_q = db.session.query(ContextEdge).join(
        ContextNode, ContextEdge.from_node_id == ContextNode.node_id,
    ).filter(ContextEdge.customer_id == customer_id)
    if account_ids:
        edge_q = edge_q.filter(ContextNode.account_id.in_(account_ids))
    for edge in edge_q.all():
        props = edge.properties or {}
        pillar_code = props.get('pillar_code')
        if not pillar_code:
            continue
        # No fallback for edges: an untagged evidence_tier must fail
        # closed, not be assumed trustworthy (see docstring above).
        if props.get('evidence_tier') == OBSERVED:
            pillars.add(pillar_code)

    return pillars


def get_causal_chain(
    node_id: int,
    direction: str = 'upstream',
    max_depth: int = 5,
    customer_id: int = None,
) -> List[Dict[str, Any]]:
    """
    Walk causal edges to build a chain of connected nodes.

    Args:
        node_id: Starting node
        direction: 'upstream' (find what caused this node) or 'downstream' (find effects)
        max_depth: Stop after N hops
        customer_id: If set, restrict traversal to this tenant (prevents
                     cross-tenant edge leakage via shared nodes).
    Returns:
        List of {node, edge, depth} dicts in traversal order
    """
    # Causal edge types — any of these indicate cause/effect relationships
    CAUSAL_EDGE_TYPES = {'CAUSED_BY', 'LED_TO', 'TRIGGERED', 'INDICATES', 'RESULTED_IN'}

    visited = set()
    chain = []

    current_ids = [node_id]
    for depth in range(1, max_depth + 1):
        next_ids = []
        for nid in current_ids:
            if nid in visited:
                continue
            visited.add(nid)

            if direction == 'upstream':
                # Find edges pointing INTO this node (from_node → nid)
                # The from_node is the upstream cause
                filters = [
                    ContextEdge.to_node_id == nid,
                    ContextEdge.edge_type.in_(CAUSAL_EDGE_TYPES),
                    # WS-2 2g — drop superseded edges (F2): without this, a
                    # duplicate/contradictory edge on the same triple
                    # (e.g. a stale wizard_a inference a later observed
                    # edge superseded) returns its node a second time and
                    # any consumer aggregating over the chain double-counts.
                    ContextEdge.superseded_by.is_(None),
                ]
                if customer_id is not None:
                    filters.append(ContextEdge.customer_id == customer_id)
                edges = ContextEdge.query.filter(*filters).all()
                for edge in edges:
                    neighbor_id = edge.from_node_id
                    if neighbor_id not in visited:
                        neighbor = db.session.get(ContextNode, neighbor_id)
                        if neighbor:
                            chain.append({
                                'node': neighbor.to_dict(),
                                'edge': edge.to_dict(),
                                'depth': depth,
                            })
                            next_ids.append(neighbor_id)
            else:
                # Find edges going OUT from this node (nid → to_node)
                # The to_node is the downstream effect
                filters = [
                    ContextEdge.from_node_id == nid,
                    ContextEdge.edge_type.in_(CAUSAL_EDGE_TYPES),
                    # WS-2 2g — see the mirrored comment in the 'upstream'
                    # branch above.
                    ContextEdge.superseded_by.is_(None),
                ]
                if customer_id is not None:
                    filters.append(ContextEdge.customer_id == customer_id)
                edges = ContextEdge.query.filter(*filters).all()
                for edge in edges:
                    neighbor_id = edge.to_node_id
                    if neighbor_id not in visited:
                        neighbor = db.session.get(ContextNode, neighbor_id)
                        if neighbor:
                            chain.append({
                                'node': neighbor.to_dict(),
                                'edge': edge.to_dict(),
                                'depth': depth,
                            })
                            next_ids.append(neighbor_id)

        current_ids = next_ids
        if not current_ids:
            break

    return chain


# ─── 2-Hop Traversal ────────────────────────────────────────────────────────

def traverse_2hop(
    account_id: int,
    center_node_id: int,
    edge_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    2-hop ego graph centered on a node. Returns all nodes and edges
    within 2 hops, useful for "impact neighborhood" visualization.

    Returns:
        {
            'center': node_dict,
            'nodes': [node_dicts...],
            'edges': [edge_dicts...],
            'revenue_at_risk': total $ across all nodes
        }
    """
    center = db.session.get(ContextNode, center_node_id)
    if not center or center.account_id != account_id:
        return {'center': None, 'nodes': [], 'edges': [], 'revenue_at_risk': 0}

    visited_nodes = {center_node_id}
    collected_edges = []
    hop1_ids = set()

    # Hop 1
    edges_1 = _get_all_edges_for_node(center_node_id, edge_types)
    for e in edges_1:
        collected_edges.append(e)
        neighbor = e.to_node_id if e.from_node_id == center_node_id else e.from_node_id
        hop1_ids.add(neighbor)
        visited_nodes.add(neighbor)

    # Hop 2
    for nid in hop1_ids:
        edges_2 = _get_all_edges_for_node(nid, edge_types)
        for e in edges_2:
            neighbor = e.to_node_id if e.from_node_id == nid else e.from_node_id
            if neighbor not in visited_nodes:
                visited_nodes.add(neighbor)
            collected_edges.append(e)

    # Deduplicate edges
    seen_edge_ids = set()
    unique_edges = []
    for e in collected_edges:
        if e.edge_id not in seen_edge_ids:
            seen_edge_ids.add(e.edge_id)
            unique_edges.append(e)

    # Load all nodes
    all_nodes = ContextNode.query.filter(
        ContextNode.node_id.in_(visited_nodes),
        ContextNode.account_id == account_id,
    ).all()

    # Revenue at risk: derived from health-score × ARR (consistent with
    # get_revenue_at_risk — signal nodes no longer carry revenue_impact).
    total_revenue = _calculate_at_risk_from_health(account_id)

    return {
        'center': center.to_dict(),
        'nodes': [n.to_dict() for n in all_nodes],
        'edges': [e.to_dict() for e in unique_edges],
        'revenue_at_risk': total_revenue,
    }


def _get_all_edges_for_node(node_id: int, edge_types: Optional[List[str]] = None) -> List[ContextEdge]:
    """Get all edges (both directions) for a node, optionally filtered."""
    q_out = ContextEdge.query.filter_by(from_node_id=node_id)
    q_in = ContextEdge.query.filter_by(to_node_id=node_id)

    if edge_types:
        q_out = q_out.filter(ContextEdge.edge_type.in_(edge_types))
        q_in = q_in.filter(ContextEdge.edge_type.in_(edge_types))

    return q_out.all() + q_in.all()


# ─── Revenue / ROI Aggregations ─────────────────────────────────────────────

def _dedupe_exact_outcome_nodes(nodes):
    """Collapse byte-identical duplicate OUTCOME nodes to one.

    Distinct from _deduplicate_outcome_amounts (a fuzzy same-economic-event
    AMOUNT collapse used per-account). This removes LITERAL duplicate nodes —
    same account, title, revenue_impact, type, and occurred_at but different
    node_ids — which a re-ingested CSV produces (found 2026-08-24: customer
    391's outcomes were ingested 4×, 63 excess rows, inflating the
    non-deduping CFO rollup's revenue_at_risk to $39.84M = 111% of its $35.9M
    ARR — an impossible number). Keeps the lowest node_id of each group.

    The account-level get_revenue_at_risk masked this via its amount-dedup;
    the cross-account rollups summed raw. Applying this in both rollups makes
    a literal duplicate impossible to double-count regardless of the ingest
    bug, which is the forward fix; the 391 residue is cleaned separately.
    """
    seen = {}
    for n in nodes:
        key = (n.account_id, n.title, str(n.revenue_impact),
               n.revenue_impact_type, n.occurred_at)
        prev = seen.get(key)
        if prev is None or n.node_id < prev.node_id:
            seen[key] = n
    return list(seen.values())


def _deduplicate_outcome_amounts(amounts: List[float], threshold: float = 0.20) -> float:
    """
    Cluster outcome amounts that are within *threshold* of each other —
    these likely represent the same economic event counted from different
    perspectives (e.g. "Expansion Deal Closed" vs "ARR Growth" vs "ROI").

    Returns the sum of the **largest** amount from each cluster.
    """
    if not amounts:
        return 0.0

    sorted_amounts = sorted(amounts, reverse=True)
    used: set = set()
    total = 0.0

    for i, a in enumerate(sorted_amounts):
        if i in used:
            continue
        used.add(i)
        # Absorb any later amounts that are within threshold of *a*
        for j in range(i + 1, len(sorted_amounts)):
            if j in used:
                continue
            b = sorted_amounts[j]
            if a > 0 and abs(a - b) / a < threshold:
                used.add(j)                 # same event — skip the smaller
        total += a                          # keep the largest per cluster

    return total


def churn_pct_for_health(health: float) -> float:
    """Sanctioned health→churn-probability band (single source of the model).

    Churn-probability bands (aligned with health thresholds):
      - Critical (health < 50):  40 % of ARR at risk
      - At-risk  (50 ≤ h < 70): 20 % of ARR at risk
      - Healthy  (h ≥ 70):       5 % of ARR at risk

    Kept as one function so the 40/20/5 bands have exactly one definition —
    both ``_calculate_at_risk_from_health`` (revenue-at-risk) and the CFO
    per-account impact allocation read from here, never a second copy.
    """
    if health < 50:
        return 0.40
    elif health < 70:
        return 0.20
    return 0.05


def _calculate_at_risk_from_health(account_id: int) -> float:
    """
    Derive at-risk revenue from the account's health score and ARR.

    Churn-probability bands (aligned with health thresholds):
      - Critical (health < 50):  40 % of ARR at risk
      - At-risk  (50 ≤ h < 70): 20 % of ARR at risk
      - Healthy  (h ≥ 70):       5 % of ARR at risk

    Health score is read from the ``health_scores`` table (latest
    measurement month).  ARR comes from ``accounts.revenue`` or
    ``accounts.profile_metadata.arr``.

    This replaces the old approach of summing negative SIGNAL
    revenue_impact values, which produced cumulative / stale
    estimates that were never real independent revenue events.
    """
    from models import Account, HealthScore

    account = Account.query.filter_by(account_id=account_id).first()
    if not account:
        return 0.0

    # ── ARR ──
    arr = 0.0
    if account.revenue:
        arr = float(account.revenue)
    elif account.profile_metadata and isinstance(account.profile_metadata, dict):
        arr = float(account.profile_metadata.get('arr', 0) or 0)

    # ── Latest health score ──
    latest_hs = (
        HealthScore.query
        .filter_by(account_id=account_id)
        .order_by(HealthScore.measurement_month.desc())
        .first()
    )
    health = float(latest_hs.health_score) if latest_hs and latest_hs.health_score else 50.0

    return round(arr * churn_pct_for_health(health), 2)


def get_revenue_at_risk(account_id: int) -> Dict[str, Any]:
    """
    Aggregate revenue impact across all active nodes for an account.
    This is the primary CRO/CFO metric.

    Revenue-counting rules (item 26 Model C, 2026-08-24 — buckets via the
    canonical _outcome_revenue_bucket_and_amount, shared with the
    cross-account rollup so the two never disagree):
      - **at_risk**:   NODE-EVIDENCED risk-trajectory OUTCOMEs (revenue_at_risk,
                        renewal_uncertainty, capacity_constraint, ...). Matches
                        the CFO summary's revenue_at_risk. (Was the health
                        heuristic — that is now modeled_churn_exposure.)
      - **modeled_churn_exposure**: health-score × ARR band (40/20/5%). A
                        modeled estimate, kept separate and clearly labeled;
                        never summed into at_risk.
      - **protected**: OUTCOME nodes classified protected.
      - **expansion**: OUTCOME nodes classified expansion.
      - **lost**:      REALIZED losses only (churn_lost, contraction, ...).
                        ~$0 until a tenant actually churns.
      - DECISION nodes are *excluded*.

    Outcome de-duplication:
      Within each bucket, outcome amounts within 20% are treated as the same
      economic event; only the largest is kept.

    Returns dict with: at_risk, modeled_churn_exposure, protected, expansion,
    lost, net_impact, node_count (has-signal gate), outcome_node_count.
    """
    now = datetime.utcnow()

    # ── modeled_churn_exposure: health-band heuristic (40/20/5% of ARR) ──
    # Item 26 Model C (2026-08-24): this was returned as `at_risk`, which
    # conflicted with the CFO summary's node-evidenced `revenue_at_risk` —
    # two screens, contradictory numbers under one label. It is now a
    # separate, clearly-labeled modeled field; account-level `at_risk` below
    # is the node-evidenced figure, matching the CFO. (Named to align with
    # the existing cost_of_inaction.annual_churn_exposure vocabulary, not a
    # third `*_at_risk` — reviewer caveat 1.)
    modeled_churn_exposure = _calculate_at_risk_from_health(account_id)

    # ── at_risk / protected / expansion / lost: OUTCOME nodes only ──
    # I3' filter: exclude unearned nodes (confidence < 0.5). Nodes that were
    # written without evidence got clamped to 0.3 by the pre-commit hook; we
    # don't want those contributing to CFO/CRO revenue claims. Nodes with
    # NULL confidence are treated as earned (legacy data, pre-clamp) —
    # the clamp only fires prospectively.
    outcome_nodes = ContextNode.query.filter(
        ContextNode.account_id == account_id,
        ContextNode.node_type == 'OUTCOME',
        ContextNode.revenue_impact.isnot(None),
        db.or_(
            ContextNode.expires_at.is_(None),
            ContextNode.expires_at > now,
        ),
        db.or_(
            ContextNode.confidence.is_(None),
            ContextNode.confidence >= 0.5,
        ),
    ).all()

    # Bucket via the SAME canonical classifier the cross-account rollup uses
    # (_outcome_revenue_bucket_and_amount), so the two paths can never assign
    # a node to different buckets (item 26 Model C). Amount is still
    # confidence-weighted here — the account-level path's existing policy;
    # on current data all risk nodes carry confidence 1.0, so this account
    # at_risk equals the CFO revenue_at_risk to the dollar.
    outcome_buckets: Dict[str, List[float]] = {
        'at_risk': [], 'protected': [], 'expansion': [], 'lost': [],
    }
    for n in outcome_nodes:
        bucket, _unweighted = _outcome_revenue_bucket_and_amount(n)
        if bucket not in outcome_buckets:
            continue
        weighted = abs(float(n.revenue_impact) * float(n.confidence or 1.0))
        outcome_buckets[bucket].append(weighted)

    # De-duplicate within each bucket (same economic event → keep largest)
    at_risk    = _deduplicate_outcome_amounts(outcome_buckets['at_risk'])
    protected  = _deduplicate_outcome_amounts(outcome_buckets['protected'])
    expansion  = _deduplicate_outcome_amounts(outcome_buckets['expansion'])
    lost       = _deduplicate_outcome_amounts(outcome_buckets['lost'])

    # node_count is NOT a pure node count — the +1 marks the health-derived
    # at_risk contribution (which comes from _calculate_at_risk_from_health,
    # not a node). It is LOAD-BEARING: three call sites gate on
    # `node_count > 0` to mean "does this account have any revenue signal"
    # (outcome_roi_api.py, portfolio_api.py, outcome_roi_engine.py), and a
    # freshly-onboarded account has health-at_risk but zero OUTCOME nodes —
    # dropping the +1 would flip those gates for the new-account case. So it
    # stays. The reviewer's "7 vs 5" complaint (2026-08-24 item 27) is that
    # the *name* implies a node count; fixed by exposing the honest deduped
    # node count separately as outcome_node_count, which the UI renders.
    outcome_node_count = len(outcome_nodes)
    # The +1 now marks the modeled_churn_exposure contribution (renamed from
    # at_risk under Model C) — a new-account with health exposure but zero
    # OUTCOME nodes still trips the `node_count > 0` has-revenue-signal gates.
    node_count = outcome_node_count + (1 if modeled_churn_exposure > 0 else 0)

    return {
        # at_risk is now NODE-EVIDENCED (Model C) — matches the CFO summary's
        # revenue_at_risk, resolving the same-label-different-number split.
        'at_risk':    round(at_risk, 2),
        # modeled_churn_exposure: the health-band heuristic (was `at_risk`).
        # Kept for callers/UI that want the modeled figure; never conflated
        # with the node-evidenced at_risk above.
        'modeled_churn_exposure': round(modeled_churn_exposure, 2),
        'protected':  round(protected, 2),
        'expansion':  round(expansion, 2),
        # lost is REALIZED loss only now — ~$0 until a tenant actually churns.
        'lost':       round(lost, 2),
        'net_impact': round(protected + expansion - lost, 2),
        # Has-revenue-signal indicator (nodes + modeled-exposure marker) —
        # keep for the `> 0` gates. Not a node count; see outcome_node_count.
        'node_count': node_count,
        # Honest count of contributing OUTCOME nodes (deduped). Render this.
        'outcome_node_count': outcome_node_count,
    }


def aggregate_revenue_across_accounts(
    customer_id: int,
    account_ids: List[int],
) -> Dict[str, Any]:
    """
    Aggregate revenue metrics across multiple accounts from context graph OUTCOME nodes.

    This is the multi-account counterpart of get_revenue_at_risk() (which is per-account).
    Used by CRO, CFO, and CEO dashboards.  Imported from utils/context_graph to avoid
    duplication across executive_dashboard_api and outcome_roi_api.

    Returns:
        {
            'revenue_at_risk': float,
            'revenue_protected': float,
            'expansion_pipeline': float,
            'node_count': int,
        }
    """
    empty = {
        'revenue_at_risk': 0, 'revenue_protected': 0,
        'expansion_pipeline': 0, 'node_count': 0,
    }
    if not account_ids:
        return empty

    outcome_nodes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.account_id.in_(account_ids),
            ContextNode.node_type == 'OUTCOME',
            ContextNode.revenue_impact.isnot(None),
        )
        .all()
    )

    # Exact-duplicate collapse before summing — this rollup used to sum raw
    # (unlike the account-level path), so re-ingested duplicate nodes
    # inflated the total past ARR. See _dedupe_exact_outcome_nodes.
    outcome_nodes = _dedupe_exact_outcome_nodes(outcome_nodes)

    at_risk = 0.0
    protected = 0.0
    expansion = 0.0
    lost = 0.0

    for node in outcome_nodes:
        bucket, amount = _outcome_revenue_bucket_and_amount(node)
        if bucket == 'at_risk':
            at_risk += amount
        elif bucket == 'lost':
            lost += amount
        elif bucket == 'expansion':
            expansion += amount
        elif bucket == 'protected':
            protected += amount

    return {
        'revenue_at_risk': round(at_risk, 2),
        'revenue_protected': round(protected, 2),
        'expansion_pipeline': round(expansion, 2),
        # revenue_lost: REALIZED losses only (item 26 Model C). Additive —
        # existing consumers that don't read it are unaffected; ~$0 until a
        # tenant actually churns. revenue_at_risk now excludes realized-loss
        # subtypes, but those have no live nodes, so this total is unchanged
        # on current data.
        'revenue_lost': round(lost, 2),
        # Here node_count IS the deduped OUTCOME node count (this rollup has
        # no health-derived at_risk marker). outcome_node_count mirrors it so
        # both revenue APIs expose the same honest field name.
        'node_count': len(outcome_nodes),
        'outcome_node_count': len(outcome_nodes),
    }


# Canonical OUTCOME subtype → bucket classification, shared by BOTH the
# account-level get_revenue_at_risk and the cross-account rollups so the two
# surfaces can never disagree on which bucket a node lands in (item 26,
# Model C, signed off 2026-08-24).
#
# at_risk vs lost is the semantic split the two paths used to disagree on:
#   at_risk = risk TRAJECTORY — money in jeopardy, not yet realized. Its
#             subtype names say so (revenue_at_risk, *_uncertainty, *_decline).
#   lost    = REALIZED loss — the account actually churned/contracted.
# On today's data _OUTCOME_LOST_TYPES has zero live nodes (demo tenants are
# mid-story; nothing has churned), so lost reads ~$0 — correct, not a bug.
_OUTCOME_AT_RISK_TYPES = {
    'at_risk', 'revenue_at_risk', 'churn_risk', 'engagement_decline',
    'renewal_uncertainty', 'capacity_constraint', 'partner_friction',
    'partial_recovery',  # decision 2: recovery-in-progress is risk, not loss
}
_OUTCOME_LOST_TYPES = {
    'lost', 'churn_lost', 'contraction', 'revenue_lost',
}
# Back-compat alias — some callers still reference the old union.
_OUTCOME_RISK_TYPES = _OUTCOME_AT_RISK_TYPES | _OUTCOME_LOST_TYPES
_OUTCOME_PROTECTED_TYPES = {
    'protected', 'revenue_protected', 'churn_averted', 'renewal_secured',
    'revenue_saved', 'engagement_recovery', 'escalation_resolved', 'intervention_outcome',
}
_OUTCOME_EXPANSION_TYPES = {
    'expansion', 'expansion_closed', 'expansion_opportunity', 'expansion_approved',
    'expansion_realized', 'revenue_growth', 'upsell', 'cross_sell',
}


def _outcome_revenue_bucket_and_amount(node: ContextNode):
    """Return (bucket, abs_amount) or (None, 0) for an OUTCOME node with revenue_impact."""
    try:
        raw = float(node.revenue_impact)
    except (TypeError, ValueError):
        return None, 0.0

    impact_type = (node.revenue_impact_type or '').lower()
    subtype = (node.node_subtype or '').lower()

    if impact_type in _OUTCOME_AT_RISK_TYPES or subtype in _OUTCOME_AT_RISK_TYPES:
        return 'at_risk', abs(raw)
    if impact_type in _OUTCOME_LOST_TYPES or subtype in _OUTCOME_LOST_TYPES:
        return 'lost', abs(raw)
    if impact_type in _OUTCOME_EXPANSION_TYPES or subtype in _OUTCOME_EXPANSION_TYPES:
        return 'expansion', abs(raw)
    if impact_type in _OUTCOME_PROTECTED_TYPES or subtype in _OUTCOME_PROTECTED_TYPES:
        return 'protected', abs(raw)
    # Unknown sign fallback: a negative of unknown type is risk (not yet
    # realized), not confirmed loss — conservative for a "lost" claim.
    if raw < 0:
        return 'at_risk', abs(raw)
    if raw > 0:
        return 'protected', raw
    return None, 0.0


def _outcome_node_trace(node: ContextNode) -> Dict[str, Any]:
    return {
        'node_id': node.node_id,
        'account_id': node.account_id,
        'node_subtype': node.node_subtype,
        'revenue_impact': float(node.revenue_impact) if node.revenue_impact is not None else None,
        'revenue_impact_type': node.revenue_impact_type,
        'title': node.title,
        'occurred_at': node.occurred_at.isoformat() if node.occurred_at else None,
    }


def aggregate_revenue_with_provenance(
    customer_id: int,
    account_ids: List[int],
    sample_per_bucket: int = 8,
) -> Dict[str, Any]:
    """
    Same totals as aggregate_revenue_across_accounts, plus trace samples for UI / audit.
    """
    base = aggregate_revenue_across_accounts(customer_id, account_ids)
    empty_prov = {
        'source': 'context_graph',
        'engine': 'aggregate_revenue_across_accounts',
        'outcome_node_count': 0,
        'revenue_at_risk': {'sample_nodes': []},
        'revenue_protected': {'sample_nodes': []},
        'expansion_pipeline': {'sample_nodes': []},
    }
    if not account_ids:
        return {**base, 'provenance': empty_prov}

    outcome_nodes = (
        ContextNode.query
        .filter(
            ContextNode.customer_id == customer_id,
            ContextNode.account_id.in_(account_ids),
            ContextNode.node_type == 'OUTCOME',
            ContextNode.revenue_impact.isnot(None),
        )
        .all()
    )

    # Same exact-duplicate collapse the base totals use, so the trace
    # samples don't show 4 identical rows for one economic event.
    outcome_nodes = _dedupe_exact_outcome_nodes(outcome_nodes)

    samples = {'at_risk': [], 'protected': [], 'expansion': []}
    for node in outcome_nodes:
        bucket, amount = _outcome_revenue_bucket_and_amount(node)
        if not bucket or amount <= 0:
            continue
        key = bucket if bucket != 'expansion' else 'expansion'
        if bucket == 'at_risk' and len(samples['at_risk']) < sample_per_bucket:
            samples['at_risk'].append(_outcome_node_trace(node))
        elif bucket == 'protected' and len(samples['protected']) < sample_per_bucket:
            samples['protected'].append(_outcome_node_trace(node))
        elif bucket == 'expansion' and len(samples['expansion']) < sample_per_bucket:
            samples['expansion'].append(_outcome_node_trace(node))

    provenance = {
        'source': 'context_graph',
        'engine': 'aggregate_revenue_across_accounts',
        'outcome_node_count': len(outcome_nodes),
        'revenue_at_risk': {
            'value': base['revenue_at_risk'],
            'label': 'Risk Exposure (Context Graph)',
            'sample_nodes': samples['at_risk'],
        },
        'revenue_protected': {
            'value': base['revenue_protected'],
            'label': 'Customer-Reported Saves (Unverified)',
            'sample_nodes': samples['protected'],
        },
        'expansion_pipeline': {
            'value': base['expansion_pipeline'],
            'label': 'Expansion (Context Graph OUTCOME nodes)',
            'sample_nodes': samples['expansion'],
        },
    }
    return {**base, 'provenance': provenance}


def get_account_graph_summary(account_id: int, include_narrative: bool = False) -> Dict[str, Any]:
    """
    Quick summary of graph density for an account.
    Useful for dashboard widgets and health overview.

    Mirrors the narrative filter in get_context_graph_mermaid: by default
    excludes narrative-only OUTCOMEs (no revenue_impact, no source_ref, no
    properties.evidence) so both tools report the same counts on the same
    account. Set include_narrative=True to see the unfiltered count.
    """
    now = datetime.utcnow()
    base_filter = db.and_(
        ContextNode.account_id == account_id,
        db.or_(
            ContextNode.expires_at.is_(None),
            ContextNode.expires_at > now,
        )
    )

    # Narrative filter: narrative-only OUTCOMEs are those with no
    # revenue_impact, no source_ref, and no properties.evidence. Applied via
    # SQL NOT predicate so the count is correct at the DB layer (not
    # post-filtered in Python). Uses postgres `->>` operator to extract
    # evidence as text regardless of whether `properties` is JSON or JSONB.
    if not include_narrative:
        _evidence_text = ContextNode.properties.op('->>')('evidence')
        narrative_only = db.and_(
            ContextNode.node_type == 'OUTCOME',
            ContextNode.revenue_impact.is_(None),
            db.or_(
                ContextNode.source_ref.is_(None),
                ContextNode.source_ref == '',
            ),
            db.or_(
                _evidence_text.is_(None),
                _evidence_text == '',
            ),
        )
        base_filter = db.and_(base_filter, db.not_(narrative_only))

    node_counts = db.session.query(
        ContextNode.node_type,
        db.func.count(ContextNode.node_id)
    ).filter(base_filter).group_by(ContextNode.node_type).all()

    total_nodes = sum(c for _, c in node_counts)

    # Count edges — only those connecting two VISIBLE nodes (both endpoints
    # must pass the filter). Mirrors how mermaid renders.
    # Additionally exclude causal edges that violate temporal ordering
    # (cause.occurred_at > effect.occurred_at) — Wizard A's arc-detection
    # signal fires at customer-creation and back-emits LED_TO edges to
    # pre-existing outcomes. Mechanically backward; also filtered by mermaid.
    visible_node_ids_subq = db.session.query(ContextNode.node_id).filter(base_filter).subquery()

    from_alias = db.aliased(ContextNode)
    to_alias = db.aliased(ContextNode)
    _causal_types = ['CAUSED_BY', 'LED_TO', 'TRIGGERED', 'RESULTED_IN', 'INDICATES']

    edge_count = (
        db.session.query(ContextEdge.edge_id)
        .join(from_alias, ContextEdge.from_node_id == from_alias.node_id)
        .join(to_alias, ContextEdge.to_node_id == to_alias.node_id)
        .filter(
            ContextEdge.from_node_id.in_(visible_node_ids_subq),
            ContextEdge.to_node_id.in_(visible_node_ids_subq),
        )
        .filter(db.not_(db.and_(
            ContextEdge.edge_type.in_(_causal_types),
            from_alias.occurred_at > to_alias.occurred_at,
        )))
        .count()
    )

    revenue = get_revenue_at_risk(account_id)

    return {
        'account_id': account_id,
        'total_nodes': total_nodes,
        'total_edges': edge_count,
        'nodes_by_type': {nt: c for nt, c in node_counts},
        'revenue': revenue,
    }


# ─── Write Helpers ───────────────────────────────────────────────────────────

def upsert_node(
    customer_id: int,
    account_id: int,
    node_type: str,
    title: str,
    occurred_at: datetime,
    properties: Dict[str, Any],
    source_platform: str = 'csv_import',
    source_event_id: Optional[str] = None,
    **kwargs,
) -> ContextNode:
    """
    Insert or update a context node. Deduplicates by source_platform + source_event_id.

    Pre-commit validation:
      I4  — confidence and properties.confidence clamped to [0, 1]
      I3' — OUTCOME without evidence (properties.evidence OR source_ref)
            gets confidence capped at 0.3 and tier forced to 2
    """
    # I4 pre-commit clamp (top-level + JSONB). Clamps only; doesn't reject.
    try:
        from utils.context_graph_invariants import (
            clamp_confidence,
            sanitize_properties_confidence,
        )
        if 'confidence' in kwargs:
            kwargs['confidence'] = clamp_confidence(kwargs.get('confidence'))
        properties = sanitize_properties_confidence(properties)
    except Exception:
        pass  # invariants module optional — fall through

    # I3' pre-commit clamp (OUTCOME without evidence → unearned).
    try:
        from utils.context_graph_invariants import clamp_unearned_confidence
        new_conf, new_props, new_tier, was_clamped = clamp_unearned_confidence(
            node_type=node_type,
            source_platform=source_platform,
            source_ref=kwargs.get('source_ref'),
            confidence=kwargs.get('confidence'),
            properties=properties,
            tier=kwargs.get('tier'),
        )
        if was_clamped:
            kwargs['confidence'] = new_conf
            kwargs['tier'] = new_tier
            properties = new_props
            # Structured WARN on first occurrence per (source_platform, subtype)
            # so ops tooling can alert on "unearned OUTCOMEs in the wild".
            _unearned_key = (source_platform, kwargs.get('node_subtype'), kwargs.get('revenue_impact_type'))
            if _unearned_key not in _seen_unearned_clamps:
                _seen_unearned_clamps.add(_unearned_key)
                _unearned_clamp_logger.warning(
                    'event=unearned_confidence_clamp first_seen=true '
                    'node_type=OUTCOME source_platform=%s subtype=%s '
                    'revenue_impact_type=%s customer_id=%s account_id=%s '
                    'clamped_to=%s tier_forced=%s',
                    source_platform, kwargs.get('node_subtype'),
                    kwargs.get('revenue_impact_type'),
                    customer_id, account_id, new_conf, new_tier,
                )
            else:
                logger.debug(
                    '[invariants] I3\' unearned-confidence clamp: OUTCOME %s '
                    'confidence→%s tier→%s',
                    kwargs.get('node_subtype'), new_conf, new_tier,
                )
    except Exception as _e:
        logger.debug(f'[invariants] unearned-clamp pass-through (error): {_e}')

    existing = None
    if source_event_id:
        existing = get_node_by_source(account_id, source_platform, source_event_id)

    if existing:
        existing.title = title
        existing.properties = properties
        existing.occurred_at = occurred_at
        for k, v in kwargs.items():
            if hasattr(existing, k):
                setattr(existing, k, v)
        db.session.flush()
        return existing

    node = ContextNode(
        customer_id=customer_id,
        account_id=account_id,
        node_type=node_type,
        title=title,
        occurred_at=occurred_at,
        properties=properties,
        source_platform=source_platform,
        source_event_id=source_event_id,
        **kwargs,
    )
    db.session.add(node)
    db.session.flush()
    return node


def add_edge(
    from_node_id: int,
    to_node_id: int,
    edge_type: str,
    weight: float = 1.0,
    confidence: float = 1.0,
    source_platform: str = 'csv_import',
    created_by: str = 'csv_import',
    customer_id: Optional[int] = None,
    **kwargs,
) -> ContextEdge:
    """
    Add an edge between two nodes. Does NOT deduplicate — caller should
    check for existing edges if needed.
    """
    edge = ContextEdge(
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        edge_type=edge_type,
        weight=weight,
        confidence=confidence,
        source_platform=source_platform,
        created_by=created_by,
        customer_id=customer_id,
        **kwargs,
    )
    db.session.add(edge)
    db.session.flush()
    return edge


def upsert_edge(
    from_node_id: int,
    to_node_id: int,
    edge_type: str,
    weight: float = 1.0,
    confidence: float = 1.0,
    source_platform: str = 'csv_import',
    created_by: str = 'csv_import',
    customer_id: Optional[int] = None,
    **kwargs,
) -> tuple:
    """
    Insert or update an edge. Deduplicates by (from_node_id, to_node_id, edge_type, source_platform).

    Pre-commit validation (Apr 2026):
      I1 — OUTCOME→OUTCOME causal edges are REJECTED (returns (None, False))
      I2 — polarity-mismatched signal→outcome edges are REJECTED
      I4 — confidence is clamped to [0, 1]

    Rejection logs a WARN with the offending subtypes so the producer
    (wizard_a, auto_linker, llm_enrichment, playbook_execution) can be
    identified from logs. Callers that iterate and check `created` will
    naturally skip rejected edges (created=False + edge=None).

    Returns:
        (ContextEdge, created: bool) — True if new, False if updated OR rejected.
        When rejected, the first element is None.
    """
    # I4 clamp. clamp_confidence(None) returns None unchanged — an explicit
    # confidence=None (WS-2 2c's EdgeFactory: an inferred edge with no
    # calibrated point estimate) must stay NULL, not get promoted to a
    # fabricated 1.0. "Not provided" is already covered by this function's
    # own confidence=1.0 default parameter, so there is no ambiguity to
    # resolve here — a caller who writes confidence=None means it.
    try:
        from utils.context_graph_invariants import clamp_confidence
        confidence = clamp_confidence(confidence)
    except Exception:
        pass

    # I1 / I2 pre-commit gate: look up node types to validate polarity/direction.
    try:
        from utils.context_graph_invariants import validate_edge_pre_commit
        from_node = db.session.get(ContextNode, from_node_id)
        to_node = db.session.get(ContextNode, to_node_id)
        if from_node and to_node:
            ok, reason = validate_edge_pre_commit(
                from_node_type=from_node.node_type,
                from_node_subtype=from_node.node_subtype,
                to_node_type=to_node.node_type,
                to_node_subtype=to_node.node_subtype,
                edge_type=edge_type,
                source_platform=source_platform,
            )
            if not ok:
                reason_key = (reason or '').split(':', 1)[0]
                dedup_key = (
                    from_node.node_subtype, to_node.node_subtype, reason_key, source_platform,
                )
                if dedup_key not in _seen_rejections:
                    _seen_rejections.add(dedup_key)
                    # First-occurrence structured event — ops/SIEM alert target.
                    _precommit_rejection_logger.warning(
                        'event=pre_commit_rejection first_seen=true '
                        'from_subtype=%s to_subtype=%s edge_type=%s '
                        'source_platform=%s customer_id=%s reason=%s',
                        from_node.node_subtype, to_node.node_subtype, edge_type,
                        source_platform, customer_id, reason,
                    )
                else:
                    # Subsequent occurrences — standard WARN (not alerted).
                    logger.warning(
                        '[invariants] edge rejected: %s (from_node=%s to_node=%s)',
                        reason, from_node_id, to_node_id,
                    )
                return (None, False)
    except Exception:
        # Never fail ingest on invariant module errors — log and proceed.
        pass

    # I3'-E pre-commit clamp (causal edge without evidence → unearned).
    # SHADOW MODE by default (context_graph_invariants.EDGE_CLAMP_ENFORCE):
    # computes what WOULD be clamped and stamps a non-authoritative
    # properties['would_be_clamped'] marker, without touching the real
    # confidence/evidence_tier/evidence_clamped fields. Flipping
    # EDGE_CLAMP_ENFORCE to True switches this block to the real mutation
    # (confidence capped, properties['evidence_clamped'] stamped) — see
    # context_graph_invariants.py's I3'-E section for the full rationale.
    try:
        from utils.context_graph_invariants import (
            check_unearned_confidence_edge,
            EDGE_CLAMP_ENFORCE,
        )
        _edge_props = kwargs.get('properties')
        _evidence_tier = _edge_props.get('evidence_tier') if isinstance(_edge_props, dict) else None
        _derivation = _edge_props.get('derivation') if isinstance(_edge_props, dict) else None
        would_clamp, would_be_conf, clamp_reason = check_unearned_confidence_edge(
            edge_type=edge_type,
            evidence_tier=_evidence_tier,
            derivation=_derivation,
            properties=_edge_props,
            confidence=confidence,
        )
        if would_clamp:
            _edge_clamp_key = (source_platform, edge_type, _derivation)
            _mode = 'enforce' if EDGE_CLAMP_ENFORCE else 'shadow'
            if _edge_clamp_key not in _seen_edge_unearned_clamps:
                _seen_edge_unearned_clamps.add(_edge_clamp_key)
                _edge_unearned_clamp_logger.warning(
                    "event=unearned_confidence_clamp_edge first_seen=true "
                    "mode=%s edge_type=%s source_platform=%s derivation=%s "
                    "evidence_tier=%s customer_id=%s confidence=%s "
                    "would_be_confidence=%s",
                    _mode, edge_type, source_platform, _derivation,
                    _evidence_tier, customer_id, confidence, would_be_conf,
                )
            else:
                logger.debug(
                    "[invariants] I3'-E unearned-confidence edge clamp "
                    "(%s): %s via %s confidence %s -> %s",
                    _mode, edge_type, source_platform, confidence, would_be_conf,
                )

            new_edge_props = dict(_edge_props) if isinstance(_edge_props, dict) else {}
            if EDGE_CLAMP_ENFORCE:
                confidence = would_be_conf
                new_edge_props['evidence_clamped'] = True
                new_edge_props['evidence_clamped_reason'] = clamp_reason
            else:
                new_edge_props['would_be_clamped'] = True
                new_edge_props['would_be_clamped_reason'] = (
                    f'[SHADOW — not enforced, EDGE_CLAMP_ENFORCE=False] {clamp_reason}'
                )
            kwargs['properties'] = new_edge_props
    except Exception as _e:
        logger.debug(f"[invariants] I3'-E edge clamp pass-through (error): {_e}")

    existing = ContextEdge.query.filter_by(
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        edge_type=edge_type,
        source_platform=source_platform,
    ).first()

    if existing:
        existing.weight = weight
        existing.confidence = confidence
        existing.created_by = created_by
        if customer_id is not None:
            existing.customer_id = customer_id
        for k, v in kwargs.items():
            if hasattr(existing, k):
                setattr(existing, k, v)
        db.session.flush()
        return (existing, False)

    edge = ContextEdge(
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        edge_type=edge_type,
        weight=weight,
        confidence=confidence,
        source_platform=source_platform,
        created_by=created_by,
        customer_id=customer_id,
        **kwargs,
    )
    db.session.add(edge)
    db.session.flush()

    # WS-2 2g — supersession. This is a THIRD kind of write-time behavior,
    # distinct from both the I1/I2/I4 pre-commit gate above (which REJECTS
    # the incoming write) and the dedup match above it (which UPDATES the
    # incoming write's OWN matching row). Supersession instead MUTATES A
    # DIFFERENT, EXISTING row — setting its superseded_by to this new
    # edge's id — as a side effect of this new edge being created. It
    # deliberately does NOT match on source_platform the way dedup does
    # above: dedup lets different writers (wizard_a, llm_enrichment,
    # csv_import, ...) coexist as separate rows on purpose; supersession is
    # the second, looser matching semantic that decides, once a tier
    # ordering exists between those rows, that one of them should stop
    # being live. See utils/supersession.py for the full tier-ordering /
    # writer-priority rule this applies. Never runs on the dedup
    # update-in-place branch above — there is no "different, existing" row
    # to compare against in that case, since the write updates its own row.
    try:
        from utils.supersession import apply_supersession
        apply_supersession(edge)
    except Exception:
        logger.exception(
            '[supersession] failed to apply supersession for new edge %s '
            '(from_node=%s to_node=%s edge_type=%s) — edge write itself '
            'still succeeds',
            edge.edge_id, from_node_id, to_node_id, edge_type,
        )

    return (edge, True)
