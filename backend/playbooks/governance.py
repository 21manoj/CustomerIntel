"""
Interventions — propose, approve+send, report, list (design §3–§7).

    evaluate(customer_id, account_id=None, dry_run=False)        journey evidence → proposed rows (cited)
    approve(customer_id, intervention_id, note=None)              human/policy approval → signed webhook → INTERVENTION node → 'sent'
    report(customer_id, intervention_id, state, ...)              the external workflow's callback → 'closed' (+ outcome)
    list_interventions(customer_id, account_id=None, state=None)  the read, with stuck ones and the per-playbook numbers

Rules kept: every proposal cites episode ids; a human approves anything
beyond a notification; every state change is a tool_audit_log row carrying
the key; the INTERVENTION node is written when the payload is sent (an
engine that never calls back is itself a finding); realized $ and exposure
$ are two numbers, never summed; nothing here carries its own number
(config/playbook_governance.json).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

NODE_TYPE = 'INTERVENTION'
SOURCE_PLATFORM = 'playbook'
CREATED_BY = 'approve_intervention'
AUDIT_SURFACE = 'playbook'
SYSTEM_ACTOR = {'key_kind': 'system', 'key_record': None, 'key_id': None, 'label': 'system:journey_rebuild'}


# ── who ───────────────────────────────────────────────────────────────

def current_actor() -> dict:
    """The caller as the auth layer sees it: server key, a customer key (with its id), or the local process."""
    from mcp_server.auth import extract_api_key, validate_server_key, validate_customer_key
    if os.environ.get('MCP_TRANSPORT', 'stdio') != 'http':
        return {'key_kind': 'local', 'key_record': None, 'key_id': None, 'label': 'local'}
    raw = extract_api_key()
    if not raw:
        return {'key_kind': 'none', 'key_record': None, 'key_id': None, 'label': 'anonymous'}
    if validate_server_key(raw):
        return {'key_kind': 'server', 'key_record': None, 'key_id': None, 'label': 'server_key'}
    rec = validate_customer_key(raw)
    if rec:
        return {'key_kind': 'customer', 'key_record': rec, 'key_id': rec.id, 'label': f'key:{rec.key_prefix}'}
    return {'key_kind': 'none', 'key_record': None, 'key_id': None, 'label': 'invalid_key'}


def _audit(customer_id: int, transition: str, actor: dict, detail: str) -> None:
    from mcp_server import audit
    audit.record(AUDIT_SURFACE, f'intervention.{transition}', customer_id, key_kind=actor['key_kind'],
                 key_record=actor.get('key_record'), outcome='allowed', detail=detail)


def _note(row, actor: dict, transition: str, note: Optional[str]) -> None:
    notes = list(row.notes or [])
    notes.append({'at': datetime.utcnow().isoformat(), 'by': actor['label'], 'transition': transition,
                  'note': (note or '').strip() or None})
    row.notes = notes


# ── evaluate ──────────────────────────────────────────────────────────

def _trigger_key(episode_ids: List[str]) -> str:
    return hashlib.sha256(','.join(sorted(episode_ids)).encode('utf-8')).hexdigest()


def _urgency_rank(level: Optional[str]) -> int:
    from signal_engine.urgency import LEVELS
    return LEVELS.index(level) if level in LEVELS else -1


def _node_urgencies(node_ids: List[int]) -> Dict[int, Optional[str]]:
    from models import ContextNode
    if not node_ids:
        return {}
    return {n.node_id: (n.properties or {}).get('effective_urgency')
            for n in ContextNode.query.filter(ContextNode.node_id.in_(node_ids)).all()}


def _match(playbook: dict, journey: dict, urgencies_cache: dict) -> tuple:
    """(fires, reason, cited_episodes, max_urgency). Reads the latest leading month's cited evidence."""
    from signal_engine.urgency import classify_structural_urgency
    trig = playbook['trigger']
    series = (journey.get('leading_vs_trailing') or {}).get('series') or []
    latest = series[-1] if series else None
    if not latest or not latest.get('contributing_episode_ids'):
        return False, 'no_recent_evidence', [], None
    by_id = {e['episode_id']: e for e in journey.get('episodes', [])}
    present = set((latest.get('roles') or {}).keys())
    want = set(trig['roles'])
    if trig['roles_match'] == 'all' and not want <= present:
        return False, f'roles_missing:{sorted(want - present)}', [], None
    if trig['roles_match'] == 'any' and not (want & present):
        return False, 'roles_absent', [], None
    cited = [by_id[i] for i in latest['contributing_episode_ids'] if i in by_id and by_id[i].get('role') in want]
    if not cited:
        return False, 'roles_absent', [], None
    node_ids = [nid for e in cited for nid in (e.get('evidence_node_ids') or [])]
    missing = [n for n in node_ids if n not in urgencies_cache]
    if missing:
        urgencies_cache.update(_node_urgencies(missing))
    levels = []
    for e in cited:
        for nid in e.get('evidence_node_ids') or []:
            lvl = urgencies_cache.get(nid) or classify_structural_urgency(e.get('role')) or 'low'
            levels.append(lvl)
    max_lvl = max(levels, key=_urgency_rank) if levels else None
    floor = trig.get('urgency_floor')
    if floor and _urgency_rank(max_lvl) < _urgency_rank(floor):
        return False, f'urgency_below_floor:{max_lvl}<{floor}', cited, max_lvl
    rwd = trig.get('renewal_within_days')
    if rwd is not None:
        days = (journey.get('features') or {}).get('days_to_renewal')
        if days is None:
            return False, 'no_renewal_date', cited, max_lvl
        if days > rwd:
            return False, f'renewal_beyond_window:{days}>{rwd}', cited, max_lvl
    return True, 'fires', cited, max_lvl


