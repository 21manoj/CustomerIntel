"""
The business transaction log for playbooks (models.BusinessEvent).

tool_audit_log answers "which key called which entry point, and was it allowed". This answers what a
business reader asks: what was proposed for which account, who approved it, what was sent, what came
back, and when the tenant's automation level or stop switch changed.

  record(...)            add one event to the open transaction. It never commits: the caller's commit
                         carries the change and its event together, so neither exists without the other.
  record_setting(...)    the same for a tenant-settings change (level, stop switch, plan approval, playbook
                         switched off), from the entries playbooks.definitions already writes to its log.
  list_events(...)       the reader: filters, paging, account names.
  to_csv(...)            the same rows as a CSV export.
  read_scope(role)       who may read: 'tenant' (every event of the tenant), 'own_accounts' (the user's
                         account list, plus tenant-level events), or None (no access). Per role, from
                         config/playbook_governance.json → business_events.read_scope_by_role, so narrowing
                         a role later is a config change, not a code change.
  backfill(...)          rebuild events for history that predates the table, marked backfilled.

First version covers playbook events only. Data loads, signal submissions and review-queue decisions are
not written here yet; the table would take them.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

EVENT_TYPES: Dict[str, str] = {
    # an intervention's life
    'proposed': 'Proposed',
    'approved': 'Approved',
    'sent': 'Sent',
    'started': 'Started',
    'step_done': 'Step done',
    'done': 'Closed: done',
    'failed': 'Closed: failed',
    'cancelled': 'Closed: cancelled',
    'declined': 'Declined',
    'reviewed': 'Reviewed afterwards',
    'agent_drafted': 'Drafted by the agent',
    # tenant settings
    'plan_approved': 'Plan approved',
    'autonomy_lowered': 'Automation level lowered',
    'level_set': 'Automation level set',
    'class_level_set': 'Automation level set for one kind of action',
    'class_level_cleared': 'Automation level for one kind of action cleared',
    'stop_switch_on': 'Stop switch turned on',
    'stop_switch_off': 'Stop switch turned off',
    'playbook_disabled': 'Playbook switched off',
    'playbook_enabled': 'Playbook switched on',
}
# playbooks.definitions autonomy-log event name → event type here (the rest keep their name)
SETTING_EVENTS = {'lowered': 'autonomy_lowered'}
SETTING_TYPES = ('plan_approved', 'autonomy_lowered', 'level_set', 'class_level_set', 'class_level_cleared',
                 'stop_switch_on', 'stop_switch_off', 'playbook_disabled', 'playbook_enabled')
READ_SCOPES = ('tenant', 'own_accounts')
CSV_COLUMNS = ['at', 'event_type', 'event', 'actor_kind', 'actor', 'account_id', 'account', 'intervention_id', 'playbook_id',
               'playbook_version', 'step', 'step_number', 'steps_total', 'action_class', 'exposure_revenue', 'state_before',
               'state_after', 'reason', 'delivery_status', 'outcome_type', 'outcome_node_id', 'trigger_key', 'backfilled', 'detail']


def settings() -> dict:
    from playbooks.definitions import governance
    return governance()['business_events']


# ── who ───────────────────────────────────────────────────────────────

def actor_kind(actor: Optional[dict] = None, label: Optional[str] = None) -> str:
    """user | key | policy | system | agent | local, from the actor the governance layer already carries
    (or, for a settings change that only kept a label, from the label's prefix)."""
    if actor:
        if actor.get('policy'):
            return 'policy'
        kind = actor.get('key_kind')
        if kind in ('customer', 'server'):
            return 'key'
        if kind in ('user', 'system', 'agent', 'local'):
            return kind
        label = label or actor.get('label')
    label = label or ''
    for prefix, kind in (('user:', 'user'), ('key:', 'key'), ('policy:', 'policy'), ('system:', 'system'), ('agent:', 'agent')):
        if label.startswith(prefix):
            return kind
    if label == 'server_key':
        return 'key'
    return 'local'


def read_scope(role: Optional[str]) -> Optional[str]:
    """'tenant' | 'own_accounts' | None for a session user's role."""
    scope = (settings().get('read_scope_by_role') or {}).get(role or '')
    return scope if scope in READ_SCOPES else None


# ── write ─────────────────────────────────────────────────────────────

def record(customer_id: int, event_type: str, actor: Optional[dict] = None, *, row=None, step: Optional[dict] = None,
           state_before: Optional[str] = None, state_after: Optional[str] = None, reason: Optional[str] = None,
           delivery_status: Optional[str] = None, outcome_node_id: Optional[int] = None, outcome_type: Optional[str] = None,
           detail: Optional[dict] = None, at: Optional[datetime] = None, actor_label: Optional[str] = None,
           account_id: Optional[int] = None, playbook_id: Optional[str] = None, action_class: Optional[str] = None,
           step_number: Optional[int] = None, backfilled: bool = False):
    """Add one event to the session. No commit: the caller's commit writes the change and the event together."""
    from extensions import db
    from models import BusinessEvent
    if event_type not in EVENT_TYPES:
        raise ValueError(f'unknown business event type {event_type!r}')
    label = actor_label or (actor or {}).get('label') or 'local'
    ev = BusinessEvent(
        at=at or datetime.utcnow(), customer_id=int(customer_id), event_type=event_type,
        actor_kind=actor_kind(actor, label), actor_label=str(label)[:120],
        intervention_id=row.id if row is not None else None,
        account_id=row.account_id if row is not None else account_id,
        playbook_id=row.playbook_id if row is not None else playbook_id,
        playbook_version=row.playbook_version if row is not None else None,
        step_id=step['id'] if step else None,
        step_number=step_number if step_number is not None else (row.step_index + 1 if step and row is not None and row.step_index is not None else None),
        steps_total=len(row.steps) if row is not None and row.steps else None,
        action_class=action_class or (step['action_class'] if step else (row.action_class if row is not None else None)),
        exposure_revenue=row.exposure_revenue if row is not None else None,
        state_before=state_before, state_after=state_after, reason=(reason or '').strip() or None,
        trigger_key=row.trigger_key if row is not None else None,
        delivery_status=delivery_status, outcome_node_id=outcome_node_id, outcome_type=outcome_type,
        detail=detail or None, backfilled=bool(backfilled),
    )
    db.session.add(ev)
    return ev


def record_setting(customer_id: int, entry: dict, ctx: Optional[dict] = None, backfilled: bool = False):
    """One event from an autonomy-log entry (playbooks.definitions._log): {at, event, by?, action_class?,
    from?, to?, playbook_id?, version?, reason?, detail?, plan_approval_withdrawn?}. ctx carries what the
    entry does not: the intervention that caused a lowering, and who reported it."""
    ctx = ctx or {}
    name = entry.get('event')
    event_type = SETTING_EVENTS.get(name, name)
    if event_type not in EVENT_TYPES:
        return None
    at = entry.get('at')
    extra = {k: entry[k] for k in ('from', 'to', 'version', 'plan_approval_withdrawn') if entry.get(k) is not None}
    row = ctx.get('row')
    ev = record(customer_id, event_type, ctx.get('actor'), actor_label=None if ctx.get('actor') else (entry.get('by') or ('unknown' if backfilled else 'local')),
                at=datetime.fromisoformat(at) if isinstance(at, str) else at,
                account_id=row.account_id if row is not None else None,
                playbook_id=entry.get('playbook_id') or (row.playbook_id if row is not None else None),
                action_class=entry.get('action_class'), reason=entry.get('detail') or entry.get('reason'),
                detail=extra, backfilled=backfilled)
    if row is not None:
        ev.intervention_id = row.id
    return ev


# ── read ──────────────────────────────────────────────────────────────

def _day(value, end: bool = False) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        when = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'{value!r} is not a date (use YYYY-MM-DD or an ISO timestamp)')
    if when.tzinfo is not None:                               # stored times are naive UTC
        when = (when - when.utcoffset()).replace(tzinfo=None)
    if end and len(text) <= 10:                               # a bare date as the end means through that day
        when += timedelta(days=1)
    return when


