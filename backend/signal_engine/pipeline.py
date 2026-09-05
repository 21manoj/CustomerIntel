"""
Signal engine v2 — the ingestion → normalization → evidence pipeline.

One path, whatever the source, ending in a journey episode:

    ingest()            raw text or a structured event → QualitativeSignal
                        (system timestamp, source, content-hash dedup, participants)
    process_pending()   for each un-materialized signal:
                          structured?  its declared subtype is already a taxonomy
                                       subtype → role by rule, no LLM
                          free text?   enrichment.enrich_signal → a LIST of taxonomy-typed
                                       signals (tool-schema enum) → one node each
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
from datetime import datetime
from typing import Dict, List, Optional

from signal_engine import settings

logger = logging.getLogger(__name__)

SOURCE_TYPES = ('manual', 'email', 'slack', 'transcript', 'ticket', 'crm_activity', 'meeting', 'external', 'csv_import')
UNCLASSIFIED_SUBTYPE = 'unclassified_signal'


# ── ingest ─────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


def content_hash(account_id: int, text: str) -> str:
    return hashlib.sha256(f"{account_id}|{normalize_text(text)[:settings.get('dedup', 'hash_chars')]}".encode('utf-8')).hexdigest()


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
           signal_id: Optional[str] = None, sentiment_score=None, origin_platform: Optional[str] = None,
           use_case: Optional[str] = None) -> dict:
    """Store one signal. Returns {'status': 'queued'|'duplicate'|'exists', 'signal_id', ...}.

    `signal_type`, when given, must be a taxonomy subtype (structured path);
    an unknown one falls through to extraction. `occurred_at` is the moment
    the event happened; it defaults to now only for live, untimestamped input.
    `signal_id` / `sentiment_score` / `origin_platform` are for the structured
    lane (a typed CSV row, a CRM export): the row keeps its own id (so a
    re-upload is 'exists', not a second copy), its recorded sentiment, and
    the system it originally came from.
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

    if signal_id:
        prior = QualitativeSignal.query.filter_by(customer_id=acct.customer_id, signal_id=signal_id).first()
        if prior:
            return {'status': 'exists', 'signal_id': signal_id, 'account_id': acct.account_id, 'customer_id': acct.customer_id}
    when = _parse_when(occurred_at)
    h = content_hash(acct.account_id, raw_text)
    dup = (QualitativeSignal.query
           .filter(QualitativeSignal.account_id == acct.account_id, QualitativeSignal.content_hash == h)
           .order_by(QualitativeSignal.id.desc()).first())
    if dup and dup.occurred_at and abs((when - dup.occurred_at).days) <= settings.get('dedup', 'window_days'):
        logger.info('signal duplicate: account=%s hash=%s parent=%s', acct.account_id, h[:12], dup.signal_id)
        return {'status': 'duplicate', 'duplicate_of': dup.signal_id, 'account_id': acct.account_id,
                'customer_id': acct.customer_id, 'content_hash': h}

    signal_id = signal_id or str(uuid.uuid4())
    try:
        sentiment_score = float(sentiment_score) if sentiment_score not in (None, '') else None
    except (TypeError, ValueError):
        sentiment_score = None
    sig = QualitativeSignal(
        signal_id=signal_id, customer_id=acct.customer_id, account_id=acct.account_id,
        signal_type=(signal_type or source_type), content=raw_text[:settings.get('storage', 'content_chars')], sentiment='neutral',
        sentiment_score=sentiment_score,
        signal_date=when.date(), occurred_at=when, source_type=source_type, raw_text=raw_text,
        requires_review=False, consent_verified=bool(consent_verified) if consent_verified is not None else source_type != 'transcript',
        composite_signal_id=signal_id, stakeholder_roles=participants or None, content_hash=h,
        source_ref=(source_ref or None), keywords=(origin_platform or None), use_case=(use_case or None),
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

def account_use_cases(account_id: int) -> List[dict]:
    """The account's declared use cases (profile_metadata.use_cases), [] when none."""
    from extensions import db
    from models import Account
    acct = db.session.get(Account, account_id)
    uc = ((acct.profile_metadata or {}).get('use_cases') if acct else None) or []
    return [u if isinstance(u, dict) else {'name': str(u)} for u in uc]


def account_roster(customer_id: int, account_id: int) -> List[dict]:
    """Known people on the account: the profile's champion / sponsor / CSM /
    CS manager plus STAKEHOLDER nodes. [{name, title, role, email}]."""
    from extensions import db
    from models import Account, ContextNode
    acct = db.session.get(Account, account_id)
    pm = (acct.profile_metadata or {}) if acct else {}
    roster: List[dict] = []
    for role, nm, em, title in (
        ('champion', pm.get('primary_champion_name'), pm.get('primary_champion_email'), pm.get('primary_champion_title')),
        ('executive_sponsor', pm.get('executive_sponsor'), pm.get('executive_sponsor_email'), 'Executive Sponsor'),
        ('csm', pm.get('csm_name'), pm.get('csm_email'), 'CSM'),
        ('cs_manager', pm.get('csm_manager'), pm.get('csm_manager_email'), 'CS Manager'),
    ):
        if nm:
            roster.append({'name': nm, 'title': title, 'role': role, 'email': em})
    for n in ContextNode.query.filter_by(account_id=account_id, node_type='STAKEHOLDER').all():
        nm = ((n.properties or {}).get('name') or n.title.split(' (')[0] or '').strip()
        if nm and not any(r['name'].lower() == nm.lower() for r in roster):
            roster.append({'name': nm, 'title': (n.properties or {}).get('title') or n.title, 'role': n.node_subtype,
                           'email': (n.properties or {}).get('email')})
    return roster


def resolve_person(customer_id: int, account_id: int, name_or_email: Optional[str], hint_role: Optional[str] = None,
                   roster: Optional[List[dict]] = None, roster_role: Optional[str] = None) -> dict:
    """Match a name or email against the roster. Unresolved people are kept,
    never dropped — the roster is what's incomplete, not the evidence.
    `roster_role` is the model's own match (it saw the roster); trusted only
    when that role really is on the roster."""
    out = {'name': (name_or_email or '').strip() or None, 'title': hint_role, 'role': None, 'resolved': False}
    if not out['name']:
        return out
    roster = account_roster(customer_id, account_id) if roster is None else roster
    key = out['name'].lower()
    is_email = '@' in key
    if roster_role:
        hit = next((r for r in roster if r['role'] == roster_role), None)
        if hit:
            return {'name': hit['name'], 'title': hit['title'], 'role': hit['role'], 'resolved': True}
    for r in roster:
        nm = r['name'].lower()
        if key == nm or (is_email and r.get('email') and key == r['email'].lower()):
            return {'name': r['name'], 'title': r['title'], 'role': r['role'], 'resolved': True}
        if not is_email and key.split()[-1] == nm.split()[-1] and key.split()[0][0] == nm[0]:
            return {'name': r['name'], 'title': r['title'], 'role': r['role'], 'resolved': True}
    return out


# ── classification ──────────────────────────────────────────────────────

def extracted_items(sig, enrichment: dict, taxonomy) -> List[dict]:
    """The signals to write for one communication, in order:
      declared subtype the taxonomy knows      → one item, basis declared_subtype (structured path)
      model extraction (enrichment['signals']) → one item per signal, basis llm_extraction
      legacy intent list (pre-v2 rows)         → first intent that resolves, basis llm_intent
      nothing resolves                         → UNCLASSIFIED_SUBTYPE, role None (visible, never dropped)"""
    declared = (sig.signal_type or '').strip().lower()
    base = {'quote': None, 'sentiment_score': enrichment.get('sentiment_score', sig.sentiment_score),
            'urgency_score': enrichment.get('urgency_score'), 'escalation_probability': enrichment.get('escalation_probability'),
            'people': [], 'confidence': None}
    if declared and taxonomy.signal_role(declared):
        return [{**base, 'subtype': declared, 'role': taxonomy.signal_role(declared), 'basis': 'declared_subtype', 'intents': []}]
    items = [{**base, **s, 'role': taxonomy.signal_role(s.get('subtype')), 'basis': 'llm_extraction', 'intents': []}
             for s in (enrichment.get('signals') or []) if isinstance(s, dict) and taxonomy.signal_role(s.get('subtype'))]
    if items:
        return items
    intents = [i for i in (enrichment.get('intent_signals') or []) if isinstance(i, str)]
    for i in intents:
        role = taxonomy.signal_role(i)
        if role:
            return [{**base, 'subtype': i, 'role': role, 'basis': 'llm_intent', 'intents': intents}]
    return [{**base, 'subtype': UNCLASSIFIED_SUBTYPE, 'role': None, 'basis': 'unclassified', 'intents': intents}]


def classify(sig, enrichment: dict, taxonomy) -> dict:
    """Primary classification of a communication (its first extracted item)."""
    it = extracted_items(sig, enrichment, taxonomy)[0]
    return {'subtype': it['subtype'], 'role': it['role'], 'basis': it['basis'], 'intents': it['intents']}


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

def _write_node(sig, item: dict, enrichment: dict, taxonomy, roster: List[dict], participants: List[dict]):
    from extensions import db
    from models import ContextNode
    from signal_engine.urgency import classify_structural_urgency, resolve_effective_urgency

    pol = taxonomy.role_polarity(item['role'])
    score, conflict, raw = reconcile_sentiment(pol, item.get('sentiment_score'))
    own = []
    for p in (item.get('people') or []):
        nm = p.get('name')
        if nm and not any(x['name'] and x['name'].lower() == nm.lower() for x in own):
            own.append(resolve_person(sig.customer_id, sig.account_id, nm, p.get('title'), roster, p.get('roster_role')))
    people = own + [p for p in participants if not any(x['name'] and p['name'] and x['name'].lower() == p['name'].lower() for x in own)]
    # the signal's own people come first; a roster match among them wins, an
    # unresolved new face is still the subject if it is all the signal names
    primary = next((p for p in own if p['resolved']), own[0] if own else
                   next((p for p in participants if p['resolved']), participants[0] if participants else None))
    structural = classify_structural_urgency(item['role'])
    effective = resolve_effective_urgency(structural, item.get('urgency_score'), item.get('escalation_probability'))

    band = settings.get('storage', 'sentiment_label_band')
    when = sig.occurred_at or datetime.combine(sig.signal_date, datetime.min.time())
    props = {
        'signal_id': sig.signal_id, 'signal_ref': sig.source_ref or sig.signal_id,
        'origin_platform': sig.keywords,          # the system a structured row came from (crm, gainsight …); None for live sources
        'sentiment': 'positive' if score > band else 'negative' if score < -band else 'neutral',
        'sentiment_score': str(round(score, 2)), 'raw_sentiment_score': raw,
        'polarity_conflict': conflict, 'role': item['role'], 'classification_basis': item['basis'],
        'intents': item.get('intents') or [], 'quote': item.get('quote'), 'confidence': item.get('confidence'),
        'urgency_score': item.get('urgency_score'), 'escalation_probability': item.get('escalation_probability'),
        'structural_urgency': structural, 'effective_urgency': effective,
        'requires_review': bool(enrichment.get('requires_review')),
        'llm_model_version': enrichment.get('llm_model_version'),
        'people': people, 'source_type': sig.source_type, 'evidence_tier': 'observed', 'use_case': sig.use_case,
    }
    if primary:
        props['stakeholder_name'] = primary['name']
        props['stakeholder_title'] = primary['title']
        props['stakeholder_role'] = primary['role']
        props['person_unresolved'] = not primary['resolved']
    node = ContextNode(
        customer_id=sig.customer_id, account_id=sig.account_id, node_type='SIGNAL', node_subtype=item['subtype'],
        source='observed', title=(item.get('quote') or sig.content or item['subtype'])[:200], properties=props, tier=2,
        occurred_at=when, source_platform=sig.source_type,
        # provenance: the event's id in its own system when the source gave one (ticket id, CSV signal_ref),
        # else our signal id; properties.signal_id always reaches the signal row
        source_event_id=(sig.source_ref or sig.signal_id), source_ref=sig.source_ref,
    )
    db.session.add(node)
    db.session.flush()
    return node, score, effective


def materialize(sig, enrichment: dict, taxonomy) -> list:
    """Write one OBSERVED SIGNAL node per extracted signal (a structured
    signal is exactly one). Returns the nodes; the first is primary and is
    what sig.cg_node_id points to."""
    roster = account_roster(sig.customer_id, sig.account_id)
    participants = []
    for p in (sig.stakeholder_roles or []):
        nm = p.get('name') if isinstance(p, dict) else str(p)
        if nm and nm not in ('email_sender', 'slack_user'):
            participants.append(resolve_person(sig.customer_id, sig.account_id, nm,
                                               (p.get('role') if isinstance(p, dict) else None), roster,
                                               (p.get('roster_role') if isinstance(p, dict) else None)))
    items = extracted_items(sig, enrichment, taxonomy)
    nodes = []
    for item in items:
        node, score, effective = _write_node(sig, item, enrichment, taxonomy, roster, participants)
        nodes.append(node)
    first = nodes[0]
    sig.cg_node_id = first.node_id
    sig.sentiment = first.properties['sentiment']
    if sig.sentiment_score is None and enrichment.get('signals'):
        sig.sentiment_score = float(first.properties['sentiment_score'])   # the row keeps what its source recorded; the node carries the reconciled value
    sig.structural_urgency = first.properties['structural_urgency']
    sig.effective_urgency = max((n.properties['effective_urgency'] for n in nodes),
                                key=lambda lvl: ('low', 'medium', 'high', 'critical').index(lvl))
    if enrichment.get('signals'):
        sig.extractions = enrichment['signals']
    return nodes


# ── process ─────────────────────────────────────────────────────────────

def _apply_enrichment(sig, result: dict) -> None:
    """Copy the LLM's fields onto the signal row (urgency is set in materialize, for both paths)."""
    for field in ('urgency_score', 'escalation_probability', 'intent_signals',
                  'suggested_action', 'confidence', 'requires_review', 'llm_model_version'):
        if field in result and result[field] is not None:
            setattr(sig, field, result[field])
    # stakeholder_roles stays what the SOURCE declared (participants of the whole
    # communication). People the model found belong to their own signal — they
    # live in `extractions` and go onto that signal's node only.


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
    out = {'processed': 0, 'structured': 0, 'enriched': 0, 'unclassified': 0, 'nodes_written': 0, 'errors': 0,
           'accounts': set(), 'signals': [], 'error_signals': []}
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
                                               account_id=sig.account_id, customer_id=sig.customer_id, vertical=vertical,
                                               taxonomy=tax, roster=account_roster(sig.customer_id, sig.account_id),
                                               use_cases=account_use_cases(sig.account_id))
                    if enrichment.get('error'):
                        # not evidence — leave it queued for the next pass, say why
                        out['errors'] += 1
                        out['error_signals'].append({'signal_id': sig.signal_id, 'error': enrichment['error']})
                        sig.suggested_action = enrichment.get('suggested_action')
                        db.session.commit()
                        continue
                    _apply_enrichment(sig, enrichment)
                    out['enriched'] += 1
                else:
                    enrichment = {'sentiment_score': sig.sentiment_score, 'intent_signals': sig.intent_signals or [],
                                  'signals': sig.extractions or [],
                                  'stakeholder_roles': sig.stakeholder_roles, 'urgency_score': sig.urgency_score,
                                  'escalation_probability': sig.escalation_probability,
                                  'requires_review': sig.requires_review, 'llm_model_version': sig.llm_model_version}
            nodes = materialize(sig, enrichment, tax)
            db.session.commit()
            node = nodes[0]
            if node.node_subtype == UNCLASSIFIED_SUBTYPE:
                out['unclassified'] += 1
            out['processed'] += 1
            out['nodes_written'] += len(nodes)
            out['accounts'].add(sig.account_id)
            out['signals'].append({'signal_id': sig.signal_id, 'account_id': sig.account_id, 'node_id': node.node_id,
                                   'node_ids': [n.node_id for n in nodes], 'subtypes': [n.node_subtype for n in nodes],
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


# ── bulk import ─────────────────────────────────────────────────────────

IMPORT_BATCH_MAX = 500


def resolve_account_ref(customer_id: int, item: dict):
    """external id → account id → name (case-insensitive); None when nothing matches."""
    from models import Account
    q = Account.query.filter_by(customer_id=int(customer_id))
    ext = item.get('source_account_id') or item.get('external_account_id')
    if ext:
        a = q.filter_by(external_account_id=str(ext)).first()
        if a:
            return a
    if item.get('account_id'):
        a = q.filter_by(account_id=int(item['account_id'])).first()
        if a:
            return a
    name = item.get('account_name')
    if name:
        a = q.filter(Account.account_name.ilike(str(name))).first()
        if a:
            return a
    return None


def import_communications(customer_id: int, communications: list, process_now: bool = True) -> dict:
    """The bulk lane for raw communications (a customer's export, the
    generator's communications.jsonl): every item goes through ingest();
    duplicates and unknown accounts are reported, not dropped."""
    if not isinstance(communications, list):
        raise ValueError('communications must be a list')
    if len(communications) > IMPORT_BATCH_MAX:
        raise ValueError(f'at most {IMPORT_BATCH_MAX} communications per call (got {len(communications)})')
    out = {'received': len(communications), 'queued': 0, 'duplicates': 0, 'unknown_accounts': [], 'rejected': [], 'signal_ids': [], 'by_ref': {}}
    for i, item in enumerate(communications):
        if not isinstance(item, dict):
            out['rejected'].append({'index': i, 'error': 'not an object'})
            continue
        acct = resolve_account_ref(customer_id, item)
        if acct is None:
            out['unknown_accounts'].append({'index': i, 'ref': item.get('source_account_id') or item.get('account_id') or item.get('account_name')})
            continue
        try:
            r = ingest(customer_id, acct.account_id, item.get('source_type') or 'manual', item.get('text') or item.get('raw_text') or '',
                       occurred_at=item.get('occurred_at'), participants=item.get('participants'), signal_type=item.get('signal_type'),
                       source_ref=item.get('source_ref') or item.get('ref'), consent_verified=item.get('consent_verified'),
                       use_case=item.get('use_case'))
        except ValueError as e:
            out['rejected'].append({'index': i, 'error': str(e)})
            continue
        ref = item.get('source_ref') or item.get('ref')
        if r['status'] == 'queued':
            out['queued'] += 1
            out['signal_ids'].append(r['signal_id'])
            if ref:
                out['by_ref'][str(ref)] = r['signal_id']
        else:
            out['duplicates'] += 1
            if ref and r.get('duplicate_of'):
                out['by_ref'][str(ref)] = r['duplicate_of']
    if process_now and out['queued']:
        totals = {'processed': 0, 'nodes_written': 0, 'unclassified': 0, 'errors': 0, 'journeys_rebuilt': 0}
        while True:
            res = process_pending(customer_id=customer_id, limit=200, rebuild_journeys=True)
            for k in totals:
                totals[k] += res.get(k, 0)
            if not res['processed']:
                break
        out['processed'] = totals
    return out
