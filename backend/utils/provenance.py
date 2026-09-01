"""
Provenance vocabulary for context graph nodes/edges.

Three values describe how a node or edge entered the graph:

  observed  — came directly from customer-uploaded data (CSV ingest,
              SoR sync). The fact is asserted by the customer.
  inferred  — came from runtime inference over real customer signals
              (signal_analyst, urgent_signal_scanner, LLM Tier-1 enrichment,
              push_intelligence). Not asserted by the customer but grounded
              in their actual data.
  synthetic — fabricated from a template independent of the customer's
              actual data (Wizard A arc-template-driven nodes, demo seeders).
              Useful for narrative scaffolding; NOT to be used as evidence
              in correlation/forecast fitting.

Default reader filter for any model that fits weights or makes predictions
(Wizard B correlation, Wizard C NRR validation, NRR forecast):

    WHERE source IN ('observed', 'inferred')

Synthetic rows are still returned by UI/journey queries — the filter is
only for downstream learners and outcome-economics calculations.

Edge-confidence threshold (paired with provenance for Wizard B/C):
    Even within ('observed', 'inferred'), an edge with confidence < 0.6
    is considered too noisy for correlation fitting.
"""

from __future__ import annotations

from typing import Iterable

# Canonical values
OBSERVED = 'observed'
INFERRED = 'inferred'
SYNTHETIC = 'synthetic'

ALL_SOURCES: tuple[str, ...] = (OBSERVED, INFERRED, SYNTHETIC)
TRUSTWORTHY_SOURCES: tuple[str, ...] = (OBSERVED, INFERRED)

# Edge-only interim tier (WS-2 adjudication matrix Hold 2, signed 2026-08-24).
# Cells 1-3 (csv_import x LED_TO/TRIGGERED/CAUSED_BY) were provisionally
# called `asserted` (a human claimed this causal link), but `asserted` is not
# determinable at write time — the platform cannot distinguish the
# load-driver from a human uploader (same API, same credentials class). The
# tier becomes meaningful only once a `data_origin` field capturing the
# *authenticated principal* at upload time lands in the write path and rows
# start arriving with it (Customer.data_origin, WS-2 2a). Until then,
# csv-path writers stamp UNKNOWN — the honest tier for "someone claimed
# this and we can't say who."
#
# UNKNOWN is deliberately NOT added to ALL_SOURCES/TRUSTWORTHY_SOURCES: those
# tuples describe ContextNode.source (a node-level fact/inference axis).
# UNKNOWN describes ContextEdge properties['evidence_tier'] on the csv_import
# causal-claim edges — a different axis entirely.
#
# It is also, by construction, never the same representation as quarantine.
# Quarantine (WS-2 2e, not yet shipped) means EXCLUSION — "nothing can be
# reconstructed" — and today is represented by a NULL source_platform / the
# complete absence of an evidence_tier property. UNKNOWN is always an
# explicit, non-NULL string stamped by a writer that knows exactly which
# path produced the row; it must never collapse into that NULL/absent
# representation, or the 49 remaining NULL-source quarantine rows stop being
# distinguishable from the csv path's interim state (Hold 2 follow-up).
UNKNOWN = 'unknown'

# Edge-confidence threshold for Wizard B/C correlation reads.
# Below this, an edge is treated as too uncertain to inform learning.
DEFAULT_EDGE_CONFIDENCE_THRESHOLD = 0.6

# Legacy → canonical mapping (used by migration backfill and by readers
# that may still encounter pre-migration values during a rolling deploy).
_LEGACY_VALUES = {'customer', 'system'}


def normalize(value: str | None) -> str:
    """Map a legacy or canonical value to a canonical value.

    'customer' → 'observed'
    'system'   → 'inferred'  (conservative — synthetic is reserved for
                              writers that explicitly tag themselves)
    Already-canonical values pass through.
    Unknown values pass through unchanged so readers can detect drift.
    """
    if value == 'customer':
        return OBSERVED
    if value == 'system':
        return INFERRED
    # FAIL-CLOSED (node-evidence-gap review, 2026-08-24): this used to be
    # `return value or OBSERVED` — a NULL source normalized to 'observed',
    # i.e. trusted by default. That fail-open is how
    # count_trustworthy_causal_edges admitted every edge for its entire
    # life: it read `edge.source`, an attribute ContextEdge doesn't even
    # have, got None, and None became observed. A missing value must never
    # become the most-trusted value.
    return value