def view(ev, account_names: Optional[dict] = None) -> dict:
    return {
        'id': ev.id, 'at': ev.at.isoformat() if ev.at else None,
        'event_type': ev.event_type, 'event': EVENT_TYPES.get(ev.event_type, ev.event_type),
        'actor_kind': ev.actor_kind, 'actor': ev.actor_label,
        'account_id': ev.account_id, 'account': (account_names or {}).get(ev.account_id),
        'intervention_id': ev.intervention_id, 'playbook_id': ev.playbook_id, 'playbook_version': ev.playbook_version,
        'step': ev.step_id, 'step_number': ev.step_number, 'steps_total': ev.steps_total, 'action_class': ev.action_class,
        'exposure_revenue': float(ev.exposure_revenue) if ev.exposure_revenue is not None else None,
        'state_before': ev.state_before, 'state_after': ev.state_after, 'reason': ev.reason,
        'delivery_status': ev.delivery_status, 'outcome_type': ev.outcome_type, 'outcome_node_id': ev.outcome_node_id,
        'trigger_key': ev.trigger_key, 'backfilled': bool(ev.backfilled), 'detail': ev.detail,
    }


def list_events(customer_id: int, *, account_id: Optional[int] = None, playbook_id: Optional[str] = None,
                event_type: Optional[str] = None, intervention_id: Optional[int] = None, since=None, until=None,
                limit: Optional[int] = None, offset: int = 0, only_account_ids: Optional[List[int]] = None) -> dict:
    """A tenant's events, newest first. only_account_ids is the caller's account scope when their role reads
    'own_accounts': events for those accounts, plus tenant-level events (which name no account)."""
    from extensions import db
    from models import Account, BusinessEvent
    cfg = settings()
    if event_type and event_type not in EVENT_TYPES:
        raise ValueError(f'unknown event_type {event_type!r} (known: {sorted(EVENT_TYPES)})')
    limit = int(cfg['page_size']) if limit in (None, '') else int(limit)
    offset = int(offset or 0)
    if limit < 1 or offset < 0:
        raise ValueError('limit must be at least 1 and offset at least 0')
    limit = min(limit, int(cfg['max_page_size']))
    q = BusinessEvent.query.filter(BusinessEvent.customer_id == int(customer_id))
    if account_id is not None:
        q = q.filter(BusinessEvent.account_id == int(account_id))
    if only_account_ids is not None:
        q = q.filter(db.or_(BusinessEvent.account_id.is_(None), BusinessEvent.account_id.in_([int(a) for a in only_account_ids] or [-1])))
    if playbook_id:
        q = q.filter(BusinessEvent.playbook_id == playbook_id)
    if event_type:
        q = q.filter(BusinessEvent.event_type == event_type)
    if intervention_id is not None:
        q = q.filter(BusinessEvent.intervention_id == int(intervention_id))
    start, end = _day(since), _day(until, end=True)
    if start:
        q = q.filter(BusinessEvent.at >= start)
    if end:
        q = q.filter(BusinessEvent.at < end)
    total = q.count()
    rows = q.order_by(BusinessEvent.at.desc(), BusinessEvent.id.desc()).offset(offset).limit(limit).all()
    ids = {r.account_id for r in rows if r.account_id is not None}
    names = dict(db.session.query(Account.account_id, Account.account_name)
                 .filter(Account.customer_id == int(customer_id), Account.account_id.in_(ids)).all()) if ids else {}
    return {'customer_id': int(customer_id), 'total': total, 'limit': limit, 'offset': offset,
            'events': [view(r, names) for r in rows],
            'event_types': [{'id': k, 'label': v} for k, v in EVENT_TYPES.items()],
            'coverage': 'playbook events only (proposals, approvals, sends, reports, drafts, automation settings)'}


