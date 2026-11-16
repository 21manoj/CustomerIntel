"""
Interventions — propose, approve+send, report, list (design §3–§7).

    evaluate(customer_id, account_id=None, dry_run=False)        journey evidence → proposed rows (cited)
    approve(customer_id, intervention_id, note=None)              human/policy approval → signed webhook → INTERVENTION node → 'sent'
    report(customer_id, intervention_id, state, ...)              the external workflow's callback → 'closed' (+ outcome)
    list_interventions(customer_id, account_id=None, state=None)  the read, with stuck ones and the per-playbook numbers
    health_counts()                                               the platform-wide numbers /health reports (same definitions)
    intervention_title(playbook_label, account_name)              the INTERVENTION node's title — approve and the repair
                                                                  script (scripts/repair_intervention_titles.py) share it

Rules kept: every proposal cites what it fired on — episode ids for communications, the KPI reading or
profile date for a fact trigger (config/playbook_governance.json → fact_triggers); a human approves anything
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
TITLE_SEP = ' — '          # journeys.narrative._strip_suffix cuts a title at the first one: a playbook label never contains it


def intervention_title(playbook_label: str, account_name: Optional[str]) -> str:
    """'<playbook label> — <account name>', the one way an INTERVENTION node is titled. The narrative reads the
    label back by cutting at the first ' — ' (journeys.narrative._strip_suffix), which is why the label itself
    must not carry that separator (playbooks.definitions refuses one that does) and why the separator lives here."""
    from models import ContextNode
    label = (playbook_label or '').strip()
    name = (account_name or '').strip()
    title = f'{label}{TITLE_SEP}{name}' if name else label
    return title[:ContextNode.__table__.c.title.type.length]


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

def _trigger_key(episode_ids: List[str], facts: Optional[List[dict]] = None) -> str:
    """Idempotency key: the cited episode ids plus, for a fact trigger, each fact's key (a KPI reading's
    code and date, a profile date's field and value). With no facts it is the hash it always was."""
    parts = sorted(episode_ids) + sorted(f['key'] for f in (facts or []))
    return hashlib.sha256(','.join(parts).encode('utf-8')).hexdigest()


_KPI_OPS = {'<': lambda a, b: a < b, '<=': lambda a, b: a <= b, '>': lambda a, b: a > b, '>=': lambda a, b: a >= b}


def _fmt_num(x: float) -> str:
    return f'{x:g}'


def _kpi_facts(account_id: int, trig: dict) -> tuple:
    """(fires, reason, facts) for trigger.kpi: each condition reads the account's latest `periods` readings of
    that KPI and holds when there are that many and every one breaches. kpi_match any/all across conditions."""
    from models import KPIMeasurement
    facts, missed = [], []
    for c in trig['kpi']:
        rows = (KPIMeasurement.query.filter_by(account_id=int(account_id), kpi_code=c['code'])
                .order_by(KPIMeasurement.measured_at.desc(), KPIMeasurement.kpi_id.desc()).limit(c['periods']).all())
        if len(rows) < c['periods']:
            missed.append(f"{c['code']}:no_reading" if not rows else f"{c['code']}:readings_{len(rows)}<{c['periods']}")
            continue
        if not all(_KPI_OPS[c['op']](float(r.value), c['value']) for r in rows):
            missed.append(f"{c['code']}:{_fmt_num(float(rows[0].value))}_not_{c['op']}{_fmt_num(c['value'])}")
            continue
        latest = rows[0]
        when = latest.measured_at.date().isoformat() if latest.measured_at else None
        run = f" for {c['periods']} readings in a row" if c['periods'] > 1 else ''
        facts.append({'kind': 'kpi', 'key': f"kpi:{c['code']}:{when}", 'code': c['code'], 'name': c['name'],
                      'value': float(latest.value), 'op': c['op'], 'threshold': c['value'], 'periods': c['periods'],
                      'measured_at': when, 'kpi_id': latest.kpi_id,
                      'text': f"{c['name']} is {_fmt_num(float(latest.value))}{run} (measured {when}); "
                              f"the trigger is {c['op']} {_fmt_num(c['value'])}"})
    if trig['kpi_match'] == 'all' and missed:
        return False, f'kpi_not_breached:{missed}', []
    if not facts:
        return False, f'kpi_not_breached:{missed}', []
    return True, 'fires', facts


