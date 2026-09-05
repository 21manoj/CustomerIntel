"""
Read surface over journey v3 and its evidence (Google G1: "evidence needs a
surface"). Pure reads, framework-agnostic; the MCP tools and HTTP routes
call these inside an app context.

    list_journeys(customer_id)                       portfolio: one row per account
    get_journey(customer_id, account_id, compact)    the journey + an evidence index keyed by node id
    get_evidence(customer_id, ...)                   evidence nodes, filterable
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional


def db_get_account(account_id: int):
    from extensions import db
    from models import Account
    return db.session.get(Account, int(account_id))


def origin_block(customer_id: int) -> dict:
    """{data_origin, label, synthetic, disclosure} for a tenant — on every read surface."""
    from extensions import db
    from models import Customer
    from utils.data_origin import block
    return block(db.session.get(Customer, int(customer_id)))


def evidence_view(n) -> dict:
    """One evidence node as the surface shows it: the quote, its typing, who,
    where it came from, how sure, and any human decision on it."""
    p = n.properties or {}
    return {
        'node_id': n.node_id, 'account_id': n.account_id, 'node_type': n.node_type, 'subtype': n.node_subtype,
        'role': p.get('role'), 'occurred_at': n.occurred_at.isoformat() if n.occurred_at else None,
        'quote': p.get('quote') or n.title, 'title': n.title,
        'sentiment': p.get('sentiment'), 'sentiment_score': p.get('sentiment_score'),
        'polarity_conflict': p.get('polarity_conflict'), 'effective_urgency': p.get('effective_urgency'),
        'person': {'name': p.get('stakeholder_name'), 'title': p.get('stakeholder_title'), 'role': p.get('stakeholder_role'),
                   'unresolved': p.get('person_unresolved')} if p.get('stakeholder_name') else None,
        'provenance': {'source': n.source, 'source_platform': n.source_platform, 'source_event_id': n.source_event_id,
                       'source_ref': n.source_ref, 'signal_id': p.get('signal_id'), 'origin_platform': p.get('origin_platform'),
                       'evidence_tier': p.get('evidence_tier'), 'tier': n.tier,
                       'classification_basis': p.get('classification_basis'), 'llm_model_version': p.get('llm_model_version'),
                       'original_subtype': p.get('original_subtype')},
        'confidence': p.get('confidence'), 'requires_review': bool(p.get('requires_review')),
        'review': p.get('review'), 'use_case': p.get('use_case'), 'attributes': p.get('attributes'),
    }


def get_evidence(customer_id: int, account_id: Optional[int] = None, node_ids: Optional[Iterable[int]] = None,
                 role: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                 include_rejected: bool = False, limit: int = 200) -> List[dict]:
    from models import ContextNode
    q = ContextNode.query.filter(ContextNode.customer_id == int(customer_id),
                                 ContextNode.node_type.in_(['SIGNAL', 'DECISION', 'OUTCOME']),
                                 ContextNode.source == 'observed')
    if account_id:
        q = q.filter(ContextNode.account_id == int(account_id))
    if node_ids:
        q = q.filter(ContextNode.node_id.in_([int(i) for i in node_ids]))
    if since:
        q = q.filter(ContextNode.occurred_at >= datetime.fromisoformat(str(since)[:19]))
    if until:
        q = q.filter(ContextNode.occurred_at <= datetime.fromisoformat(str(until)[:19]))
    rows = q.order_by(ContextNode.occurred_at.desc()).limit(int(limit)).all()
    out = []
    for n in rows:
        v = evidence_view(n)
        if role and v['role'] != role:
            continue
        if not include_rejected and (v['review'] or {}).get('status') == 'rejected':
            continue
        out.append(v)
    return out


def get_journey(customer_id: int, account_id: int, compact: bool = False) -> Optional[dict]:
    from models import JourneyData, ContextNode, QualitativeSignal
    j = JourneyData.query.filter_by(customer_id=int(customer_id), account_id=int(account_id)).first()
    if not j:
        return None
    journey = dict(j.journey_json)
    ids = sorted({nid for e in journey.get('episodes', []) for nid in (e.get('evidence_node_ids') or [])})
    evidence = {}
    if ids:
        for n in ContextNode.query.filter(ContextNode.node_id.in_(ids)).all():
            evidence[str(n.node_id)] = evidence_view(n)
    open_review = QualitativeSignal.query.filter_by(account_id=int(account_id), requires_review=True).count()
    from models import Account
    acct = db_get_account(int(account_id))
    pm = (acct.profile_metadata or {}) if acct else {}
    journey['account'] = {'use_cases': pm.get('use_cases') or [], 'contract_type': pm.get('contract_type'),
                          'renewal_date': pm.get('renewal_date') or pm.get('contract_end'), 'refresh_date': pm.get('refresh_date'),
                          'champion': pm.get('primary_champion_name'), 'executive_sponsor': pm.get('executive_sponsor'), 'csm': pm.get('csm_name'),
                          'attributes': pm.get('attributes') or {}}
    journey['evidence'] = evidence
    journey['open_review_count'] = open_review
    journey.update(origin_block(customer_id))
    journey['generated_at'] = j.updated_at.isoformat() if j.updated_at else None
    if compact:
        for k in ('episodes', 'phases', 'counterfactual_hooks', 'expected_path', 'features'):
            journey.pop(k, None)
        lvt = journey.get('leading_vs_trailing') or {}
        if lvt.get('series'):
            lvt['series'] = lvt['series'][-3:]
    return journey


def list_journeys(customer_id: int) -> List[dict]:
    from models import JourneyData, Account, QualitativeSignal
    rows = (JourneyData.query.filter_by(customer_id=int(customer_id))
            .join(Account, Account.account_id == JourneyData.account_id).add_entity(Account)
            .order_by(Account.account_name).all())
    from sqlalchemy import func
    from extensions import db
    open_by_acct = dict(db.session.query(QualitativeSignal.account_id, func.count(QualitativeSignal.id))
                        .filter_by(customer_id=int(customer_id), requires_review=True).group_by(QualitativeSignal.account_id).all())
    out = []
    for j, a in rows:
        jj = j.journey_json or {}
        series = (jj.get('leading_vs_trailing') or {}).get('series') or []
        latest = series[-1] if series else {}
        arc = jj.get('arc') or {}
        out.append({
            'account_id': a.account_id, 'account_name': a.account_name, 'revenue': float(a.revenue) if a.revenue is not None else None,
            'use_cases': (a.profile_metadata or {}).get('use_cases') or [],
            'contract_type': (a.profile_metadata or {}).get('contract_type'),
            'arc_type': arc.get('arc_type'), 'state': jj.get('state'), 'arc_confidence': arc.get('confidence'),
            'current_phase': jj.get('current_phase'), 'last_scored_month': jj.get('last_scored_month'),
            'live_months': len(jj.get('live_months') or []), 'last_evidence_at': jj.get('last_evidence_at'),
            'latest': {'month': latest.get('month'), 'kpi_only': latest.get('kpi_only'), 'qual': latest.get('qual'),
                       'early_warning': latest.get('early_warning'), 'roles': latest.get('roles')},
            'first_leading_warning_at': (jj.get('leading_vs_trailing') or {}).get('first_leading_warning_at'),
            'lead_days': (jj.get('leading_vs_trailing') or {}).get('lead_days'),
            'episodes': len(jj.get('episodes') or []), 'open_review_count': open_by_acct.get(a.account_id, 0),
            'updated_at': j.updated_at.isoformat() if j.updated_at else None,
        })
    return out