def evaluate(customer_id: int, account_id: Optional[int] = None, dry_run: bool = False,
             actor: Optional[dict] = None) -> dict:
    """Propose interventions from the journeys' latest leading month. Idempotent per
    (account, playbook, trigger set); one open proposal per (account, playbook); nothing
    within window_days of a closed one. dry_run returns what it would propose."""
    from extensions import db
    from models import Account, JourneyData, Intervention
    from playbooks.definitions import playbooks_for_customer, governance
    actor = actor or current_actor()
    defs = playbooks_for_customer(customer_id)
    cfg = defs['tenant']
    out = {'customer_id': int(customer_id), 'vertical': defs['vertical'], 'dry_run': bool(dry_run),
           'playbooks_considered': [p['id'] for p in defs['playbooks']], 'disabled': defs['disabled'],
           'proposed': [], 'auto_approved': [], 'skipped': [], 'accounts_evaluated': 0, 'status': 'evaluated'}
    if cfg['kill_switch']:
        out['status'] = 'disabled'
        out['note'] = 'kill switch is on for this tenant: no evaluation, no sends (configure_playbooks kill_switch=false)'
        return out
    if not defs['playbooks']:
        out['status'] = 'no_playbooks'
        out['note'] = defs.get('note') or 'every playbook for this vertical is switched off'
        return out
    q = JourneyData.query.filter_by(customer_id=int(customer_id))
    if account_id is not None:
        q = q.filter_by(account_id=int(account_id))
    now = datetime.utcnow()
    urgencies: dict = {}
    for jd in q.order_by(JourneyData.account_id).all():
        out['accounts_evaluated'] += 1
        acct = db.session.get(Account, jd.account_id)
        journey = jd.journey_json or {}
        as_of = journey.get('as_of')
        for pb in defs['playbooks']:
            fires, reason, cited, urgency = _match(pb, journey, urgencies)
            if not fires:
                if reason != 'no_recent_evidence':
                    out['skipped'].append({'account_id': jd.account_id, 'playbook_id': pb['id'], 'reason': reason})
                continue
            episode_ids = sorted(e['episode_id'] for e in cited)
            key = _trigger_key(episode_ids)
            prior = Intervention.query.filter_by(account_id=jd.account_id, playbook_id=pb['id'], trigger_key=key).first()
            if prior:
                out['skipped'].append({'account_id': jd.account_id, 'playbook_id': pb['id'], 'reason': 'exists', 'intervention_id': prior.id})
                continue
            open_row = Intervention.query.filter(Intervention.account_id == jd.account_id, Intervention.playbook_id == pb['id'],
                                                 Intervention.state != 'closed').first()
            if open_row:
                out['skipped'].append({'account_id': jd.account_id, 'playbook_id': pb['id'], 'reason': 'open', 'intervention_id': open_row.id})
                continue
            since = now - timedelta(days=int(pb['expected_outcome']['window_days']))
            recent = Intervention.query.filter(Intervention.account_id == jd.account_id, Intervention.playbook_id == pb['id'],
                                               Intervention.state == 'closed', Intervention.closed_at >= since).first()
            if recent:
                out['skipped'].append({'account_id': jd.account_id, 'playbook_id': pb['id'], 'reason': 'suppressed_recent_close',
                                       'intervention_id': recent.id})
                continue
            first = cited[0]
            quote = ((first.get('meta') or {}).get('quote') or first.get('title') or '').strip()
            proposal = {
                'account_id': jd.account_id, 'account_name': acct.account_name if acct else None, 'playbook_id': pb['id'],
                'label': pb['label'], 'action_class': pb['action_class'], 'approval': pb['approval'], 'urgency': urgency,
                'trigger_episode_ids': episode_ids, 'trigger_quote': quote,
                'trigger_roles': sorted({e.get('role') for e in cited if e.get('role')}),
                'expected_outcome': pb['expected_outcome'], 'exposure_revenue': float(acct.revenue) if acct and acct.revenue is not None else None,
                'evaluated_as_of': as_of,
            }
            if dry_run:
                out['proposed'].append(proposal)
                continue
            row = Intervention(
                customer_id=int(customer_id), account_id=jd.account_id, playbook_id=pb['id'], playbook_version=defs['version'],
                action_class=pb['action_class'], approval_mode=pb['approval'], state='proposed', urgency=urgency,
                trigger_key=key, trigger_episode_ids=episode_ids,
                trigger_node_ids=sorted({nid for e in cited for nid in (e.get('evidence_node_ids') or [])}),
                trigger_roles=proposal['trigger_roles'], trigger_quote=quote[:2000] or None,
                evaluated_as_of=datetime.fromisoformat(as_of) if as_of else None,
                expected_outcome_types=list(pb['expected_outcome']['types']), expected_window_days=int(pb['expected_outcome']['window_days']),
                exposure_revenue=proposal['exposure_revenue'], proposed_at=now, proposed_by=actor['label'], notes=[],
            )
            db.session.add(row)
            db.session.commit()
            _audit(customer_id, 'propose', actor,
                   f'#{row.id} {pb["id"]} account {jd.account_id} urgency={urgency} cites {",".join(episode_ids)[:120]}')
            proposal['intervention_id'] = row.id
            out['proposed'].append(proposal)
            if pb['approval'] == 'auto' and cfg['automation_level'] >= 1:
                policy = {'key_kind': 'system', 'key_record': None, 'key_id': None,
                          'label': f"policy:automation_level_{cfg['automation_level']}"}
                res = approve(customer_id, row.id, note='approved by policy (approval=auto, automation_level>=1)', actor=policy)
                out['auto_approved'].append({'intervention_id': row.id, 'delivery': res.get('delivery')})
    logger.info('evaluate_playbooks customer=%s accounts=%d proposed=%d auto=%d skipped=%d dry_run=%s',
                customer_id, out['accounts_evaluated'], len(out['proposed']), len(out['auto_approved']), len(out['skipped']), dry_run)
    return out