def _date_fact(account, trig: dict, as_of: datetime) -> tuple:
    """(fires, reason, facts) for trigger.date: a date in the account profile, measured against the journey's
    as_of. within_days N fires when the date is N days away or closer (a date already past counts);
    overdue_by_days N fires when the date passed N or more days ago."""
    d = trig['date']
    from utils.account_profile import profile_field
    raw = profile_field(account, d['field']) if account is not None else None
    if not raw:
        return False, f"no_date:{d['field']}", []
    try:
        when = datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return False, f"bad_date:{d['field']}", []
    days = (when - as_of.date()).days
    if d['within_days'] is not None:
        if days > d['within_days']:
            return False, f"date_beyond_window:{d['field']}:{days}>{d['within_days']}", []
        rule = f"within {d['within_days']} days"
    else:
        if days > -d['overdue_by_days']:
            return False, f"date_not_overdue:{d['field']}:{days}", []
        rule = f"{d['overdue_by_days']} or more days overdue"
    where = 'is today' if days == 0 else (f'is in {days} days' if days > 0 else f'was {-days} days ago')
    return True, 'fires', [{'kind': 'date', 'key': f"date:{d['field']}:{when.isoformat()}", 'field': d['field'],
                            'label': d['label'], 'date': when.isoformat(), 'days_until': days,
                            'text': f"{d['label'][:1].upper()}{d['label'][1:]} ({when.isoformat()}) {where}; the trigger is {rule}"}]


def _match_facts(playbook: dict, account, as_of: datetime) -> tuple:
    """(fires, reason, facts) for the trigger's kpi and date parts (both must hold when both are declared)."""
    trig, facts = playbook['trigger'], []
    if trig.get('kpi'):
        ok, reason, got = _kpi_facts(account.account_id, trig)
        if not ok:
            return False, reason, []
        facts += got
    if trig.get('date'):
        ok, reason, got = _date_fact(account, trig, as_of)
        if not ok:
            return False, reason, []
        facts += got
    return True, 'fires', facts


def _urgency_rank(level: Optional[str]) -> int:
    from signal_engine.urgency import LEVELS
    return LEVELS.index(level) if level in LEVELS else -1


def _node_urgencies(node_ids: List[int]) -> Dict[int, Optional[str]]:
    from models import ContextNode
    if not node_ids:
        return {}
    return {n.node_id: (n.properties or {}).get('effective_urgency')
            for n in ContextNode.query.filter(ContextNode.node_id.in_(node_ids)).all()}


def _match_roles(trig: dict, journey: dict, urgencies_cache: dict) -> tuple:
    """(holds, reason, cited_episodes, max_urgency) for trigger.roles + urgency_floor."""
    from signal_engine.urgency import classify_structural_urgency
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
    return True, 'fires', cited, max_lvl


def _match(playbook: dict, journey: dict, urgencies_cache: dict) -> tuple:
    """(fires, reason, cited_episodes, max_urgency). The communications part of a trigger: the latest leading
    month's cited evidence (when the trigger names roles) and the renewal window. Facts are _match_facts."""
    trig = playbook['trigger']
    cited, max_lvl = [], None
    if trig['roles']:
        ok, reason, cited, max_lvl = _match_roles(trig, journey, urgencies_cache)
        if not ok:
            return False, reason, cited, max_lvl
    rwd = trig.get('renewal_within_days')
    if rwd is not None:
        days = (journey.get('features') or {}).get('days_to_renewal')
        if days is None:
            return False, 'no_renewal_date', cited, max_lvl
        if days > rwd:
            return False, f'renewal_beyond_window:{days}>{rwd}', cited, max_lvl
    return True, 'fires', cited, max_lvl


# ── autonomy: who approves ────────────────────────────────────────────

def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ('true', 'yes', 'y', '1')


