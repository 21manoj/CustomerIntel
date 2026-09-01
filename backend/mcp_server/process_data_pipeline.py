"""
process_data post-ingest pipeline stages. Each stage is a standalone
function that takes customer_id, returns a step-description string (or
None), never raises, and logs its own errors.

Ported 2026-09-01 (Tier 2A-3), stage by stage as each one's dependencies
land. The old repo's version of this module carried 12 stages; only the
ones whose dependencies exist in this build are here. Stage order is the
old repo's, with items 28/32/38 (ordering bugs found there) already
reflected — see each stage's docstring.

Present:
  link_stakeholders_to_decisions  (item 38 — runs AFTER Wizard A)

Next sub-checkpoints, in order (see project memory):
  calculate_health_scores + publish_health_events  (Tier 2A-5)
  run_wizard_a_step                                 (Tier 2A-4)
Later phases: LLM tier-1 inference, Wizard B, signal analyst, urgent
scanner, ROI engine, approval-queue seeding, Qdrant indexing, onboarding
agent, WizardRun audit row.
"""
import logging

logger = logging.getLogger(__name__)


# Stakeholder role (substring-matched against STAKEHOLDER.node_subtype) →
# DECISION.node_subtype substrings that stakeholder is INVOLVED in.
STAKEHOLDER_DECISION_MAP = {
    'champion': ['renewal', 'champion', 'renewal_confirmed'],
    'executive_sponsor': ['escalation', 'executive_sponsor', 'playbook'],
    'technical_lead': ['technical', 'playbook', 'remediation'],
    'csm': ['playbook', 'intervention', 'playbook_crisis_recovery', 'playbook_exec_sponsor_change'],
    # The CSM's manager (account_details.csv's csm_manager) — a distinct
    # person from 'csm', one level up, so csm's list plus escalation.
    'cs_manager': ['escalation', 'playbook', 'intervention', 'playbook_crisis_recovery', 'playbook_exec_sponsor_change'],
    'primary_contact': ['renewal', 'champion'],
}
_DEFAULT_DECISION_SUBTYPES = ['playbook', 'renewal']


def link_stakeholders_to_decisions(customer_id: int):
    """INVOLVES edges from STAKEHOLDER nodes to same-account DECISION nodes
    whose subtype matches the stakeholder's role.

    Item 38 (2026-08-29): this must run AFTER Wizard A. For a standard
    registration (the canonical 4 CSVs) no decisions.csv is uploaded — the
    DECISION nodes that exist come from Wizard A's arc_decision_generator.
    Two earlier placements (after stakeholders.csv, then after decisions.csv)
    both ran before Wizard A and produced zero matches live; only 14/236
    STAKEHOLDER nodes platform-wide had any INVOLVES edge before the fix.
    Returns step string or None.
    """
    from models import ContextNode, ContextEdge
    from extensions import db
    from utils.context_graph import upsert_edge

    try:
        stakeholders = ContextNode.query.filter_by(customer_id=customer_id, node_type='STAKEHOLDER').all()
        decisions = ContextNode.query.filter_by(customer_id=customer_id, node_type='DECISION').all()
        if not stakeholders or not decisions:
            return None
        by_account: dict = {}
        for d in decisions:
            by_account.setdefault(d.account_id, []).append(d)

        created = 0
        for sn in stakeholders:
            role = (sn.node_subtype or '').lower()
            subtypes = next(
                (v for k, v in STAKEHOLDER_DECISION_MAP.items() if k in role),
                _DEFAULT_DECISION_SUBTYPES,
            )
            for dn in by_account.get(sn.account_id, []):
                dec_sub = (dn.node_subtype or '').lower()
                if not any(s in dec_sub for s in subtypes):
                    continue
                if ContextEdge.query.filter_by(from_node_id=sn.node_id, to_node_id=dn.node_id).first():
                    continue
                _edge, is_new = upsert_edge(
                    from_node_id=sn.node_id, to_node_id=dn.node_id,
                    edge_type='INVOLVES',
                    confidence=0.8,
                    properties={
                        'source': 'role_match',
                        'stakeholder_role': role,
                        'derivation': 'process_data.stakeholder_role_match',
                        # Typed heuristic constant, not an epistemic estimate (WS-1.2).
                        'confidence_semantics': 'role_match_heuristic_constant',
                    },
                    source_platform='process_data',
                    created_by='stakeholder_decision_linker',
                    customer_id=customer_id,
                )
                if is_new:
                    created += 1
        if created:
            db.session.commit()
            return f'stakeholder_edges_{created}'
        return None
    except Exception as e:
        logger.warning('Stakeholder-decision linking failed (non-fatal): %s', e)
        db.session.rollback()
        return None
