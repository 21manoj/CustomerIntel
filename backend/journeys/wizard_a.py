"""
Wizard A v2 runner — builds and persists journeys.

Writes:
  JourneyData            one row per account, journey_json v3 (upsert)
  Account.arc_type/arc_phase/arc_confidence   from the evidence-cited arc
                         (NULL arc_type for steady / unclassified — the
                         state lives in the journey; nothing is invented)
  HealthScore.qual_score / divergence / early_warning   per scored month,
                         from the leading-vs-trailing series. These columns
                         existed for the two-layer model and had no writer.

Does NOT write ContextNode / ContextEdge rows. Templates are attached to
the journey as `expected_path` only.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

# Bump on EVERY change to the journey JSON shape or the builder's semantics.
# Journeys carrying an older version are rebuilt by rebuild_stale_journeys
# (deploy step + /health 'stale_journeys' count) so the read surface never
# serves two shapes at once.
#   3.0  journey v3 (2026-09-02)
#   3.1  live months, quote/confidence/requires_review/review on episodes,
#        unreviewed weighting, generator_version inside the JSON (2026-09-04)
GENERATOR_VERSION = '3.1'


def stale_journey_query(customer_id: Optional[int] = None):
    from models import JourneyData
    q = JourneyData.query.filter((JourneyData.generator_version.is_(None)) | (JourneyData.generator_version != GENERATOR_VERSION))
    if customer_id is not None:
        q = q.filter(JourneyData.customer_id == int(customer_id))
    return q


def rebuild_stale_journeys(customer_id: Optional[int] = None) -> dict:
    """Rebuild every journey whose generator_version is behind GENERATOR_VERSION."""
    stale = stale_journey_query(customer_id).all()
    by_customer: Dict[int, Set[int]] = {}
    for jd in stale:
        by_customer.setdefault(jd.customer_id, set()).add(jd.account_id)
    out = {'generator_version': GENERATOR_VERSION, 'stale': len(stale), 'rebuilt': 0, 'customers': {}}
    for cid, aids in by_customer.items():
        res = run_wizard_a(cid, aids)
        out['rebuilt'] += res.get('processed', 0)
        out['customers'][cid] = res.get('processed', 0)
    logger.info('stale journeys: %d found, %d rebuilt to %s', out['stale'], out['rebuilt'], GENERATOR_VERSION)
    return out


def run_wizard_a(customer_id: int, account_ids: Optional[Iterable[int]] = None) -> dict:
    from models import Account, JourneyData
    from extensions import db
    from utils.vertical_registry import get_vertical_for_customer
    from journeys.journey_builder import build_journey

    vertical = get_vertical_for_customer(customer_id)
    accounts = Account.query.filter_by(customer_id=customer_id).order_by(Account.account_id).all()
    if account_ids is not None:
        wanted: Set[int] = set(account_ids)
        accounts = [a for a in accounts if a.account_id in wanted]

    result = {
        'status': 'completed', 'vertical': vertical, 'processed': 0, 'journeys_written': 0,
        'leading_rows_written': 0, 'arcs': {},
        'coverage': {'classified': 0, 'steady': 0, 'unclassified': 0},
    }
    if not accounts:
        result['status'] = 'skipped'
        return result

    for acct in accounts:
        try:
            journey = build_journey(acct, vertical)
        except Exception as e:
            logger.error('Wizard A failed for account %s (%s): %s', acct.account_id, acct.account_name, e, exc_info=True)
            continue
        journey['generator_version'] = GENERATOR_VERSION
        arc = journey['arc']
        acct.arc_type = arc.get('arc_type')
        acct.arc_phase = journey.get('current_phase')
        acct.arc_confidence = arc.get('confidence')

        jd = JourneyData.query.filter_by(customer_id=customer_id, account_id=acct.account_id).first()
        pattern = arc.get('arc_type') or arc['state']
        if jd:
            jd.journey_json = journey
            jd.journey_pattern = pattern
            jd.total_weeks = journey['total_weeks']
            jd.generator_version = GENERATOR_VERSION
            jd.updated_at = datetime.utcnow()
        else:
            db.session.add(JourneyData(
                customer_id=customer_id, account_id=acct.account_id, journey_json=journey,
                journey_pattern=pattern, total_weeks=journey['total_weeks'], generator_version=GENERATOR_VERSION,
            ))
        result['journeys_written'] += 1

        result['leading_rows_written'] += _write_leading_columns(acct.account_id, journey['leading_vs_trailing'])

        result['processed'] += 1
        result['coverage'][arc['state']] += 1
        result['arcs'][acct.account_id] = {
            'account_name': acct.account_name, 'state': arc['state'], 'arc_type': arc.get('arc_type'),
            'phase': journey.get('current_phase'), 'confidence': arc.get('confidence'),
            'supporting_episodes': len(arc.get('supporting_episode_ids', [])),
            'lead_days': journey['leading_vs_trailing'].get('lead_days'),
        }
        logger.info('Wizard A v2: account=%s (%s) state=%s arc=%s phase=%s evidence=%d',
                    acct.account_id, acct.account_name, arc['state'], arc.get('arc_type'),
                    journey.get('current_phase'), len(arc.get('supporting_episode_ids', [])))

    db.session.commit()
    n = result['processed']
    result['coverage']['classified_pct'] = round(100.0 * (result['coverage']['classified'] + result['coverage']['steady']) / n, 1) if n else 0.0
    return result


def _write_leading_columns(account_id: int, lvt: dict) -> int:
    from models import HealthScore
    by_month: Dict[date, dict] = {date.fromisoformat(s['month']): s for s in lvt.get('series', [])}
    written = 0
    for hs in HealthScore.query.filter_by(account_id=account_id).all():
        s = by_month.get(hs.measurement_month)
        if not s:
            continue
        qual, div, label = s['qual'], s['divergence'], s['early_warning']
        cur = (
            float(hs.qual_score) if hs.qual_score is not None else None,
            float(hs.divergence) if hs.divergence is not None else None,
            hs.early_warning,
        )
        if cur != (qual, div, label):
            hs.qual_score = qual
            hs.divergence = div
            hs.early_warning = label
            written += 1
    return written