def policy_decision(cfg: dict, playbook: dict, action_class: str, effects: List[str], account, account_flags: List[str]) -> dict:
    """Whether policy may approve one action (a single-action playbook, or one step of a plan) for this tenant,
    and why not when it may not. Rules and numbers are config/playbook_governance.json → autonomy.
    {'by_policy', 'level', 'reason', 'review_afterwards'}."""
    from playbooks.definitions import governance, effective_level
    from utils.account_profile import profile_field
    a = governance()['autonomy']
    level = effective_level(cfg, action_class)
    no = lambda reason: {'by_policy': False, 'level': level, 'reason': reason, 'review_afterwards': False}
    if action_class in a['always_human']['action_classes']:
        return no(f'always_human:action_class:{action_class}')
    hit = [e for e in effects if e in a['always_human']['effects']]
    if hit:
        return no(f'always_human:effect:{hit[0]}')
    for flag in account_flags:
        if account is not None and _truthy(profile_field(account, flag)):
            return no(f'always_human:account_flag:{flag}')
    if action_class not in a['policy_approves'][str(level)]:
        return no(f'level_{level}_does_not_cover:{action_class}')
    declared_auto = playbook.get('approval') == 'auto'
    if level in a['declared_auto_only'] and not declared_auto:
        return no(f'level_{level}_covers_only_playbooks_declared_auto')
    if level in a['needs_plan_approval'] and not declared_auto:
        plan = (cfg.get('approved_plans') or {}).get(playbook['id']) or {}
        if not plan or plan.get('version') != playbook.get('version'):
            return no(f'level_{level}_needs_the_plan_approved_once')
    return {'by_policy': True, 'level': level, 'reason': f'level_{level}_covers:{action_class}',
            'review_afterwards': action_class in a['review_afterwards']}


def _refuse_agent(actor: dict, what: str) -> None:
    """The playbook agent drafts; it never decides. Its actor kind is refused on every decision."""
    if (actor or {}).get('key_kind') == 'agent':
        raise ValueError(f'the agent cannot {what} an intervention: that takes a person or the tenant\'s policy')


def _policy_actor(decision: dict) -> dict:
    return {'key_kind': 'system', 'key_record': None, 'key_id': None, 'policy': True,
            'label': f"policy:automation_level_{decision['level']}", 'review_afterwards': decision['review_afterwards']}


def _step(row) -> Optional[dict]:
    """The current step of a multi-step row (a copy), or None for a single action."""
    if not row.steps or row.step_index is None:
        return None
    return dict(row.steps[row.step_index])


def _set_step(row, **changes) -> None:
    steps = [dict(st) for st in row.steps]
    steps[row.step_index].update(changes)
    row.steps = steps


def _account_flags(customer_id: int) -> List[str]:
    from playbooks.definitions import load_vertical
    from utils.vertical_registry import get_vertical_for_customer
    return list((load_vertical(get_vertical_for_customer(int(customer_id))).get('always_human') or {}).get('account_flags') or [])


