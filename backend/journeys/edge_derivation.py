"""
Phase-transition edge derivation — tprm_v1 only.

The platform already computes phase structure (journeys/journey_builder.py's
detect_phases / detect_phases_from_evidence) and each phase names the
episode that triggered it (`trigger_episode_id`). This module turns that
already-computed structure into causal LED_TO edges between the trigger
episodes of consecutive phases: deterioration's trigger LED_TO
intervention's trigger LED_TO resolution's trigger, and so on.

That's the platform reacting to its own prior inference (the phase
detector it already ran), not an externally logged fact — `system.self`,
the same shape as utils/edge_factory.py's AUTO_TRIGGER_DERIVATION and
CLOSE_LINK_DERIVATION.

    derive_phase_transition_edges(customer_id, account_id, phases, episodes) -> list

Wired from journeys/wizard_a.py's run_wizard_a(), gated strictly on
`vertical == 'tprm_v1'` — no other vertical is touched.

Takes the dict shapes build_journey already returns (journey['phases'],
journey['episodes'] — the latter is [Episode.to_json() for e in episodes],
not Episode objects) rather than re-deriving episodes from the graph.

Every write routes through utils/edge_factory.py's create_inferred_edge,
which itself routes through utils.context_graph.upsert_edge — so the
I1/I2/I17 pre-commit invariants (backend/utils/context_graph_invariants.py)
and from/to-pair dedup apply exactly as they do to every other writer.
Never raises on a rejected or skipped pair; every pair considered is
reported in the returned list instead, for logging/testing.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# The platform reacting to its own already-computed phase structure, not an
# externally logged fact. Same naming convention and shape as edge_factory.py's
# AUTO_TRIGGER_DERIVATION ('system.self.playbook_auto_trigger') and
# CLOSE_LINK_DERIVATION ('system.self.playbook_close_linker').
PHASE_TRANSITION_DERIVATION = 'system.self.phase_transition_chain'

# edge_factory.create_inferred_edge's source_platform — wizard_a is where
# this is called from (journeys/wizard_a.py's run_wizard_a, tprm_v1 only).
SOURCE_PLATFORM = 'journeys.wizard_a'


def derive_phase_transition_edges(
    customer_id: int,
    account_id: int,
    phases: List[dict],
    episodes: List[dict],
) -> List[dict]:
    """Derive a causal LED_TO edge from each phase's trigger episode to the
    NEXT phase's trigger episode, walking `phases` in their existing
    chronological order (the order build_journey already produced them in
    — this function does not re-sort).

    Args:
        customer_id: tenant id, stamped on every edge written.
        account_id: carried into the returned summary for logging/audit
            context only. The evidence nodes cited here are already this
            account's own — `episodes` comes from
            journeys.journey_builder.collect_episodes, which is queried
            scoped to one account — so no separate node-ownership check is
            needed before writing; edge_factory.create_inferred_edge itself
            takes no account_id (edges are customer-scoped, not
            account-scoped, like every other ContextEdge writer).
        phases: journey['phases'] — dicts with at least 'name',
            'entered_at', 'exited_at', 'trigger_episode_id'.
        episodes: journey['episodes'] — Episode.to_json() dicts (NOT
            Episode objects), each with 'episode_id' and
            'evidence_node_ids' (a list of raw ContextNode ids).

    A consecutive pair is skipped — not an error — when either phase has no
    trigger_episode_id, both phases share the same trigger episode (no real
    transition happened), or either trigger episode has no
    evidence_node_ids to anchor an edge on.

    Returns one entry per consecutive phase pair walked:
        {'account_id', 'phase_a', 'phase_b', 'trigger_episode_id_a',
         'trigger_episode_id_b', 'status', 'from_node_id'?, 'to_node_id'?,
         'edge_id'?}
    status is one of:
        'created'             a new edge was written
        'updated'             an existing (from, to, edge_type, source_platform)
                               edge was re-upserted (idempotent re-run)
        'rejected'             create_inferred_edge/upsert_edge's I1/I2/I17
                               pre-commit gate rejected the edge
        'skipped_no_trigger'   one or both phases have no trigger_episode_id
        'skipped_same_trigger' both phases cite the same trigger episode
        'skipped_no_evidence'  a cited trigger episode has no evidence_node_ids
    """
    from utils.edge_factory import create_inferred_edge

    by_id: Dict[str, dict] = {e['episode_id']: e for e in (episodes or []) if e.get('episode_id')}

    results: List[dict] = []
    phase_list = phases or []
    for phase_a, phase_b in zip(phase_list, phase_list[1:]):
        trig_a_id: Optional[str] = phase_a.get('trigger_episode_id')
        trig_b_id: Optional[str] = phase_b.get('trigger_episode_id')
        entry = {
            'account_id': account_id, 'phase_a': phase_a.get('name'), 'phase_b': phase_b.get('name'),
            'trigger_episode_id_a': trig_a_id, 'trigger_episode_id_b': trig_b_id,
        }

        if not trig_a_id or not trig_b_id:
            entry['status'] = 'skipped_no_trigger'
            results.append(entry)
            continue
        if trig_a_id == trig_b_id:
            entry['status'] = 'skipped_same_trigger'
            results.append(entry)
            continue

        ep_a, ep_b = by_id.get(trig_a_id), by_id.get(trig_b_id)
        nodes_a = (ep_a or {}).get('evidence_node_ids') or []
        nodes_b = (ep_b or {}).get('evidence_node_ids') or []
        if not nodes_a or not nodes_b:
            entry['status'] = 'skipped_no_evidence'
            results.append(entry)
            continue

        from_node_id, to_node_id = nodes_a[0], nodes_b[0]
        edge, created = create_inferred_edge(
            from_node_id, to_node_id, edge_type='LED_TO', source_platform=SOURCE_PLATFORM,
            derivation=PHASE_TRANSITION_DERIVATION, customer_id=customer_id,
            label=f"{phase_a.get('name')} to {phase_b.get('name')}",
        )
        entry['from_node_id'] = from_node_id
        entry['to_node_id'] = to_node_id
        if edge is None:
            entry['status'] = 'rejected'
        else:
            entry['edge_id'] = edge.edge_id
            entry['status'] = 'created' if created else 'updated'
        results.append(entry)

    return results