def evaluate_after_rebuild(customer_id: int, account_ids) -> Optional[dict]:
    """The hook journeys.wizard_a calls after it commits. Never raises; off via config."""
    from playbooks.definitions import governance
    if not governance().get('evaluate_on_journey_rebuild', False):
        return None
    try:
        results = None
        for aid in sorted(set(account_ids or [])):
            r = evaluate(customer_id, aid, actor=SYSTEM_ACTOR)
            if results is None:
                results = r
            else:
                for k in ('proposed', 'auto_approved', 'skipped'):
                    results[k].extend(r[k])
                results['accounts_evaluated'] += r['accounts_evaluated']
            if r['status'] in ('disabled', 'no_playbooks'):
                break
        return results
    except Exception as e:  # pragma: no cover — must never break the rebuild
        logger.warning('evaluate_playbooks after journey rebuild failed for customer %s: %s', customer_id, e, exc_info=True)
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass
        return None


# ── approve + send ────────────────────────────────────────────────────

def _get(customer_id: int, intervention_id: int):
    from extensions import db
    from models import Intervention
    row = db.session.get(Intervention, int(intervention_id))
    if not row or int(row.customer_id) != int(customer_id):
        raise ValueError(f'intervention {intervention_id} not found for customer {customer_id}')
    return row