def evaluate(customer_id: int, account_id: Optional[int] = None, dry_run: bool = False,
             actor: Optional[dict] = None) -> dict:
    """Propose interventions from the journeys' latest leading month. Idempotent per
    (account, playbook, trigger set); one open proposal per (account, playbook); nothing
    within window_days of a closed one. dry_run returns what it would propose."""
    from extensions import db
    from models import Account, JourneyData, Intervention
    from playbooks.definitions import playbooks_for_customer, governance
    actor = actor or current_actor()
    defs = playbooks_for_customer(customer_id, suggestions=False)
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
    flags = defs['always_human']['account_flags']
    for jd in q.order_by(JourneyData.account_id).all():
        out['accounts_evaluated'] += 1
        acct = db.session.get(Account, jd.account_id)
        journey = jd.journey_json or {}
        as_of = journey.get('as_of')
        if (journey.get('lifecycle') or {}).get('status') == 'closed':
            # nobody is asked to save a customer who has already left, or to remediate a terminated vendor
            out['skipped'].append({'account_id': jd.account_id, 'playbook_id': None, 'reason': 'account_closed'})
            continue
        for pb in defs['playbooks']:
            fires, reason, cited, urgency = _match(pb, journey, urgencies)
            facts = []
            if fires and (pb['trigger'].get('kpi') or pb['trigger'].get('date')):
                if acct is None:
                    fires, reason = False, 'no_account'
                else:
                    # days to a date are counted from the journey's as_of, but never from a day that has not come yet:
                    # a monthly reading dates the journey at its month end, which can be after today
                    fires, reason, facts = _match_facts(pb, acct, min(datetime.fromisoformat(as_of), now) if as_of else now)
            if not fires:
                if reason != 'no_recent_evidence':
                    out['skipped'].append({'account_id': jd.account_id, 'playbook_id': pb['id'], 'reason': reason})
                continue
            episode_ids = sorted(e['episode_id'] for e in cited)
            key = _trigger_key(episode_ids, facts)
            urgency = urgency or pb['trigger'].get('urgency')
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
            pbv = {**pb, 'version': defs['version']}
            head = pb['steps'][0] if pb['steps'] else None
            decision = policy_decision(cfg, pbv, head['action_class'] if head else pb['action_class'],
                                       sorted(set(pb['effects']) | set(head['effects'] if head else [])), acct, flags)
            first = cited[0] if cited else {}
            quote = ((first.get('meta') or {}).get('quote') or first.get('title') or (facts[0]['text'] if facts else '')).strip()
            proposal = {
                'account_id': jd.account_id, 'account_name': acct.account_name if acct else None, 'playbook_id': pb['id'],
                'label': pb['label'], 'action_class': pb['action_class'], 'approval': pb['approval'], 'urgency': urgency,
                'trigger_episode_ids': episode_ids, 'trigger_quote': quote, 'trigger_facts': facts,
                'trigger_roles': sorted({e.get('role') for e in cited if e.get('role')}),
                'expected_outcome': pb['expected_outcome'], 'exposure_revenue': float(acct.revenue) if acct and acct.revenue is not None else None,
                'evaluated_as_of': as_of, 'approval_policy': decision,
                'steps': [{'id': st['id'], 'label': st['label'], 'action_class': st['action_class']} for st in pb['steps']],
            }
            if dry_run:
                out['proposed'].append(proposal)
                continue
            row = Intervention(
                customer_id=int(customer_id), account_id=jd.account_id, playbook_id=pb['id'], playbook_version=defs['version'],
                action_class=pb['action_class'], approval_mode=pb['approval'], state='proposed', urgency=urgency,
                trigger_key=key, trigger_episode_ids=episode_ids,
                trigger_node_ids=sorted({nid for e in cited for nid in (e.get('evidence_node_ids') or [])}),
                trigger_roles=proposal['trigger_roles'], trigger_quote=quote[:2000] or None, trigger_facts=facts or None,
                evaluated_as_of=datetime.fromisoformat(as_of) if as_of else None,
                expected_outcome_types=list(pb['expected_outcome']['types']), expected_window_days=int(pb['expected_outcome']['window_days']),
                exposure_revenue=proposal['exposure_revenue'], proposed_at=now, proposed_by=actor['label'], notes=[],
                approval_policy=decision,
                steps=[{**st, 'state': 'awaiting_approval' if i == 0 else 'pending'} for i, st in enumerate(pb['steps'])] or None,
                step_index=0 if pb['steps'] else None,
            )
            db.session.add(row)
            db.session.commit()
            _audit(customer_id, 'propose', actor,
                   f'#{row.id} {pb["id"]} account {jd.account_id} urgency={urgency} cites '
                   f'{",".join(episode_ids + [f["key"] for f in facts])[:120]}')
            proposal['intervention_id'] = row.id
            out['proposed'].append(proposal)
            if decision['by_policy']:
                res = approve(customer_id, row.id, note=f"approved by policy ({decision['reason']})", actor=_policy_actor(decision))
                out['auto_approved'].append({'intervention_id': row.id, 'delivery': res.get('delivery'),
                                             'review_afterwards': decision['review_afterwards']})
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


