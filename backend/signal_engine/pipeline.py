"""
Signal engine v2 — the ingestion → normalization → evidence pipeline.

One path, whatever the source, ending in a journey episode:

    ingest()            raw text or a structured event → QualitativeSignal
                        (system timestamp, source, content-hash dedup, participants)
    process_pending()   for each un-materialized signal:
                          structured?  its declared subtype is already a taxonomy
                                       subtype → role by rule, no LLM
                          free text?   enrichment.enrich_signal → intents →
                                       the first intent that resolves to a role
                          reconcile polarity (role wins; conflict flagged)
                          resolve people (roster / profile; unresolved kept, flagged)
                          write an OBSERVED SIGNAL ContextNode with a taxonomy
                          subtype, provenance and the person → journey v3

The old engine (Tier 1 port) wrote `node_subtype='qualitative_signal'` (a
vocabulary the journey cannot read), only when an intent had no context-
graph equivalent, with source='inferred' — and fused qualitative
components into pillar/composite health (retired: the journey's leading
composite is the only leading score; absolute-separation rule).

Everything here is framework-agnostic: the HTTP routes (signal_engine.http)
and the MCP tools call these functions inside an app context.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SOURCE_TYPES = ('manual', 'email', 'slack', 'transcript', 'ticket', 'crm_activity', 'meeting', 'external')
STRUCTURED_SOURCES = ('ticket', 'crm_activity', 'external')     # carry a category → subtype; no LLM needed
DEDUP_WINDOW_DAYS = 7
UNCLASSIFIED_SUBTYPE = 'unclassified_signal'


# ── ingest ─────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


def content_hash(account_id: int, text: str) -> str:
    return hashlib.sha256(f'{account_id}|{normalize_text(text)[:2000]}'.encode('utf-8')).hexdigest()


def _parse_when(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        v = value.strip().replace('Z', '+00:00')
        try:
            d = datetime.fromisoformat(v)
            return d.replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            pass
    return datetime.utcnow()


def ingest(customer_id: int, account_id: int, source_type: str, raw_text: str, *,
           occurred_at=None, participants: Optional[List[dict]] = None, signal_type: Optional[str] = None,
           source_ref: Optional[str] = None, consent_verified: Optional[bool] = None,
           metadata: Optional[dict] = None) -> dict:
    """Store one signal. Returns {'status': 'queued'|'duplicate', 'signal_id', ...}.

    `signal_type`, when given, must be a taxonomy subtype (structured path).
    `occurred_at` is the moment the event happened (system timestamp from
    the source); it defaults to now only for live, untimestamped input.
    """
    from extensions import db
    from models import Account, QualitativeSignal

    source_type = (source_type or 'manual').strip().lower()
    if source_type not in SOURCE_TYPES:
        raise ValueError(f'unknown source_type {source_type!r}; one of {SOURCE_TYPES}')
    raw_text = (raw_text or '').strip()
    if not raw_text:
        raise ValueError('raw_text is required')
    acct = db.session.get(Account, int(account_id))
    if not acct or acct.customer_id != int(customer_id):
        raise ValueError(f'account {account_id} does not belong to customer {customer_id}')
    if source_type == 'transcript' and not consent_verified:
        raise ValueError('transcript ingestion requires consent_verified=true')

    when = _parse_when(occurred_at)
    h = content_hash(acct.account_id, raw_text)
    dup = (QualitativeSignal.query
           .filter(QualitativeSignal.account_id == acct.account_id, QualitativeSignal.content_hash == h)
           .order_by(QualitativeSignal.id.desc()).first())
    if dup and dup.occurred_at and abs((when - dup.occurred_at).days) <= DEDUP_WINDOW_DAYS:
        logger.info('signal duplicate: account=%s hash=%s parent=%s', acct.account_id, h[:12], dup.signal_id)
        return {'status': 'duplicate', 'duplicate_of': dup.signal_id, 'account_id': acct.account_id,
                'customer_id': acct.customer_id, 'content_hash': h}

    signal_id = str(uuid.uuid4())
    sig = QualitativeSignal(
        signal_id=signal_id, customer_id=acct.customer_id, account_id=acct.account_id,
        signal_type=(signal_type or source_type), content=raw_text[:2000], sentiment='neutral',
        signal_date=when.date(), occurred_at=when, source_type=source_type, raw_text=raw_text,
        requires_review=False, consent_verified=bool(consent_verified) if consent_verified is not None else source_type != 'transcript',
        composite_signal_id=signal_id, stakeholder_roles=participants or None, content_hash=h,
        keywords=(source_ref or None),
    )
    db.session.add(sig)
    db.session.commit()
    try:
        from signal_engine.worker import notify_new_signal
        notify_new_signal()
    except Exception:
        pass
    logger.info('signal ingested: id=%s source=%s account=%s customer=%s at=%s',
                signal_id, source_type, acct.account_id, acct.customer_id, when.isoformat())
    return {'status': 'queued', 'signal_id': signal_id, 'account_id': acct.account_id,
            'customer_id': acct.customer_id, 'source_type': source_type, 'occurred_at': when.isoformat(),
            'structured': bool(signal_type), 'content_hash': h}


# ── people ─────────────────────────────────────────────────────────────

def resolve_person(customer_id: int, account_id: int, name_or_email: Optional[str], hint_role: Optional[str] = None) -> dict:
    """Match a name or email against the account's roster (STAKEHOLDER nodes
    and the profile's champion / sponsor / CSM). Unresolved people are kept,
    never dropped — the roster is what's incomplete, not the evidence."""
    from models import Account, ContextNode
    out = {'name': (name_or_email or '').strip() or None, 'title': hint_role, 'role': None, 'resolved': False}
    if not out['name']:
        return out
    key = out['name'].lower()
    is_email = '@' in key
    acct = Account.query.get(account_id) if hasattr(Account, 'query') else None
    pm = (acct.profile_metadata or {}) if acct else {}
    roster = [
        ('champion', pm.get('primary_champion_name'), pm.get('primary_champion_email'), pm.get('primary_champion_title')),
        ('executive_sponsor', pm.get('executive_sponsor'), pm.get('executive_sponsor_email'), 'Executive Sponsor'),
        ('csm', pm.get('csm_name'), pm.get('csm_email'), 'CSM'),
        ('cs_manager', pm.get('csm_manager'), pm.get('csm_manager_email'), 'CS Manager'),
    ]
    for role, nm, em, title in roster:
        if nm and (key == nm.lower() or (is_email and em and key == em.lower())):
            return {'name': nm, 'title': title, 'role': role, 'resolved': True}
        if not is_email and nm and key.split()[-1] == nm.lower().split()[-1] and key.split()[0][0] == nm.lower()[0]:
            return {'name': nm, 'title': title, 'role': role, 'resolved': True}
    for n in ContextNode.query.filter_by(account_id=account_id, node_type='STAKEHOLDER').all():
        nm = ((n.properties or {}).get('name') or n.title.split(' (')[0] or '').strip()
        if nm and key == nm.lower():
            return {'name': nm, 'title': (n.properties or {}).get('title') or n.title, 'role': n.node_subtype, 'resolved': True}
    return out