def _playbook_def(row) -> dict:
    from playbooks.definitions import load_vertical
    from utils.vertical_registry import get_vertical_for_customer
    for p in load_vertical(get_vertical_for_customer(row.customer_id))['playbooks']:
        if p['id'] == row.playbook_id:
            return p
    return {'id': row.playbook_id, 'label': row.playbook_id.replace('_', ' '), 'action_class': row.action_class}


def _triggers(row) -> List[dict]:
    """The cited evidence as the payload and the node carry it: episode id, node id, role, subtype, quote, when."""
    from models import JourneyData
    jd = JourneyData.query.filter_by(customer_id=row.customer_id, account_id=row.account_id).first()
    by_id = {e['episode_id']: e for e in ((jd.journey_json or {}).get('episodes') or [])} if jd else {}
    out = []
    for eid in row.trigger_episode_ids or []:
        e = by_id.get(eid) or {}
        nids = e.get('evidence_node_ids') or []
        out.append({'episode_id': eid, 'node_id': nids[0] if nids else None, 'role': e.get('role'), 'subtype': e.get('subtype'),
                    'quote': (e.get('meta') or {}).get('quote') or e.get('title'), 'occurred_at': e.get('date')})
    return out


def approve(customer_id: int, intervention_id: int, note: Optional[str] = None, actor: Optional[dict] = None) -> dict:
    """proposed → approved → (send) → sent. Writes the INTERVENTION node and the LED_TO edges from the cited
    evidence when the payload is sent — also when delivery fails or nothing is configured: the approved
    intervention must be visible on the journey, and so must the delivery problem."""
    from extensions import db
    from models import Account, Customer, ContextNode, ContextEdge
    from playbooks.definitions import tenant_config, tenant_secret
    from playbooks import webhook
    from utils.data_origin import block
    actor = actor or current_actor()
    row = _get(customer_id, intervention_id)
    if row.state != 'proposed':
        raise ValueError(f'intervention {row.id} is {row.state}, only a proposed one can be approved')
    cfg = tenant_config(customer_id)
    if cfg['kill_switch']:
        raise ValueError('kill switch is on for this tenant: nothing is approved or sent')
    now = datetime.utcnow()
    row.state, row.approved_at, row.approved_by, row.approved_by_key_id = 'approved', now, actor['label'], actor.get('key_id')
    _note(row, actor, 'approve', note)
    db.session.commit()
    _audit(customer_id, 'approve', actor, f'#{row.id} {row.playbook_id} account {row.account_id} by {actor["label"]}')

    acct = db.session.get(Account, row.account_id)
    cust = db.session.get(Customer, row.customer_id)
    triggers = _triggers(row)
    pb = _playbook_def(row)
    payload = webhook.build_payload(row, acct, cust, triggers, actor['label'], block(cust))
    delivery = webhook.deliver(cfg.get('webhook_url'), tenant_secret(customer_id), payload)
    row.sent_at = datetime.utcnow()
    row.delivery = delivery
    row.state = 'sent'

    node = ContextNode(
        customer_id=row.customer_id, account_id=row.account_id, node_type=NODE_TYPE, node_subtype=row.playbook_id,
        source='observed', tier=1, title=f"{pb['label']} — {acct.account_name}"[:500],
        properties={'intervention_id': row.id, 'playbook_id': row.playbook_id, 'playbook_version': row.playbook_version,
                    'action_class': row.action_class, 'approved_by': row.approved_by, 'approved_at': row.approved_at.isoformat(),
                    'trigger_episode_ids': list(row.trigger_episode_ids or []), 'trigger_quote': row.trigger_quote,
                    'urgency': row.urgency, 'delivery_status': delivery['status'], 'delivery_error': delivery.get('error'),
                    'delivery_host': delivery.get('url_host'), 'evidence_tier': 'observed', 'closed_state': None},
        confidence=1.0, source_platform=SOURCE_PLATFORM, source_event_id=f'intervention:{row.id}', occurred_at=row.sent_at,
    )
    db.session.add(node)
    db.session.flush()
    row.node_id = node.node_id
    for t in triggers:
        if t.get('node_id'):
            db.session.add(ContextEdge(
                customer_id=row.customer_id, from_node_id=int(t['node_id']), to_node_id=node.node_id, edge_type='LED_TO',
                weight=1.0, confidence=1.0, source_platform=SOURCE_PLATFORM, created_by=CREATED_BY,
                properties={'evidence': f'cited by intervention #{row.id} ({row.playbook_id})', 'evidence_tier': 'observed',
                            'derivation': 'playbook_trigger', 'episode_id': t['episode_id']},
            ))
    db.session.commit()
    _audit(customer_id, 'send', actor, f'#{row.id} {row.playbook_id} → {delivery["status"]} host={delivery.get("url_host")} '
                                        f'attempts={delivery["attempts"]} node={node.node_id}' + (f' error={delivery["error"][:80]}' if delivery.get('error') else ''))
    rebuilt = _rebuild(row)
    return {**row_view(row), 'payload': payload, 'journeys_rebuilt': rebuilt}