def playbook_def(customer_id: int, playbook_id: str, action_class: Optional[str] = None) -> dict:
    """The CURRENT definition of a playbook for the tenant's vertical. A playbook that no longer exists (renamed,
    removed from the vertical file) comes back as its id humanised, so a row or node written under it still reads."""
    from playbooks.definitions import load_vertical
    from utils.vertical_registry import get_vertical_for_customer
    for p in load_vertical(get_vertical_for_customer(int(customer_id)))['playbooks']:
        if p['id'] == playbook_id:
            return p
    return {'id': playbook_id, 'label': playbook_id.replace('_', ' '), 'action_class': action_class}


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
    intervention must be visible on the journey, and so must the delivery problem. A notify-class row on a
    tenant with a Slack URL also posts there; that result is delivery.slack, a second entry.

    On a multi-step row this approves the CURRENT step: the first one from 'proposed', a later one while the row
    is 'sent' and that step is awaiting approval. A person's first approval is also the plan approval that
    autonomy.needs_plan_approval asks for (remembered per playbook version)."""
    from extensions import db
    from models import Account, Customer, ContextNode, ContextEdge
    from playbooks.definitions import tenant_config, tenant_secret, tenant_slack_url, record_plan_approval
    from playbooks import webhook
    from adapters import slack_notify
    from utils.data_origin import block
    actor = actor or current_actor()
    _refuse_agent(actor, 'approve')
    row = _get(customer_id, intervention_id)
    step = _step(row)
    first = row.state == 'proposed'
    if not first and not (row.state == 'sent' and step and step['state'] == 'awaiting_approval'):
        raise ValueError(f'intervention {row.id} is {row.state}, only a proposed one (or a step awaiting approval) can be approved')
    cfg = tenant_config(customer_id)
    if cfg['kill_switch']:
        raise ValueError('kill switch is on for this tenant: nothing is approved or sent')
    by_policy = bool(actor.get('policy'))
    now = datetime.utcnow()
    if first:
        row.state, row.approved_at, row.approved_by, row.approved_by_key_id = 'approved', now, actor['label'], actor.get('key_id')
    if step:
        _set_step(row, state='approved', approved_by=actor['label'], approved_at=now.isoformat())
    if by_policy and actor.get('review_afterwards'):
        row.review_required, row.reviewed_at, row.reviewed_by = True, None, None
    _note(row, actor, 'approve' if first else 'approve_step', note)
    db.session.commit()
    what = f"step {row.step_index + 1}/{len(row.steps)} {step['id']} " if step else ''
    _audit(customer_id, 'approve', actor, f'#{row.id} {row.playbook_id} {what}account {row.account_id} by {actor["label"]}')
    if first and not by_policy and record_plan_approval(customer_id, row.playbook_id, row.playbook_version, actor['label']):
        _audit(customer_id, 'plan_approved', actor, f'{row.playbook_id} v{row.playbook_version} (first approval, #{row.id}) by {actor["label"]}')

    acct = db.session.get(Account, row.account_id)
    cust = db.session.get(Customer, row.customer_id)
    triggers = _triggers(row)
    pb = playbook_def(row.customer_id, row.playbook_id, row.action_class)
    action_class = step['action_class'] if step else row.action_class
    payload = webhook.build_payload(row, acct, cust, triggers, actor['label'], block(cust))
    delivery = webhook.deliver(cfg.get('webhook_url'), tenant_secret(customer_id), payload)
    if cfg['slack_webhook_url_set'] and slack_notify.applies_to(action_class):
        # a second entry, not a second channel: delivery.status stays the workflow engine's (adapters.md §3)
        delivery = {**delivery, 'slack': slack_notify.post(tenant_slack_url(customer_id), row, acct, pb)}
    sent_at = datetime.utcnow()
    row.delivery = delivery
    row.state = 'sent'
    if step:
        _set_step(row, state='sent', sent_at=sent_at.isoformat(), delivery=delivery)

    if first:
        row.sent_at = sent_at
        node = ContextNode(
            customer_id=row.customer_id, account_id=row.account_id, node_type=NODE_TYPE, node_subtype=row.playbook_id,
            source='observed', tier=1, title=intervention_title(pb['label'], acct.account_name),
            properties={'intervention_id': row.id, 'playbook_id': row.playbook_id, 'playbook_version': row.playbook_version,
                        'action_class': row.action_class, 'approved_by': row.approved_by, 'approved_at': row.approved_at.isoformat(),
                        'trigger_episode_ids': list(row.trigger_episode_ids or []), 'trigger_quote': row.trigger_quote,
                        'trigger_facts': list(row.trigger_facts or []),
                        'urgency': row.urgency, 'delivery_status': delivery['status'], 'delivery_error': delivery.get('error'),
                        'delivery_host': delivery.get('url_host'), 'evidence_tier': 'observed', 'closed_state': None,
                        **({'step': step['id'], 'step_number': 1, 'steps_total': len(row.steps)} if step else {})},
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
    elif row.node_id:
        node = db.session.get(ContextNode, row.node_id)          # one node per intervention: a later step updates it
        if node:
            node.properties = {**(node.properties or {}), 'step': step['id'], 'step_number': row.step_index + 1,
                               'steps_total': len(row.steps), 'delivery_status': delivery['status'],
                               'delivery_error': delivery.get('error'), 'delivery_host': delivery.get('url_host')}
    db.session.commit()
    _audit(customer_id, 'send', actor, f'#{row.id} {row.playbook_id} {what}→ {delivery["status"]} host={delivery.get("url_host")} '
                                        f'attempts={delivery["attempts"]} node={row.node_id}' + (f' error={delivery["error"][:80]}' if delivery.get('error') else '')
                                        + (f' slack={delivery["slack"]["status"]}' if delivery.get('slack') else ''))
    rebuilt = _rebuild(row) if first else 0
    return {**row_view(row), 'payload': payload, 'journeys_rebuilt': rebuilt}


def review(customer_id: int, intervention_id: int, note: Optional[str] = None, actor: Optional[dict] = None) -> dict:
    """A person has looked at an action policy approved on its own (autonomy.review_afterwards). Recorded on the
    row and in the audit log; it changes nothing else. To say the action was wrong, report it failed or cancelled."""
    from extensions import db
    actor = actor or current_actor()
    _refuse_agent(actor, 'review')
    row = _get(customer_id, intervention_id)
    if not row.review_required:
        raise ValueError(f'intervention {row.id} was not marked for review (a person approved it, or its action class is not reviewed afterwards)')
    if row.reviewed_at:
        raise ValueError(f'intervention {row.id} was already reviewed by {row.reviewed_by}')
    row.reviewed_at, row.reviewed_by = datetime.utcnow(), actor['label']
    _note(row, actor, 'reviewed', note)
    db.session.commit()
    _audit(customer_id, 'reviewed', actor, f'#{row.id} {row.playbook_id} reviewed by {actor["label"]}')
    return row_view(row)


def _lower(row, action_class: str, reason: str, actor: dict) -> Optional[dict]:
    """A decline or a failure lowers the tenant's level for that action class (playbooks.definitions.lower_level)."""
    from playbooks.definitions import lower_level
    change = lower_level(row.customer_id, action_class, reason, f'intervention #{row.id} ({row.playbook_id}) {reason} by {actor["label"]}',
                         playbook_id=row.playbook_id)
    if change:
        _audit(row.customer_id, 'autonomy_lowered', actor,
               f'{action_class} level {change["from"]} → {change["to"]} after #{row.id} {row.playbook_id} {reason}'
               + (f'; plan approval for {change["plan_approval_withdrawn"]} withdrawn' if change.get('plan_approval_withdrawn') else ''))
    return change