def is_trustworthy(source: str | None) -> bool:
    """True if the source should be included in correlation/forecast reads."""
    return normalize(source) in TRUSTWORTHY_SOURCES


def trustworthy_sources(extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Return the default reader allowlist, optionally extended.

    Pass `extra=(SYNTHETIC,)` to opt into synthetic rows for a specific
    query (e.g. a UI render that wants to show the model-generated badge).
    """
    if not extra:
        return TRUSTWORTHY_SOURCES
    return tuple(set(TRUSTWORTHY_SOURCES) | set(extra))


def is_trustworthy_edge(
    edge,
    *,
    min_confidence: float = DEFAULT_EDGE_CONFIDENCE_THRESHOLD,
) -> bool:
    """True if a ContextEdge row is trustworthy for correlation fitting.

    Two checks:
      1. Confidence >= min_confidence (or null → treated as trustworthy
         for backward compat with rows pre-dating per-edge confidence).
      2. The edge's endpoint nodes are both trustworthy (caller's
         responsibility — pass already-filtered nodes via subquery, or
         join + filter at SQL level, using `is_trustworthy()`/
         `normalize()` so the legacy 'customer' DB default is included —
         see item 23 phase-2 sweep, 2026-08-25).

    This helper only checks #1.
    """
    conf = getattr(edge, 'confidence', None)
    if conf is None:
        return True
    try:
        return float(conf) >= min_confidence
    except (TypeError, ValueError):
        return True


def count_trustworthy_causal_edges(
    *,
    customer_id: int | None = None,
    account_id: int | None = None,
    min_confidence: float = DEFAULT_EDGE_CONFIDENCE_THRESHOLD,
) -> dict:
    """Count edges that pass both provenance + confidence gates.

    Used by Wizard B to surface "how much causal evidence backs this
    NRR forecast" alongside the forecast itself. Either ``customer_id``
    or ``account_id`` (or both) must be provided as a scope.

    Returns:
        {
            'total': int,                   # all edges in scope
            'trustworthy': int,             # passed both gates
            'dropped_synthetic': int,       # excluded by source filter
            'dropped_low_confidence': int,  # excluded by confidence gate
            'min_confidence': float,
        }

    Both gates apply: an edge from `synthetic` source is excluded
    even if its confidence is high; an edge from `inferred` source
    is excluded if confidence < threshold.
    """
    if customer_id is None and account_id is None:
        raise ValueError(
            'count_trustworthy_causal_edges requires customer_id or account_id'
        )

    from models import ContextEdge, ContextNode
    from extensions import db

    # Edge endpoint nodes carry the account_id; join via from_node_id.
    q = db.session.query(ContextEdge).join(
        ContextNode, ContextEdge.from_node_id == ContextNode.node_id,
    )
    if customer_id is not None:
        q = q.filter(ContextEdge.customer_id == customer_id)
    if account_id is not None:
        q = q.filter(ContextNode.account_id == account_id)

    edges = q.all()
    total = len(edges)
    dropped_synthetic = 0
    dropped_low_conf = 0
    trustworthy = 0

    # Synthetic-class edge writers per the signed WS-2 adjudication matrix
    # (cells 4-6: wizard_a template/trajectory edges are inferred-from-
    # nothing-observed — the class this stat exists to exclude). A NULL
    # source_platform is also untrusted: fail-closed.
    #
    # This block used to read `getattr(e, 'source', None)` — an attribute
    # ContextEdge does not have — so every edge got None, None normalized
    # to 'observed', and dropped_synthetic was structurally ALWAYS ZERO:
    # a guard that could never fire (node-evidence-gap review, 2026-08-24).
    synthetic_platforms = {'wizard_a'}
    for e in edges:
        sp = getattr(e, 'source_platform', None)
        if sp is None or sp in synthetic_platforms:
            dropped_synthetic += 1
            continue
        if not is_trustworthy_edge(e, min_confidence=min_confidence):
            dropped_low_conf += 1
            continue
        trustworthy += 1

    return {
        'total': total,
        'trustworthy': trustworthy,
        'dropped_synthetic': dropped_synthetic,
        'dropped_low_confidence': dropped_low_conf,
        'min_confidence': min_confidence,
    }
