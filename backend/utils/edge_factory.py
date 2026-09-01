"""
EdgeFactory (WS-2 2c) — the sanctioned path for writing a causal edge that
the platform inferred rather than observed as fact.

Per the WS-2 review (2026-08-24): "NULLing confidence alone was rejected as
a half-fix — rows neither confidently-wrong nor honestly-labelled, with
unaudited NULL-handling downstream." The fix is NULL confidence PAIRED with
a stamped evidence_tier (utils/provenance.py's observed/inferred/synthetic
vocabulary), so a NULL here always means "no calibrated estimate exists",
never "forgot to set it" or "defaulted to trusted". Every edge still routes
through utils.context_graph.upsert_edge, so the I1/I2/I17 pre-commit
invariants and from/to-pair dedup apply exactly as they do to every other
writer — this module supplies the confidence/evidence_tier/derivation
discipline on top, it does not bypass graph integrity.

`derivation` distinguishes system.self (the platform reacting to its own
prior inference or trigger condition — playbook auto-triggers, heuristic
close-linking) from system.external (a genuinely external logged fact — SoR
sync, a recorded trigger condition). WS-2 matrix Hold 1 signed cell 14
(playbook_auto_trigger x TRIGGERED) `observed` only on condition that
Evidence Density's observed-denominator excludes every system.self
derivation — otherwise the metric inflates every time more auto-triggers
ship. See tests/test_evidence_density_contract.py.
"""
from __future__ import annotations

from typing import Optional

from utils.context_graph import upsert_edge
from utils.provenance import INFERRED

# Cell 14 (playbook_auto_trigger x TRIGGERED): the platform re-triggering a
# playbook off its own prior inference, not an externally logged fact.
AUTO_TRIGGER_DERIVATION = 'system.self.playbook_auto_trigger'

# Cells 12/13 (playbook close-linker's DECISION/SIGNAL -> OUTCOME edges):
# the platform's own recency heuristic over already-observed nodes, not a
# logged causal fact either.
CLOSE_LINK_DERIVATION = 'system.self.playbook_close_linker'

# Cells 1-3 (csv_import x LED_TO/TRIGGERED/CAUSED_BY, WS-2 matrix Hold 2):
# neither system.self (the platform is not reacting to its own inference
# here) nor system.external (the platform did not itself log this as a
# fact — someone uploaded signal_edges.csv and claimed it). It's a third,
# distinct shape: an unattributed external claim via the upload path, held
# at `unknown` until Customer.data_origin lets a future pass re-tier it by
# authenticated principal.
CSV_IMPORT_DERIVATION = 'csv_import.unattributed'


def create_inferred_edge(
    from_node_id: int,
    to_node_id: int,
    edge_type: str,
    *,
    source_platform: str,
    derivation: str,
    customer_id: Optional[int] = None,
    evidence_tier: str = INFERRED,
    label: Optional[str] = None,
    extra_properties: Optional[dict] = None,
) -> tuple:
    """Write an edge whose causal weight the platform inferred, not observed.

    Confidence is always NULL — an inferred link has no calibrated point
    estimate to report. Callers with a real, calibrated confidence value
    for a genuinely observed edge should call utils.context_graph.upsert_edge
    directly instead of routing through here.

    Returns the (ContextEdge, created) tuple upsert_edge returns — including
    (None, False) if the I1/I2/I17 pre-commit gate rejected the edge.
    """
    properties = {'derivation': derivation, 'evidence_tier': evidence_tier}
    if label:
        properties['label'] = label
    if extra_properties:
        properties.update(extra_properties)
    return upsert_edge(
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        edge_type=edge_type,
        confidence=None,
        source_platform=source_platform,
        created_by='edge_factory',
        customer_id=customer_id,
        properties=properties,
    )