def _advance(row, actor: dict, note: Optional[str]) -> dict:
    """The current step was reported done and there is another: start it. Policy approves and sends it when the
    tenant's level allows; otherwise (or while the stop switch is on) it waits for a person."""
    from extensions import db
    from models import Account
    from playbooks.definitions import tenant_config
    now = datetime.utcnow()
    done = _step(row)
    _set_step(row, state='done', done_at=now.isoformat(), reported_by=actor['label'])
    row.step_index += 1
    row.last_report_at = now
    nxt = _step(row)
    cfg = tenant_config(row.customer_id)
    pb = playbook_def(row.customer_id, row.playbook_id, row.action_class)
    decision = policy_decision(cfg, {**pb, 'version': row.playbook_version}, nxt['action_class'],
                               sorted(set(pb.get('effects') or []) | set(nxt.get('effects') or [])),
                               db.session.get(Account, row.account_id), _account_flags(row.customer_id))
    if cfg['kill_switch']:
        decision = {**decision, 'by_policy': False, 'reason': 'stop_switch_on'}
    _set_step(row, state='awaiting_approval', policy=decision)
    _note(row, actor, 'step_done', note)
    db.session.commit()
    _audit(row.customer_id, 'step_done', actor,
           f'#{row.id} {row.playbook_id} step {row.step_index}/{len(row.steps)} {done["id"]} done; next {nxt["id"]} '
           f'({nxt["action_class"]}): {"policy" if decision["by_policy"] else "waits for a person"} ({decision["reason"]})')
    if decision['by_policy']:
        approve(row.customer_id, row.id, note=f"approved by policy ({decision['reason']})", actor=_policy_actor(decision))
    return {**row_view(row), 'outcome': None, 'journeys_rebuilt': 0,
            'advanced_to': {'step': nxt['id'], 'number': row.step_index + 1, 'of': len(row.steps), 'by_policy': decision['by_policy'],
                            'reason': decision['reason']}}


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
    _refuse_agent(actor, 'report on')
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
    step = _step(row)
    waiting = bool(step and row.state == 'sent' and step['state'] == 'awaiting_approval')
    if waiting and state in ('done', 'failed'):
        raise ValueError(f"intervention {row.id} step {step['id']!r} is awaiting approval; approve it, or decline with state='cancelled'")
    if state == 'done' and step and row.step_index < len(row.steps) - 1 and not outcome_type:
        return _advance(row, actor, note)                 # more steps to go: the row stays open
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
    was_proposed = row.sent_at is None
    declined = state == 'cancelled' and (was_proposed or waiting)
    acted_class = step['action_class'] if step else row.action_class
    if step:                                              # close the plan: this step takes the reported state, the rest never ran
        final = {'done': 'done', 'failed': 'failed', 'cancelled': 'declined' if declined else 'cancelled'}[state]
        row.steps = [{**st, 'state': final, 'done_at': now.isoformat()} if i == row.step_index
                     else ({**st, 'state': 'skipped'} if i > row.step_index else dict(st)) for i, st in enumerate(row.steps)]
    row.state, row.closed_at, row.closed_state, row.closed_by, row.last_report_at = 'closed', now, state, actor['label'], now
    _note(row, actor, state, note)
    if row.node_id:
        node = db.session.get(ContextNode, row.node_id)
        if node:
            node.properties = {**(node.properties or {}), 'closed_state': state, 'closed_at': now.isoformat(),
                               'outcome_node_id': row.outcome_node_id, 'reported_by': actor['label']}
    db.session.commit()
    _audit(customer_id, 'declined' if declined else state, actor,
           f'#{row.id} {row.playbook_id} {state} by {actor["label"]}' + (f' outcome node {row.outcome_node_id}' if row.outcome_node_id else ''))
    lowered = _lower(row, acted_class, 'declined' if declined else state, actor) if (declined or state == 'failed') else None
    rebuilt = _rebuild(row) if row.node_id or row.outcome_node_id else 0
    return {**row_view(row), 'outcome': outcome, 'journeys_rebuilt': rebuilt, 'autonomy_lowered': lowered}