def to_csv(events: List[dict]) -> str:
    import json
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for e in events:
        w.writerow([_cell(json.dumps(e[c], sort_keys=True) if c == 'detail' and e.get(c) is not None else e.get(c)) for c in CSV_COLUMNS])
    return buf.getvalue()


def _cell(v):
    """A text cell that starts with = + - or @ is read as a formula by a spreadsheet: prefix it."""
    if v is None:
        return ''
    if isinstance(v, str) and v[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + v
    return v


def export_csv(customer_id: int, **filters) -> str:
    """Every matching event, up to business_events.export_max_rows, as CSV."""
    cfg = settings()
    cap, page = int(cfg['export_max_rows']), int(cfg['max_page_size'])
    out, offset = [], 0
    while len(out) < cap:
        got = list_events(customer_id, limit=page, offset=offset, **filters)['events']
        out.extend(got)
        if len(got) < page:
            break
        offset += page
    return to_csv(out[:cap])


# ── backfill ──────────────────────────────────────────────────────────

_DRAFT_RE = re.compile(r'^intervention (\d+):')


def _when(text) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text) if isinstance(text, str) else text
    except ValueError:
        return None


def _backfill_intervention(row) -> int:
    """Events for one intervention that has none, from its own timestamps, steps and notes."""
    n = 0
    steps = list(row.steps or [])
    total = len(steps) or None

    def add(event_type, at, by, **kw):
        nonlocal n
        if at is None:
            return
        idx = kw.pop('idx', None)
        step = steps[idx] if steps and idx is not None and idx < len(steps) else None
        record(row.customer_id, event_type, None, row=row, step=step, at=at, actor_label=by or 'unknown',
               step_number=(idx + 1) if step else None, backfilled=True, **kw)
        n += 1

    add('proposed', row.proposed_at, row.proposed_by, state_after='proposed', idx=0 if steps else None)
    idx = 0
    for note in list(row.notes or []):
        t, at, by, why = note.get('transition'), _when(note.get('at')), note.get('by'), note.get('note')
        if t in ('approve', 'approve_step'):
            add('approved', at, by, state_before='proposed' if t == 'approve' else 'sent', state_after='approved', reason=why, idx=idx)
            step = steps[idx] if steps and idx < len(steps) else None
            sent_at = _when(step.get('sent_at')) if step else row.sent_at
            delivery = (step.get('delivery') if step else row.delivery) or {}
            add('sent', sent_at, by, state_before='approved', state_after='sent', delivery_status=delivery.get('status'), idx=idx)
        elif t == 'step_done':
            add('step_done', at, by, state_before='sent', state_after='sent', reason=why, idx=idx)
            idx += 1
        elif t == 'started':
            add('started', at, by, state_before='sent', state_after='sent', reason=why, idx=idx)
        elif t == 'reviewed':
            add('reviewed', at, by, reason=why)
        elif t in ('done', 'failed', 'cancelled'):
            declined = t == 'cancelled' and (row.sent_at is None or any(s.get('state') == 'declined' for s in steps))
            add('declined' if declined else t, at, by, state_before='proposed' if row.sent_at is None else 'sent', state_after='closed',
                reason=why, outcome_node_id=row.outcome_node_id if t == 'done' else None, idx=idx,
                detail=({'in_window': row.outcome_in_window, 'expected': row.outcome_expected} if row.outcome_node_id and t == 'done' else None))
    return n