# ── classification ──────────────────────────────────────────────────────

def classify(sig, enrichment: dict, taxonomy) -> dict:
    """Pick the taxonomy subtype for a signal: its declared subtype if the
    taxonomy knows it (structured path), else the first enrichment intent
    that resolves to a role, else UNCLASSIFIED_SUBTYPE (role None — visible
    as unmapped, never silently dropped)."""
    declared = (sig.signal_type or '').strip().lower()
    if declared and taxonomy.signal_role(declared):
        return {'subtype': declared, 'role': taxonomy.signal_role(declared), 'basis': 'declared_subtype', 'intents': []}
    intents = [i for i in (enrichment.get('intent_signals') or []) if isinstance(i, str)]
    for i in intents:
        role = taxonomy.signal_role(i)
        if role:
            return {'subtype': i, 'role': role, 'basis': 'llm_intent', 'intents': intents}
    return {'subtype': UNCLASSIFIED_SUBTYPE, 'role': None, 'basis': 'unclassified', 'intents': intents}


def reconcile_sentiment(role_polarity: int, sentiment_score) -> tuple:
    """Role polarity wins over a contradicting sentiment; the conflict is
    flagged, and the raw value kept. Returns (score, conflict, raw)."""
    import utils.health_thresholds as ht
    defaults = ht.leading_indicator_config()['default_sentiment_by_polarity']
    raw = None
    try:
        raw = float(sentiment_score) if sentiment_score is not None else None
    except (TypeError, ValueError):
        raw = None
    if role_polarity == 0:
        return (raw if raw is not None else defaults['neutral']), False, raw
    default = defaults['negative'] if role_polarity < 0 else defaults['positive']
    if raw is None:
        return default, False, raw
    if (raw > 0 and role_polarity < 0) or (raw < 0 and role_polarity > 0):
        return default, True, raw
    return raw, False, raw