# ── read ──────────────────────────────────────────────────────────────

def _stuck(row, now: datetime, after_days: int) -> Optional[int]:
    """Days a sent row has waited without any report once past after_days; None otherwise. The one definition
    of 'stuck' (row_view, list_interventions, health_counts)."""
    if row.state != 'sent' or row.last_report_at:
        return None
    days = (now - row.sent_at).days if row.sent_at else None
    return days if days is not None and days >= after_days else None


def _delivery_problem(row) -> bool:
    """A delivery record whose status is not 'delivered' (failed, not_configured). The one definition."""
    from playbooks.webhook import DELIVERED
    d = row.delivery or {}
    return bool(d) and d.get('status') != DELIVERED


def row_view(row, now: Optional[datetime] = None) -> dict:
    from playbooks.definitions import governance
    now = now or datetime.utcnow()
    stuck_days = _stuck(row, now, int(governance()['stuck_after_days']))
    iso = lambda x: x.isoformat() if x else None
    return {
        'intervention_id': row.id, 'customer_id': row.customer_id, 'account_id': row.account_id,
        'playbook_id': row.playbook_id, 'playbook_version': row.playbook_version, 'action_class': row.action_class,
        'approval_mode': row.approval_mode, 'state': row.state, 'closed_state': row.closed_state, 'urgency': row.urgency,
        'trigger': {'episode_ids': row.trigger_episode_ids, 'node_ids': row.trigger_node_ids, 'roles': row.trigger_roles,
                    'quote': row.trigger_quote, 'facts': row.trigger_facts or [], 'evaluated_as_of': iso(row.evaluated_as_of)},
        'expected_outcome': {'types': row.expected_outcome_types, 'window_days': row.expected_window_days},
        'exposure_revenue': float(row.exposure_revenue) if row.exposure_revenue is not None else None,
        'proposed_at': iso(row.proposed_at), 'proposed_by': row.proposed_by,
        'approved_at': iso(row.approved_at), 'approved_by': row.approved_by, 'approved_by_key_id': row.approved_by_key_id,
        'sent_at': iso(row.sent_at), 'delivery': row.delivery, 'delivery_problem': _delivery_problem(row),
        'started_at': iso(row.started_at), 'last_report_at': iso(row.last_report_at),
        'closed_at': iso(row.closed_at), 'closed_by': row.closed_by,
        'outcome': {'node_id': row.outcome_node_id, 'in_window': row.outcome_in_window, 'expected': row.outcome_expected} if row.outcome_node_id else None,
        'node_id': row.node_id, 'stuck': stuck_days is not None, 'stuck_days': stuck_days, 'notes': row.notes or [],
        'approval_policy': row.approval_policy, 'agent_plan': row.agent_plan,
        'steps': row.steps or [],
        'step': ({'number': row.step_index + 1, 'of': len(row.steps), **{k: row.steps[row.step_index].get(k)
                                                                         for k in ('id', 'label', 'action_class', 'state')}}
                 if row.steps and row.step_index is not None else None),
        'awaiting_approval': row.state == 'proposed' or bool(
            row.state == 'sent' and row.steps and row.step_index is not None and row.steps[row.step_index].get('state') == 'awaiting_approval'),
        'review': {'required': bool(row.review_required), 'reviewed_at': iso(row.reviewed_at), 'reviewed_by': row.reviewed_by,
                   'pending': bool(row.review_required and not row.reviewed_at)},
    }


