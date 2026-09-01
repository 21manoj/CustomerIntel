"""
Edge supersession (WS-2 2g).

Fixes a confirmed, live bug: ``upsert_edge()``
(``utils/context_graph.py``) dedups on ``(from_node_id, to_node_id,
edge_type, source_platform)`` — so different writers (``wizard_a``,
``llm_enrichment``, ``csv_import``, etc.) each get their own parallel edge
on the same node pair by construction; nothing collapses them.
``get_causal_chain()`` has no filter for this, so it returns
duplicate/contradictory edges on the same triple, and any consumer
aggregating over a chain double-counts.

Supersession is a *second*, looser matching semantic layered on top of
dedup: it keys on ``(from_node_id, to_node_id, edge_type)`` only — dropping
``source_platform`` on purpose. Dedup and supersession deliberately
disagree by design: dedup lets different writers coexist as separate rows;
supersession decides, once a tier ordering exists, that one of those rows
should stop being live. Both are correct, for different questions.

── Tier vocabulary ──────────────────────────────────────────────────────

An edge's evidentiary tier is read from ``properties['evidence_tier']``
(not a DB column — see ``utils/edge_factory.py``). Four values, ranked
low to high:

    unknown < inferred < asserted < observed

This is a *different* axis from ``utils.provenance``'s three-value
observed/inferred/synthetic vocabulary (which governs Wizard B/C's
trust-for-learning reader filter). The two are related in spirit but not
interchangeable: this module's ranking exists purely to adjudicate which
of two edges on the same triple should stay live.

── Supersession rule (WS-2 review, 2026-08-27 decision) ────────────────

1. Cross-tier: full monotonic ordering. An arriving edge supersedes an
   existing live edge (``superseded_by IS NULL``) on the same triple only
   when the arriving edge's tier is STRICTLY higher.
2. Within the SAME tier, different writers (different ``source_platform``):
   only defined for ``inferred`` — consult INFERRED_TIER_WRITER_PRIORITY
   below. An undefined writer (on either side) means NO supersession —
   fail safe, not fail by guessing.
3. Same writer, same tier, re-firing on the same triple: the newer edge
   supersedes the older one — recency, the one case where recency alone
   decides.
4. ``observed``/``asserted`` tier ties (same tier, any writers, not the
   same-writer-recency case above): do NOT auto-resolve. Leave both live.
   This is deliberate — a CRM sync and a CSM's manual assertion disagreeing
   is a real disagreement a human should see, not a priority list's call.
   The writer-priority mechanism from rule 2 is NEVER extended to this
   case, under any circumstance.

── The writer-priority list is a living artifact ───────────────────────

INFERRED_TIER_WRITER_PRIORITY only ranks the ``inferred``-tier writer pair
this decision was made for (``llm_enrichment`` vs ``wizard_a``). Every new
inferred-tier writer EdgeFactory produces (``utils/edge_factory.py``) needs
a line added here. An unranked writer participating in a same-tier
collision means no supersession fires for that edge — the safe failure
mode, not a guess.

── An extension this implementation makes, not a verbatim decision ─────

The tier-ordering decision above was written against edges that carry an
explicit ``evidence_tier``. It does not by itself say what tier an edge
with NO ``evidence_tier`` key at all should be treated as for
*supersession-ranking* purposes — that specific question was decided for
a different mechanism (2f's unearned-confidence clamp, which treats
absence as in-scope of "unearned"). ``resolve_evidence_tier()`` below
extends that same philosophy to 2g: an edge with an absent ``evidence_tier``
is treated as ``inferred`` for ranking, because the writers that don't yet
stamp it (``wizard_a``, ``llm_enrichment``) ARE semantically inferred-tier
per the signed adjudication matrix, even though they haven't been updated
to say so on the row itself. This is a reasonable extension of the
existing philosophy, not something handed down verbatim for 2g
specifically — flagged here, and in the implementation report, as such.
"""
from __future__ import annotations

from typing import Optional

# ─── Tier vocabulary ─────────────────────────────────────────────────────

OBSERVED = 'observed'
ASSERTED = 'asserted'
INFERRED = 'inferred'
UNKNOWN = 'unknown'

# Monotonic rank: higher number = stronger evidentiary claim.
TIER_RANK = {
    OBSERVED: 3,
    ASSERTED: 2,
    INFERRED: 1,
    UNKNOWN: 0,
}

# ─── Writer priority (within the `inferred` tier only) ──────────────────
#
# Highest priority first. First-pass ranking per the WS-2 review
# (2026-08-27): llm_enrichment makes a case-specific judgment; wizard_a's
# template-based inference is generically pattern-matched, so a
# case-specific LLM read outranks a generic template match on the same
# triple. This is explicitly incomplete — other inferred-tier writers
# (auto_linker, signal_analyst, urgent_signal_scanner) are NOT yet ranked.
# Add a line here every time utils/edge_factory.py grows a new inferred-tier
# writer. A writer not on this list never participates in supersession
# (see should_supersede — fail safe, not fail by guessing).
INFERRED_TIER_WRITER_PRIORITY = [
    'llm_enrichment',
    'wizard_a',
]


def _writer_priority_index(source_platform: Optional[str]) -> Optional[int]:
    """Return this writer's position in INFERRED_TIER_WRITER_PRIORITY
    (lower = higher priority), or None if unranked."""
    try:
        return INFERRED_TIER_WRITER_PRIORITY.index(source_platform)
    except ValueError:
        return None