# ── materialize ─────────────────────────────────────────────────────────

def materialize(sig, enrichment: dict, taxonomy):
    """Write the OBSERVED SIGNAL node the journey reads. Returns the node."""
    from extensions import db
    from models import ContextNode

    cls = classify(sig, enrichment, taxonomy)
    pol = taxonomy.role_polarity(cls['role'])
    score, conflict, raw = reconcile_sentiment(pol, enrichment.get('sentiment_score', sig.sentiment_score))

    people = []
    for p in (sig.stakeholder_roles or []):
        nm = p.get('name') if isinstance(p, dict) else str(p)
        if nm and nm not in ('email_sender', 'slack_user'):
            people.append(resolve_person(sig.customer_id, sig.account_id, nm, (p.get('role') if isinstance(p, dict) else None)))
    for p in (enrichment.get('stakeholder_roles') or []):
        nm = p.get('name') if isinstance(p, dict) else None
        if nm and not any(x['name'] and x['name'].lower() == nm.lower() for x in people):
            people.append(resolve_person(sig.customer_id, sig.account_id, nm, p.get('role')))
    primary = next((p for p in people if p['resolved']), people[0] if people else None)

    when = sig.occurred_at or datetime.combine(sig.signal_date, datetime.min.time())
    props = {
        'signal_id': sig.signal_id, 'signal_ref': sig.signal_id,
        'sentiment': 'positive' if score > 0.1 else 'negative' if score < -0.1 else 'neutral',
        'sentiment_score': str(round(score, 2)), 'raw_sentiment_score': raw,
        'polarity_conflict': conflict, 'role': cls['role'], 'classification_basis': cls['basis'],
        'intents': cls['intents'], 'urgency_score': enrichment.get('urgency_score'),
        'escalation_probability': enrichment.get('escalation_probability'),
        'requires_review': bool(enrichment.get('requires_review')),
        'llm_model_version': enrichment.get('llm_model_version'),
        'people': people, 'source_type': sig.source_type, 'evidence_tier': 'observed',
    }
    if primary:
        props['stakeholder_name'] = primary['name']
        props['stakeholder_title'] = primary['title']
        props['stakeholder_role'] = primary['role']
        props['person_unresolved'] = not primary['resolved']
    node = ContextNode(
        customer_id=sig.customer_id, account_id=sig.account_id, node_type='SIGNAL', node_subtype=cls['subtype'],
        source='observed', title=(sig.content or cls['subtype'])[:200], properties=props, tier=2,
        occurred_at=when, source_platform=sig.source_type, source_event_id=sig.signal_id,
    )
    db.session.add(node)
    db.session.flush()
    sig.cg_node_id = node.node_id
    sig.sentiment = props['sentiment']
    sig.sentiment_score = round(score, 2)
    return node


# ── process ─────────────────────────────────────────────────────────────

def _apply_enrichment(sig, result: dict) -> None:
    for field in ('relationship_sentiment', 'product_sentiment', 'urgency_score', 'escalation_probability',
                  'intent_signals', 'stakeholder_roles', 'suggested_action', 'confidence',
                  'requires_review', 'llm_model_version'):
        if field in result and result[field] is not None:
            if field == 'stakeholder_roles' and sig.stakeholder_roles:
                continue   # the source's participants beat the model's guesses
            setattr(sig, field, result[field])
    try:
        from signal_engine.urgency import classify_structural_urgency, resolve_effective_urgency, AccountContext, SignalContext
        structural = classify_structural_urgency(
            SignalContext(intent_signals=result.get('intent_signals', []), health_delta=0,
                          llm_urgency_score=result.get('urgency_score', 0.5),
                          escalation_probability=result.get('escalation_probability', 0)),
            AccountContext(account_id=sig.account_id))
        sig.structural_urgency = structural
        sig.effective_urgency = resolve_effective_urgency(structural, result.get('urgency_score', 0.5),
                                                          result.get('escalation_probability', 0))
    except Exception as e:  # pragma: no cover
        logger.debug('urgency classification skipped: %s', e)