def health_counts(now: Optional[datetime] = None) -> dict:
    """The platform-wide intervention numbers /health reports, across every tenant:
    {'total', 'by_state': {<each governance state>: n}, 'stuck', 'delivery_problems'} — stuck and delivery
    problem by the same definitions list_interventions uses (_stuck, _delivery_problem)."""
    from extensions import db
    from sqlalchemy import func
    from models import Intervention
    from playbooks.definitions import governance
    gov = governance()
    now = now or datetime.utcnow()
    by_state = {s: 0 for s in gov['states']}
    for state, n in db.session.query(Intervention.state, func.count(Intervention.id)).group_by(Intervention.state).all():
        by_state[state] = int(n)
    after_days = int(gov['stuck_after_days'])
    stuck = delivery_problems = 0
    for r in Intervention.query.filter(Intervention.sent_at.isnot(None)).all():   # only a sent row can be stuck or undelivered
        stuck += int(_stuck(r, now, after_days) is not None)
        delivery_problems += int(_delivery_problem(r))
    return {'total': sum(by_state.values()), 'by_state': by_state, 'stuck': stuck, 'delivery_problems': delivery_problems}


def list_interventions(customer_id: int, account_id: Optional[int] = None, state: Optional[str] = None) -> dict:
    from extensions import db
    from models import Intervention, Account, ContextNode
    from playbooks.definitions import governance, tenant_config
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_vertical_for_customer
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
    # why-panel A4 ("why does this outcome carry protected/expansion/lost revenue"):
    # the same taxonomy.revenue_bucket() journey_builder.py already uses per outcome
    # episode -- computed once here since it needs the tenant's vertical, not per row.
    taxonomy = get_taxonomy(get_vertical_for_customer(int(customer_id))) if outcomes else None
    views, per_playbook = [], {}
    for r in rows:
        v = row_view(r, now)
        v['account_name'] = names.get(r.account_id)
        on = outcomes.get(r.outcome_node_id)
        if on is not None and v['outcome']:
            v['outcome']['bucket'] = taxonomy.revenue_bucket(on.revenue_impact_type or on.node_subtype)
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
            'awaiting_approval': [v['intervention_id'] for v in views if v['awaiting_approval']],
            'awaiting_review': [v['intervention_id'] for v in views if v['review']['pending']],
            'stuck_after_days': int(governance()['stuck_after_days']),
            'by_playbook': sorted(per_playbook.values(), key=lambda s: s['playbook_id']),
            'tenant': tenant_config(customer_id)}


def raise_suggestions(customer_id: int) -> List[dict]:
    """Where the record supports a higher level: per action class, a run of autonomy.suggest_raise_after
    approvals by a person with no decline or failure since. A suggestion only — a person raises a level
    (configure_playbooks); nothing here changes one."""
    from models import Intervention
    from playbooks.definitions import governance, tenant_config, ceiling_for
    gov = governance()
    a = gov['autonomy']
    need = int(a['suggest_raise_after'])
    cfg = tenant_config(customer_id)

    def rank(ac, lvl):                                    # how freely policy acts on a class at a level
        if ac not in a['policy_approves'][str(lvl)]:
            return 0
        return 1 if lvl in a['declared_auto_only'] else 2 if lvl in a['needs_plan_approval'] else 3
    out = []
    for ac in gov['action_classes']:
        cur = cfg['effective_levels'][ac]
        if ac in a['always_human']['action_classes'] or cur >= ceiling_for(ac):
            continue
        higher = [lvl for lvl in sorted(int(k) for k in a['policy_approves'])
                  if cur < lvl <= ceiling_for(ac) and rank(ac, lvl) > rank(ac, cur)]
        if not higher:
            continue                                      # no higher level would change anything for this class
        run = 0
        for r in Intervention.query.filter_by(customer_id=int(customer_id), action_class=ac).order_by(Intervention.id.desc()).all():
            if r.closed_state in ('failed',) or (r.closed_state == 'cancelled' and r.sent_at is None):
                break
            if r.approved_by and not r.approved_by.startswith('policy:'):
                run += 1
        if run >= need:
            out.append({'action_class': ac, 'from': cur, 'to': higher[0], 'clean_approvals': run,
                        'reason': f'the last {run} {ac} proposals a person approved had no decline or failure since; '
                                  f'level {higher[0]}: {gov["automation_levels"][str(higher[0])]}'})
    return out