def backfill(customer_id: Optional[int] = None, dry_run: bool = False) -> dict:
    """Rebuild events for history written before the table existed. Safe to run again: an intervention that
    already has any event is skipped, and so is a settings change that already has its event.
    Sources: the intervention row (timestamps, steps, notes), the tenant's automation log (kept to its last
    entries, so older settings changes are not recoverable) and the audit rows of agent drafts."""
    from extensions import db
    from models import BusinessEvent, Customer, Intervention, ToolAuditLog
    from playbooks.definitions import tenant_config
    from playbooks.agent import AUDIT_TOOL, agent_actor
    out = {'dry_run': bool(dry_run), 'tenants': {}}
    tenants = [db.session.get(Customer, int(customer_id))] if customer_id is not None else Customer.query.order_by(Customer.customer_id).all()
    for cust in [c for c in tenants if c]:
        cid = cust.customer_id
        have = {i for (i,) in db.session.query(BusinessEvent.intervention_id).filter(
            BusinessEvent.customer_id == cid, BusinessEvent.intervention_id.isnot(None)).distinct()}
        counts = {'interventions': 0, 'intervention_events': 0, 'setting_events': 0, 'draft_events': 0}
        todo = [r for r in Intervention.query.filter_by(customer_id=cid).order_by(Intervention.id).all() if r.id not in have]
        for row in todo:
            counts['interventions'] += 1
            counts['intervention_events'] += _backfill_intervention(row)
        by_id = {r.id: r for r in todo}
        for a in ToolAuditLog.query.filter_by(customer_id=cid, tool=AUDIT_TOOL, outcome='allowed').order_by(ToolAuditLog.id).all():
            m = _DRAFT_RE.match(a.detail or '')
            row = by_id.get(int(m.group(1))) if m else None
            if row is not None:
                record(cid, 'agent_drafted', agent_actor(), row=row, at=a.at, reason=a.detail, backfilled=True)
                counts['draft_events'] += 1
        seen = {(t, at) for t, at in db.session.query(BusinessEvent.event_type, BusinessEvent.at).filter(
            BusinessEvent.customer_id == cid, BusinessEvent.event_type.in_(SETTING_TYPES))}
        for entry in tenant_config(cid)['autonomy_log']:       # a live settings event carries its log entry's time
            key = (SETTING_EVENTS.get(entry.get('event'), entry.get('event')), _when(entry.get('at')))
            if key not in seen and record_setting(cid, entry, backfilled=True) is not None:
                counts['setting_events'] += 1
        if any(counts.values()):
            out['tenants'][cid] = counts
        if dry_run:
            db.session.rollback()
        else:
            db.session.commit()
    out['events'] = sum(c['intervention_events'] + c['setting_events'] + c['draft_events'] for c in out['tenants'].values())
    return out