def process_pending(customer_id: Optional[int] = None, limit: int = 50, rebuild_journeys: bool = True) -> dict:
    """Enrich (if needed) and materialize every signal that has no node yet.
    Structured signals skip the LLM. Rebuilds the journeys of the accounts
    touched, so a new signal shows on the canvas immediately."""
    from extensions import db
    from models import QualitativeSignal
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_vertical_for_customer
    from signal_engine.enrichment import enrich_signal

    q = QualitativeSignal.query.filter(QualitativeSignal.source_type.isnot(None), QualitativeSignal.cg_node_id.is_(None))
    if customer_id is not None:
        q = q.filter(QualitativeSignal.customer_id == int(customer_id))
    sigs = q.order_by(QualitativeSignal.id).limit(limit).all()
    out = {'processed': 0, 'structured': 0, 'enriched': 0, 'unclassified': 0, 'duplicates_skipped': 0,
           'accounts': set(), 'signals': []}
    taxonomies: Dict[str, object] = {}
    for sig in sigs:
        try:
            vertical = get_vertical_for_customer(sig.customer_id)
            tax = taxonomies.setdefault(vertical, get_taxonomy(vertical))
            declared_known = bool(sig.signal_type and tax.signal_role(sig.signal_type))
            if declared_known:
                enrichment = {'sentiment_score': sig.sentiment_score, 'intent_signals': [],
                              'llm_model_version': 'structured_rule_map', 'requires_review': False}
                out['structured'] += 1
            else:
                if sig.intent_signals is None:
                    enrichment = enrich_signal(signal_id=sig.signal_id, raw_text=sig.raw_text or sig.content or '',
                                               account_id=sig.account_id, customer_id=sig.customer_id, vertical=vertical)
                    _apply_enrichment(sig, enrichment)
                    out['enriched'] += 1
                else:
                    enrichment = {'sentiment_score': sig.sentiment_score, 'intent_signals': sig.intent_signals or [],
                                  'stakeholder_roles': sig.stakeholder_roles, 'urgency_score': sig.urgency_score,
                                  'escalation_probability': sig.escalation_probability,
                                  'requires_review': sig.requires_review, 'llm_model_version': sig.llm_model_version}
            node = materialize(sig, enrichment, tax)
            db.session.commit()
            if node.node_subtype == UNCLASSIFIED_SUBTYPE:
                out['unclassified'] += 1
            out['processed'] += 1
            out['accounts'].add(sig.account_id)
            out['signals'].append({'signal_id': sig.signal_id, 'account_id': sig.account_id, 'node_id': node.node_id,
                                   'subtype': node.node_subtype, 'role': (node.properties or {}).get('role'),
                                   'basis': (node.properties or {}).get('classification_basis'),
                                   'polarity_conflict': (node.properties or {}).get('polarity_conflict'),
                                   'person': (node.properties or {}).get('stakeholder_name'),
                                   'person_unresolved': (node.properties or {}).get('person_unresolved')})
        except Exception as e:
            logger.warning('signal %s failed to process: %s', sig.signal_id, e, exc_info=True)
            db.session.rollback()

    out['journeys_rebuilt'] = 0
    if rebuild_journeys and out['accounts']:
        by_customer: Dict[int, set] = {}
        for sig in sigs:
            if sig.account_id in out['accounts']:
                by_customer.setdefault(sig.customer_id, set()).add(sig.account_id)
        from journeys.wizard_a import run_wizard_a
        for cid, aids in by_customer.items():
            try:
                res = run_wizard_a(cid, aids)
                out['journeys_rebuilt'] += res.get('processed', 0)
            except Exception as e:
                logger.warning('journey rebuild after signals failed for customer %s: %s', cid, e)
                db.session.rollback()
    out['accounts'] = sorted(out['accounts'])
    return out