def _rebuild(row) -> int:
    from extensions import db
    from journeys.wizard_a import run_wizard_a
    try:
        return run_wizard_a(row.customer_id, [row.account_id], evaluate_playbooks=False).get('processed', 0)
    except Exception as e:  # pragma: no cover
        logger.warning('journey rebuild after intervention #%s failed: %s', row.id, e)
        db.session.rollback()
        return 0


# ── report (the callback) ─────────────────────────────────────────────

def report(customer_id: int, intervention_id: int, state: str, note: Optional[str] = None, outcome_type: Optional[str] = None,
           outcome_date=None, revenue=None, actor: Optional[dict] = None) -> dict:
    """What the external workflow calls back with: started (informational), done, failed, cancelled.
    cancelled on a proposed row is a human declining it. An outcome, if given, goes through log_outcome
    and is linked to the INTERVENTION node."""
    from extensions import db
    from models import ContextNode, ContextEdge
    from playbooks.definitions import governance
    actor = actor or current_actor()
    gov = governance()
    state = (state or '').strip().lower()
    if state not in gov['report_states']:
        raise ValueError(f"state must be one of {gov['report_states']}")
    row = _get(customer_id, intervention_id)
    now = datetime.utcnow()
    if state == 'started':
        if row.state != 'sent':
            raise ValueError(f'intervention {row.id} is {row.state}; only a sent one can start')
        row.started_at = row.started_at or now
        row.last_report_at = now
        _note(row, actor, 'started', note)
        db.session.commit()
        _audit(customer_id, 'started', actor, f'#{row.id} {row.playbook_id} started (reported by {actor["label"]})')
        return row_view(row)
    if row.state == 'closed':
        raise ValueError(f'intervention {row.id} is already closed ({row.closed_state})')
    if state in ('done', 'failed') and row.state != 'sent':
        raise ValueError(f'intervention {row.id} is {row.state}; only a sent one can be reported {state}')
    if state == 'cancelled' and row.state not in ('proposed', 'sent'):
        raise ValueError(f'intervention {row.id} is {row.state}; cannot cancel')

    outcome = None
    if outcome_type:
        from journeys.outcomes import log_outcome
        res = log_outcome(int(customer_id), row.account_id, outcome_type, outcome_date or now, revenue=revenue,
                          note=note or f'reported by the {row.playbook_id} workflow (intervention #{row.id})',
                          linked_signal_ids=[str(n) for n in (row.trigger_node_ids or [])], decided_by=f'intervention:{row.id}',
                          source_type=SOURCE_PLATFORM, source_ref=f'intervention:{row.id}:outcome', rebuild=False)
        row.outcome_node_id = res['node_id']
        when = datetime.fromisoformat(res['occurred_at'])
        row.outcome_in_window = bool(row.sent_at and row.sent_at.date() <= when.date() <= (row.sent_at + timedelta(days=row.expected_window_days)).date())
        row.outcome_expected = outcome_type.strip().lower() in set(row.expected_outcome_types or [])
        already = ContextEdge.query.filter_by(from_node_id=row.node_id, to_node_id=res['node_id'], edge_type='LED_TO').first() if row.node_id else None
        if row.node_id and not already:      # an outcome that was already on record (status 'exists') is linked too: the workflow named it
            db.session.add(ContextEdge(
                customer_id=row.customer_id, from_node_id=row.node_id, to_node_id=res['node_id'], edge_type='LED_TO',
                weight=1.0, confidence=1.0, source_platform=SOURCE_PLATFORM, created_by='report_intervention',
                properties={'evidence': f'outcome reported for intervention #{row.id} ({state})', 'evidence_tier': 'observed',
                            'derivation': 'playbook_report'},
            ))
        outcome = {'node_id': res['node_id'], 'status': res['status'], 'outcome_type': res['outcome_type'], 'bucket': res.get('bucket'),
                   'revenue': res.get('revenue'), 'in_window': row.outcome_in_window, 'expected': row.outcome_expected}
    row.state, row.closed_at, row.closed_state, row.closed_by, row.last_report_at = 'closed', now, state, actor['label'], now
    was_proposed = row.sent_at is None
    _note(row, actor, state, note)
    if row.node_id:
        node = db.session.get(ContextNode, row.node_id)
        if node:
            node.properties = {**(node.properties or {}), 'closed_state': state, 'closed_at': now.isoformat(),
                               'outcome_node_id': row.outcome_node_id, 'reported_by': actor['label']}
    db.session.commit()
    _audit(customer_id, 'declined' if (state == 'cancelled' and was_proposed) else state, actor,
           f'#{row.id} {row.playbook_id} {state} by {actor["label"]}' + (f' outcome node {row.outcome_node_id}' if row.outcome_node_id else ''))
    rebuilt = _rebuild(row) if row.node_id or row.outcome_node_id else 0
    return {**row_view(row), 'outcome': outcome, 'journeys_rebuilt': rebuilt}


