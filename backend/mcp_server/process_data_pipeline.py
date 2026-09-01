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
  calculate_health_scores         (Stage 2 — Tier 2A-4, ORM rewrite)
  backfill_product_adoption       (Stage 2b)
  link_stakeholders_to_decisions  (item 38 — runs AFTER Wizard A)

Next: run_wizard_a_step (Tier 2A-5 — needs health scores, hence after).
Deferred with their consumers: publish_health_events (event_system.py +
its three subscribers — push intelligence, product health, CG regen —
are all later phases; there is nothing to publish to yet), LLM tier-1
inference, Wizard B, signal analyst, urgent scanner, ROI engine,
approval-queue seeding, Qdrant indexing, onboarding agent, WizardRun
audit row.
"""
import logging
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Stage 2: Health score calculation (immutable scores)
# ═══════════════════════════════════════════════════════════════

_HEALTH_UPSERT_COLUMNS = (
    'health_score', 'kpi_only_score', 'health_status', 'contributing_pillars', 'calculated_at',
)


def calculate_health_scores(
    customer_id: int,
    acct_list: list,
    mode: str = 'auto',
) -> Tuple[Optional[str], Set[int], Dict[str, float]]:
    """Score every unscored (account, month) pair from KPIMeasurement.

    'auto': existing HealthScore rows are immutable — only new months are
    scored (weight changes apply forward only). 'full_recalc': every month
    is rewritten with current weights.

    Rewritten on the ORM (Tier 2A-4). The old repo's version opened its own
    raw-SQL engine to write health_scores, which is why score_calculator's
    account_status sync never ran for the real pipeline (item 28) — and why
    its month-over-month UPDATE went unnoticed: it subtracted the row's own
    health_score from itself instead of the LAG() value, so
    change_from_last_month was 0.00 on every row it ever wrote (confirmed
    on customer 359's 72 rows). Computed correctly here.

    Also now written: kpi_only_score (= health_score; this IS the pure-KPI
    score, and the NRR/churn models are specified to read only that
    column — it was NULL on every row before). Lifecycle-stage pillar
    weights are passed to the calculator explicitly and actually applied
    (see utils/vertical_health.calculate_kpi_health). `written` counts
    rows actually inserted/updated, not rows attempted.

    Still not written (no source for it yet): pillar_weights (the scorer
    doesn't expose the weights it used) and trend (no writer in the old
    repo either).

    Returns (step, changed_account_ids, timings).
    """
    import utils.health_thresholds as ht
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from models import KPIMeasurement, CustomerConfig, HealthScore
    from extensions import db
    from mcp_server.common import get_health_functions
    from utils.lifecycle_stages import resolve_account_stage, get_stage_weights

    timings: Dict[str, float] = {}
    changed: Set[int] = set()
    t0 = time.time()
    acct_ids = [a.account_id for a in acct_list]
    if not acct_ids:
        return None, changed, timings

    try:
        calculate_fn, _, _ = get_health_functions(customer_id)
        acct_by_id = {a.account_id: a for a in acct_list}

        cc = CustomerConfig.query.filter_by(customer_id=customer_id).first()
        lifecycle = cc.lifecycle_stage_weights if cc and cc.lifecycle_stage_weights else None

        scored: Set[tuple] = set()
        if mode != 'full_recalc':
            scored = {
                (r[0], r[1]) for r in HealthScore.query.filter(HealthScore.account_id.in_(acct_ids))
                .with_entities(HealthScore.account_id, HealthScore.measurement_month).all()
            }

        groups: Dict[tuple, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
        skipped_immutable = 0
        for k in KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(acct_ids)).all():
            month = (k.measured_at.date() if k.measured_at else date.today()).replace(day=1)
            if (k.account_id, month) in scored:
                skipped_immutable += 1
                continue
            groups[(k.account_id, month)][k.kpi_code].append(float(k.value))
        timings['kpi_grouping'] = round(time.time() - t0, 2)
        if skipped_immutable:
            logger.info(
                'Immutable scores: skipped %d KPI rows (%d scored months preserved) — %d new pairs',
                skipped_immutable, len(scored), len(groups),
            )

        now = datetime.utcnow()
        rows = []
        for (aid, month), kpi_groups in groups.items():
            kpi_vals = {code: sum(v) / len(v) for code, v in kpi_groups.items()}
            pillar_overrides = None
            if lifecycle:
                stage = resolve_account_stage(acct_by_id[aid], month, lifecycle)
                pillar_overrides, _kpi_overrides = get_stage_weights(stage)
            try:
                health, pillars = calculate_fn(
                    kpi_vals, customer_id=customer_id, pillar_weight_overrides=pillar_overrides,
                )
            except Exception as calc_err:
                logger.warning('Health score calc failed for account %s month %s: %s', aid, month, calc_err)
                continue
            score = round(health, 2)
            rows.append({
                'account_id': aid,
                'measurement_month': month,
                'health_score': score,
                'kpi_only_score': score,
                'health_status': ht.classify(health),
                'contributing_pillars': {k: round(v, 2) for k, v in pillars.items()} if pillars else None,
                'calculated_at': now,
            })
        timings['health_calc'] = round(time.time() - t0, 2)

        written = 0
        if rows:
            stmt = pg_insert(HealthScore.__table__).values(rows)
            if mode == 'full_recalc':
                stmt = stmt.on_conflict_do_update(
                    index_elements=['account_id', 'measurement_month'],
                    set_={c: stmt.excluded[c] for c in _HEALTH_UPSERT_COLUMNS},
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=['account_id', 'measurement_month'])
            res = db.session.execute(stmt)
            written = res.rowcount if res.rowcount is not None else len(rows)
            changed = {r['account_id'] for r in rows}
            _update_month_over_month(changed)
            db.session.commit()
        timings['health_write'] = round(time.time() - t0, 2)
        logger.info('Health scores: %d written — customer %s (mode=%s)', written, customer_id, mode)

        if changed:
            _sync_account_status(customer_id, changed)

        return f'health_scores_{mode}_{written}_written', changed, timings
    except Exception as e:
        logger.error('Health score calculation failed: %s', e, exc_info=True)
        db.session.rollback()
        return None, set(), timings


def _update_month_over_month(account_ids: Set[int]) -> None:
    """change_from_last_month = this month's score minus the previous scored
    month's, per account. Recomputed over every row of each changed
    account, so a full_recalc corrects history too."""
    from models import HealthScore
    rows = HealthScore.query.filter(HealthScore.account_id.in_(account_ids)).order_by(
        HealthScore.account_id, HealthScore.measurement_month).all()
    prev = None
    for hs in rows:
        if prev is not None and prev.account_id == hs.account_id:
            hs.change_from_last_month = round(float(hs.health_score) - float(prev.health_score), 2)
        else:
            hs.change_from_last_month = None
        prev = hs


def _sync_account_status(customer_id: int, account_ids: Set[int]) -> int:
    """Item 28: keep Account.account_status in step with the latest health
    status for the accounts scored this run. healthy → active,
    at_risk/critical → at_risk, churned is terminal and never overwritten."""
    from models import Account, HealthScore
    from extensions import db
    try:
        latest: Dict[int, str] = {}
        for hs in HealthScore.query.filter(HealthScore.account_id.in_(account_ids)).order_by(
                HealthScore.account_id, HealthScore.measurement_month.desc()).all():
            latest.setdefault(hs.account_id, hs.health_status)
        synced = 0
        for acct in Account.query.filter(Account.account_id.in_(account_ids)).all():
            if (acct.account_status or '').lower() == 'churned':
                continue
            status = latest.get(acct.account_id)
            target = (
                'active' if status == 'healthy'
                else 'at_risk' if status in ('at_risk', 'critical')
                else acct.account_status
            )
            if target != acct.account_status:
                acct.account_status = target
                synced += 1
        if synced:
            db.session.commit()
            logger.info('account_status synced for %d accounts (customer %s)', synced, customer_id)
        return synced
    except Exception as e:
        logger.warning('account_status sync failed (non-fatal): %s', e)
        db.session.rollback()
        return 0


# ═══════════════════════════════════════════════════════════════
# Stage 2b: Product adoption back-fill
# ═══════════════════════════════════════════════════════════════

def backfill_product_adoption(customer_id: int, acct_list: list, vertical: str) -> Optional[str]:
    """Copy the latest adoption-pillar score onto each account's
    profile_metadata['product_adoption'] and every products[i]['adoption'].

    Which pillar is "adoption" comes from the vertical's pillar_roles
    registry. The old repo hardcoded 'P1' — the adoption pillar for SaaS
    and dc2_s, but Revenue & Unit Economics for datacenter_v1 (adoption is
    P6 there), so every datacenter account's product_adoption was its
    revenue-pillar score. A vertical with no adoption pillar
    (healthcare_provider, manufacturing_iot) is a no-op, not a wrong
    number.
    """
    from models import HealthScore
    from extensions import db
    from utils.vertical_registry import role

    pillar = role(vertical, 'adoption')
    if not pillar:
        return None
    try:
        count = 0
        for acct in acct_list:
            pm = acct.profile_metadata or {}
            prods = pm.get('products')
            if not isinstance(prods, list) or not prods:
                continue
            latest = HealthScore.query.filter_by(account_id=acct.account_id).order_by(
                HealthScore.measurement_month.desc()).first()
            score = (latest.contributing_pillars or {}).get(pillar) if latest else None
            if score is None:
                continue
            adoption = round(float(score), 1)
            # New dict + new list objects: SQLAlchemy's JSON change detection
            # compares by value, so mutating the loaded dict in place and
            # reassigning it (what the old code did) is a silent no-op.
            new_prods = [
                {**p, 'adoption': adoption} if isinstance(p, dict) else p for p in prods
            ]
            acct.profile_metadata = {**pm, 'products': new_prods, 'product_adoption': adoption}
            count += 1
        if count:
            db.session.commit()
            return f'product_adoption_backfill({count})'
        return None
    except Exception as e:
        logger.warning('Product adoption back-fill failed (non-fatal): %s', e)
        db.session.rollback()
        return None


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