def resolve_evidence_tier(properties: Optional[dict]) -> str:
    """Resolve an edge's tier for supersession-ranking purposes.

    Reads ``properties['evidence_tier']`` when present and recognized.
    An absent or unrecognized value is treated as ``inferred`` — see the
    module docstring's "extension this implementation makes" section:
    this is this implementation's own reasonable extension of 2f's
    absence-is-in-scope philosophy, not a verbatim instruction for 2g.
    """
    if isinstance(properties, dict):
        tier = properties.get('evidence_tier')
        if tier in TIER_RANK:
            return tier
    return INFERRED


def should_supersede(
    *,
    existing_tier: str,
    existing_platform: Optional[str],
    incoming_tier: str,
    incoming_platform: Optional[str],
) -> bool:
    """Decide whether an incoming edge should supersede an existing live
    edge on the same (from_node_id, to_node_id, edge_type) triple.

    Pure decision function — no DB access — so it can be unit-tested
    directly against every rule in the module docstring without needing
    to fight upsert_edge()'s own (from,to,edge_type,source_platform) dedup,
    which — by construction — never lets two rows with identical platform
    AND triple coexist (a same-writer re-fire always updates the one
    matching row in place, rather than creating a second one for
    supersession to act on).
    """
    existing_rank = TIER_RANK.get(existing_tier, TIER_RANK[UNKNOWN])
    incoming_rank = TIER_RANK.get(incoming_tier, TIER_RANK[UNKNOWN])

    # Rule 1: cross-tier, full monotonic ordering. Strictly higher only.
    if incoming_rank > existing_rank:
        return True
    if incoming_rank < existing_rank:
        return False

    # Same tier from here on.

    # Rule 3: same writer, same tier, re-firing — recency wins.
    if incoming_platform is not None and incoming_platform == existing_platform:
        return True

    # Rule 2: within `inferred`, different writers — writer-priority list,
    # fail safe if either side is unranked.
    if existing_tier == INFERRED and incoming_tier == INFERRED:
        existing_pos = _writer_priority_index(existing_platform)
        incoming_pos = _writer_priority_index(incoming_platform)
        if existing_pos is None or incoming_pos is None:
            return False
        return incoming_pos < existing_pos

    # Rule 4: observed/asserted/unknown ties across different writers —
    # never auto-resolve. The writer-priority mechanism is deliberately
    # not extended here under any circumstance.
    return False


def apply_supersession(edge, exclude_self: bool = True) -> list:
    """DB-level side effect: given `edge` (already flushed, has edge_id),
    find other LIVE edges on the same (from_node_id, to_node_id, edge_type)
    triple and resolve supersession against each.

    This mutates OTHER, EXISTING rows — a third kind of write-time
    behavior distinct from upsert_edge()'s own I1/I2/I4 pre-commit checks
    (which REJECT the incoming write) and its dedup (which UPDATES the
    incoming write's own matching row). Supersession's match key is
    intentionally looser than dedup's (it drops source_platform), so it
    can catch a different writer's edge on the same triple that dedup, by
    design, leaves as a separate row.

    Two distinct resolution shapes coexist here, matching the module
    docstring's rules:
      - Cross-tier (rule 1) and same-writer-recency (rule 3) are
        ARRIVAL-ORDER-based: only the just-created `edge` can retire an
        older row; an older, stronger row never reaches back to retire a
        newer, weaker one that was just written (should_supersede()
        already encodes this one-directional shape).
      - Writer-priority within `inferred` (rule 2) is IDENTITY-based, not
        arrival-order-based: "the higher-ranked writer's edge supersedes
        the lower-ranked writer's edge" regardless of which one happened
        to be written first. If `edge` is the lower-priority one, it must
        be born superseded by the pre-existing higher-priority edge — the
        mirror image of the usual "new edge supersedes old row" direction.
        This mirror check is scoped narrowly to inferred/inferred,
        both-ranked pairs; it must not apply to cross-tier or recency,
        which are genuinely order-dependent.

    Returns the list of edges just marked superseded (for logging/tests).
    Does not include `edge` itself even if `edge` ends up superseded —
    check `edge.superseded_by` for that.
    """
    from extensions import db
    from models import ContextEdge

    incoming_tier = resolve_evidence_tier(edge.properties)
    incoming_platform = edge.source_platform

    query = ContextEdge.query.filter(
        ContextEdge.from_node_id == edge.from_node_id,
        ContextEdge.to_node_id == edge.to_node_id,
        ContextEdge.edge_type == edge.edge_type,
        ContextEdge.superseded_by.is_(None),
    )
    if exclude_self:
        query = query.filter(ContextEdge.edge_id != edge.edge_id)

    superseded = []
    edge_itself_superseded = False

    for other in query.all():
        if edge_itself_superseded:
            # The just-created edge has already lost to a stronger
            # pre-existing writer; stop evaluating it against further rows.
            break

        other_tier = resolve_evidence_tier(other.properties)
        other_platform = other.source_platform

        if should_supersede(
            existing_tier=other_tier,
            existing_platform=other_platform,
            incoming_tier=incoming_tier,
            incoming_platform=incoming_platform,
        ):
            other.superseded_by = edge.edge_id
            superseded.append(other)
            continue

        # Mirror check for identity-based writer-priority only (see
        # docstring above). Narrowly scoped: both sides must be
        # `inferred`, different platforms, and both present in
        # INFERRED_TIER_WRITER_PRIORITY.
        if (
            incoming_tier == INFERRED
            and other_tier == INFERRED
            and incoming_platform != other_platform
        ):
            incoming_pos = _writer_priority_index(incoming_platform)
            other_pos = _writer_priority_index(other_platform)
            if (
                incoming_pos is not None
                and other_pos is not None
                and other_pos < incoming_pos
            ):
                edge.superseded_by = other.edge_id
                edge_itself_superseded = True

    if superseded or edge_itself_superseded:
        db.session.flush()

    return superseded