# ── read ──────────────────────────────────────────────────────────────

def _stuck(row, now: datetime, after_days: int) -> Optional[int]:
    if row.state != 'sent' or row.last_report_at:
        return None
    days = (now - row.sent_at).days if row.sent_at else None
    return days if days is not None and days >= after_days else None


def row_view(row, now: Optional[datetime] = None) -> dict:
    from playbooks.definitions import governance
    now = now or datetime.utcnow()
    stuck_days = _stuck(row, now, int(governance()['stuck_after_days']))
    d = row.delivery or {}
    iso = lambda x: x.isoformat() if x else None
    return {
        'intervention_id': row.id, 'customer_id': row.customer_id, 'account_id': row.account_id,
        'playbook_id': row.playbook_id, 'playbook_version': row.playbook_version, 'action_class': row.action_class,
        'approval_mode': row.approval_mode, 'state': row.state, 'closed_state': row.closed_state, 'urgency': row.urgency,
        'trigger': {'episode_ids': row.trigger_episode_ids, 'node_ids': row.trigger_node_ids, 'roles': row.trigger_roles,
                    'quote': row.trigger_quote, 'evaluated_as_of': iso(row.evaluated_as_of)},
        'expected_outcome': {'types': row.expected_outcome_types, 'window_days': row.expected_window_days},
        'exposure_revenue': float(row.exposure_revenue) if row.exposure_revenue is not None else None,
        'proposed_at': iso(row.proposed_at), 'proposed_by': row.proposed_by,
        'approved_at': iso(row.approved_at), 'approved_by': row.approved_by, 'approved_by_key_id': row.approved_by_key_id,
        'sent_at': iso(row.sent_at), 'delivery': row.delivery, 'delivery_problem': bool(d) and d.get('status') != 'delivered',
        'started_at': iso(row.started_at), 'last_report_at': iso(row.last_report_at),
        'closed_at': iso(row.closed_at), 'closed_by': row.closed_by,
        'outcome': {'node_id': row.outcome_node_id, 'in_window': row.outcome_in_window, 'expected': row.outcome_expected} if row.outcome_node_id else None,
        'node_id': row.node_id, 'stuck': stuck_days is not None, 'stuck_days': stuck_days, 'notes': row.notes or [],
    }


def list_interventions(customer_id: int, account_id: Optional[int] = None, state: Optional[str] = None) -> dict:
    from extensions import db
    from models import Intervention, Account, ContextNode
    from playbooks.definitions import governance, tenant_config
    q = Intervention.query.filter_by(customer_id=int(customer_id))
    if account_id is not None:
        q = q.filter_by(account_id=int(account_id))
    if state:
        q = q.filter_by(state=state)
    rows = q.order_by(Intervention.proposed_at.desc(), Intervention.id.desc()).all()
    now = datetime.utcnow()
    names = {a.account_id: a.account_name for a in Account.query.filter_by(customer_id=int(customer_id)).all()}
    outcome_ids = [r.outcome_node_id for r in rows if r.outcome_node_id]
    outcomes = {n.node_id: n for n in ContextNode.query.filter(ContextNode.node_id.in_(outcome_ids)).all()} if outcome_ids else {}
    views, per_playbook = [], {}
    for r in rows:
        v = row_view(r, now)
        v['account_name'] = names.get(r.account_id)
        on = outcomes.get(r.outcome_node_id)
        if on is not None and v['outcome']:
            v['outcome'].update({'outcome_type': on.node_subtype, 'revenue': float(on.revenue_impact) if on.revenue_impact is not None else None,
                                 'occurred_at': on.occurred_at.isoformat() if on.occurred_at else None})
        views.append(v)
        s = per_playbook.setdefault(r.playbook_id, {
            'playbook_id': r.playbook_id, 'proposed': 0, 'approved': 0, 'sent': 0, 'closed_done': 0, 'closed_failed': 0, 'closed_cancelled': 0,
            'delivery_problems': 0, 'stuck': 0, 'outcomes_reported': 0, 'outcomes_in_window': 0, 'outcomes_expected': 0,
            'realized_revenue': 0.0, 'exposure_revenue': 0.0,
            'note': 'realized_revenue = linked outcomes (signed: losses negative); exposure_revenue = account revenue on the rows. Two numbers, never summed.',
        })
        if r.state == 'closed':
            s[f'closed_{r.closed_state}'] += 1
        else:
            s[r.state] += 1
        if v['delivery_problem']:
            s['delivery_problems'] += 1
        if v['stuck']:
            s['stuck'] += 1
        if r.outcome_node_id:
            s['outcomes_reported'] += 1
            s['outcomes_in_window'] += int(bool(r.outcome_in_window))
            s['outcomes_expected'] += int(bool(r.outcome_expected))
            if on is not None and on.revenue_impact is not None:
                s['realized_revenue'] = round(s['realized_revenue'] + float(on.revenue_impact), 2)
        if r.exposure_revenue is not None:
            s['exposure_revenue'] = round(s['exposure_revenue'] + float(r.exposure_revenue), 2)
    return {'customer_id': int(customer_id), 'count': len(views), 'interventions': views,
            'stuck': [v['intervention_id'] for v in views if v['stuck']],
            'stuck_after_days': int(governance()['stuck_after_days']),
            'by_playbook': sorted(per_playbook.values(), key=lambda s: s['playbook_id']),
            'tenant': tenant_config(customer_id)}
