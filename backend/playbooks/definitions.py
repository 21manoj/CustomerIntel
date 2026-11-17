"""
Playbook definitions and the governance config.

    governance()                         config/playbook_governance.json (every number the layer uses)
    load_vertical(vertical)              validated playbooks for a vertical ([] + note when no file)
    tenant_config(customer_id)           the tenant's overlay: webhook target, Slack URL (set/unset only), disabled playbooks,
                                         automation level, kill switch
    playbooks_for_customer(customer_id)  the vertical's playbooks minus the ones the tenant switched off
    validate_all()                       boot / test check over every config/playbooks/*.json

Validation at load (design §2): a trigger needs at least one of roles, kpi
or date (config/playbook_governance.json → fact_triggers); roles must exist in
the vertical's taxonomy, kpi codes in its KPI catalog,
outcome types in its revenue buckets (the check log_outcome makes),
action_class / approval / roles_match / urgency_floor from the governance
enums, approval=auto only for the classes the config allows, and a label that
never carries the INTERVENTION title separator (the narrative cuts there).
"""
from __future__ import annotations

import glob
import json
import os
from functools import lru_cache
from typing import List, Optional

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config')
GOVERNANCE_PATH = os.path.join(_CONFIG_DIR, 'playbook_governance.json')
PLAYBOOKS_DIR = os.path.join(_CONFIG_DIR, 'playbooks')
TOGGLE_NAME = 'playbooks'          # FeatureToggle.feature_name for the tenant overlay
TOGGLE_DESCRIPTION = ('playbook governance: webhook target, switched-off playbooks, automation levels, approved plans; '
                      'enabled=false is the stop switch')


class PlaybookConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def governance() -> dict:
    with open(GOVERNANCE_PATH, encoding='utf-8') as f:
        return json.load(f)


def reset_cache() -> None:
    governance.cache_clear()
    load_vertical.cache_clear()


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_kpi_trigger(trig: dict, name: str, vertical: str, gov: dict) -> tuple:
    """trigger.kpi → ([{code, op, value, periods}], kpi_match). Codes must be in the vertical's KPI catalog."""
    from utils.vertical_registry import get_kpis
    ft = gov['fact_triggers']
    conds = trig.get('kpi')
    kpi_match = trig.get('kpi_match', 'any')
    if conds is None:
        return [], kpi_match
    if not isinstance(conds, list) or not conds:
        raise PlaybookConfigError(f'{name}: trigger.kpi must be a non-empty list of {{code, op, value}}')
    if kpi_match not in ft['kpi_match_modes']:
        raise PlaybookConfigError(f"{name}: kpi_match must be one of {ft['kpi_match_modes']}")
    known = get_kpis(vertical) or {}
    out = []
    for c in conds:
        if not isinstance(c, dict):
            raise PlaybookConfigError(f'{name}: each trigger.kpi entry must be an object {{code, op, value}}')
        code = c.get('code')
        if code not in known:
            raise PlaybookConfigError(f'{name}: trigger kpi code {code!r} is not in the {vertical} KPI catalog')
        if c.get('op') not in ft['kpi_ops']:
            raise PlaybookConfigError(f"{name}: trigger kpi op must be one of {ft['kpi_ops']}")
        if not _is_number(c.get('value')):
            raise PlaybookConfigError(f'{name}: trigger kpi value must be a number')
        periods = c.get('periods', 1)
        if not isinstance(periods, int) or isinstance(periods, bool) or not 1 <= periods <= int(ft['kpi_max_periods']):
            raise PlaybookConfigError(f"{name}: trigger kpi periods must be an integer from 1 to {ft['kpi_max_periods']}")
        out.append({'code': code, 'op': c['op'], 'value': float(c['value']), 'periods': periods,
                    'name': (known[code] or {}).get('name') or code})
    return out, kpi_match


def _validate_date_trigger(trig: dict, name: str):
    """trigger.date → {field, within_days | overdue_by_days} or None. Exactly one of the two windows."""
    d = trig.get('date')
    if d is None:
        return None
    if not isinstance(d, dict) or not isinstance(d.get('field'), str) or not d['field'].strip():
        raise PlaybookConfigError(f'{name}: trigger.date must be an object with a field name')
    within, overdue = d.get('within_days'), d.get('overdue_by_days')
    if (within is None) == (overdue is None):
        raise PlaybookConfigError(f'{name}: trigger.date needs exactly one of within_days or overdue_by_days')
    n = within if within is not None else overdue
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise PlaybookConfigError(f'{name}: trigger.date within_days / overdue_by_days must be a whole number of days, 0 or more')
    return {'field': d['field'].strip(), 'within_days': within, 'overdue_by_days': overdue,
            'label': (d.get('label') or d['field'].replace('_', ' ')).strip()}


def _validate_effects(effects, name: str, gov: dict) -> list:
    """effects: what an action touches beyond a message or a meeting (governance → autonomy.always_human.effects).
    Anything declared here always waits for a person, at every automation level."""
    if effects in (None, []):
        return []
    known = gov['autonomy']['always_human']['effects']
    if not isinstance(effects, list) or any(e not in known for e in effects):
        raise PlaybookConfigError(f'{name}: effects must be a list drawn from {known}')
    return sorted(set(effects))


def _validate_steps(steps, name: str, gov: dict) -> list:
    """steps: an optional ordered plan, [{id, label?, action_class, effects?}]. Each step is approved, sent and
    reported on its own; the next one starts when the one before is reported done. No steps = one action."""
    if steps in (None, []):
        return []
    limit = int(gov['autonomy']['max_steps'])
    if not isinstance(steps, list) or len(steps) < 2 or len(steps) > limit:
        raise PlaybookConfigError(f'{name}: steps must be a list of 2 to {limit} steps (leave it out for a single action)')
    out, seen = [], set()
    for i, st in enumerate(steps):
        if not isinstance(st, dict) or not isinstance(st.get('id'), str) or not st['id'].strip():
            raise PlaybookConfigError(f'{name}: step {i + 1} needs a string id')
        sid = st['id'].strip()
        if sid in seen:
            raise PlaybookConfigError(f'{name}: duplicate step id {sid!r}')
        seen.add(sid)
        if st.get('action_class') not in gov['action_classes']:
            raise PlaybookConfigError(f"{name}: step {sid!r} action_class must be one of {gov['action_classes']}")
        out.append({'id': sid, 'label': (st.get('label') or sid.replace('_', ' ')).strip(), 'action_class': st['action_class'],
                    'effects': _validate_effects(st.get('effects'), f'{name} step {sid!r}', gov)})
    return out


def _validate_always_human(block, vertical: str) -> dict:
    """The vertical's own always-human rule: account profile fields that, when set on an account, keep every
    action on that account with a person (a regulated service, a named-executive account...)."""
    block = block or {}
    flags = block.get('account_flags') or []
    if not isinstance(block, dict) or not isinstance(flags, list) or any(not isinstance(f, str) or not f.strip() for f in flags):
        raise PlaybookConfigError(f'{vertical}.json: always_human.account_flags must be a list of account profile field names')
    return {'account_flags': [f.strip() for f in flags]}


def _validate_playbook(pb: dict, vertical: str, taxonomy, gov: dict, seen: set) -> dict:
    from signal_engine.urgency import LEVELS
    from playbooks.governance import TITLE_SEP
    name = f"{vertical}.json playbook {pb.get('id')!r}"
    pid = pb.get('id')
    if not pid or not isinstance(pid, str):
        raise PlaybookConfigError(f'{vertical}.json: every playbook needs a string id')
    if pid in seen:
        raise PlaybookConfigError(f'{name}: duplicate id')
    seen.add(pid)
    label = pb.get('label') or pid.replace('_', ' ')
    if TITLE_SEP in label:
        raise PlaybookConfigError(f'{name}: label must not contain {TITLE_SEP!r} — the INTERVENTION node title is '
                                  f'"<label>{TITLE_SEP}<account>" and the narrative cuts at the first one (use a colon)')
    trig = pb.get('trigger') or {}
    roles = trig.get('roles') or []
    if not isinstance(roles, list):
        raise PlaybookConfigError(f'{name}: trigger.roles must be a list')
    kpi, kpi_match = _validate_kpi_trigger(trig, name, vertical, gov)
    date = _validate_date_trigger(trig, name)
    if not roles and not kpi and not date:
        raise PlaybookConfigError(f'{name}: a trigger needs at least one of roles (non-empty list), kpi or date')
    unknown = [r for r in roles if r not in taxonomy.signal_roles]
    if unknown:
        raise PlaybookConfigError(f'{name}: trigger roles not in the {vertical} taxonomy: {unknown} '
                                  f'(known: {sorted(taxonomy.signal_roles)})')
    match = trig.get('roles_match', 'any')
    if match not in gov['roles_match_modes']:
        raise PlaybookConfigError(f"{name}: roles_match must be one of {gov['roles_match_modes']}")
    floor = trig.get('urgency_floor')
    if floor is not None and floor not in LEVELS:
        raise PlaybookConfigError(f'{name}: urgency_floor must be one of {list(LEVELS)}')
    if floor is not None and not roles:
        raise PlaybookConfigError(f'{name}: urgency_floor reads the cited communications, so it needs trigger.roles')
    urgency = trig.get('urgency')
    if urgency is not None and urgency not in LEVELS:
        raise PlaybookConfigError(f'{name}: trigger.urgency must be one of {list(LEVELS)}')
    rwd = trig.get('renewal_within_days')
    if rwd is not None and (not isinstance(rwd, int) or isinstance(rwd, bool) or rwd <= 0):
        raise PlaybookConfigError(f'{name}: renewal_within_days must be a positive integer')
    ac = pb.get('action_class')
    if ac not in gov['action_classes']:
        raise PlaybookConfigError(f"{name}: action_class must be one of {gov['action_classes']}")
    ap = pb.get('approval')
    if ap not in gov['approval_modes']:
        raise PlaybookConfigError(f"{name}: approval must be one of {gov['approval_modes']}")
    if ap == 'auto' and ac not in gov['auto_approval_allowed_for']:
        raise PlaybookConfigError(f"{name}: approval=auto is allowed only for {gov['auto_approval_allowed_for']}, not {ac!r}")
    effects = _validate_effects(pb.get('effects'), name, gov)
    steps = _validate_steps(pb.get('steps'), name, gov)
    if ap == 'auto' and any(st['action_class'] not in gov['auto_approval_allowed_for'] for st in steps):
        raise PlaybookConfigError(f"{name}: approval=auto needs every step to be one of {gov['auto_approval_allowed_for']}")
    eo = pb.get('expected_outcome') or {}
    types = eo.get('types') or []
    if not types or not isinstance(types, list):
        raise PlaybookConfigError(f'{name}: expected_outcome.types must be a non-empty list')
    bad = [t for t in types if not taxonomy.revenue_bucket(t)]
    if bad:
        raise PlaybookConfigError(f'{name}: expected outcome types not in the {vertical} revenue buckets: {bad}')
    win = eo.get('window_days')
    if not isinstance(win, int) or win <= 0:
        raise PlaybookConfigError(f'{name}: expected_outcome.window_days must be a positive integer')
    return {
        'id': pid, 'label': label,
        'trigger': {'roles': list(roles), 'roles_match': match, 'urgency_floor': floor, 'renewal_within_days': rwd,
                    'kpi': kpi, 'kpi_match': kpi_match, 'date': date, 'urgency': urgency},
        'action_class': ac, 'approval': ap, 'effects': effects, 'steps': steps,
        'expected_outcome': {'types': list(types), 'window_days': win},
    }


@lru_cache(maxsize=16)
def load_vertical(vertical: str) -> dict:
    """{'vertical', 'version', 'playbooks': [...], 'source': path | None}. No file → no playbooks (and says so)."""
    from utils.taxonomy_loader import get_taxonomy
    path = os.path.join(PLAYBOOKS_DIR, f'{vertical}.json')
    if not os.path.exists(path):
        return {'vertical': vertical, 'version': None, 'playbooks': [], 'source': None, 'always_human': {'account_flags': []},
                'note': f'no playbook definitions for vertical {vertical!r} (config/playbooks/{vertical}.json)'}
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if data.get('vertical') != vertical:
        raise PlaybookConfigError(f'{vertical}.json declares vertical {data.get("vertical")!r}')
    version = str(data.get('version') or '')
    if not version:
        raise PlaybookConfigError(f'{vertical}.json: version is required')
    taxonomy = get_taxonomy(vertical)
    gov = governance()
    seen: set = set()
    playbooks = [_validate_playbook(pb, vertical, taxonomy, gov, seen) for pb in (data.get('playbooks') or [])]
    return {'vertical': vertical, 'version': version, 'playbooks': playbooks, 'source': path,
            'always_human': _validate_always_human(data.get('always_human'), vertical)}


def validate_all() -> List[str]:
    """Load every vertical file; returns the verticals validated. Raises on the first bad file."""
    out = []
    for path in sorted(glob.glob(os.path.join(PLAYBOOKS_DIR, '*.json'))):
        v = os.path.splitext(os.path.basename(path))[0]
        load_vertical(v)
        out.append(v)
    governance()
    return out


# ── tenant overlay ─────────────────────────────────────────────────────

def _toggle(customer_id: int):
    from models import FeatureToggle
    return FeatureToggle.query.filter_by(customer_id=int(customer_id), feature_name=TOGGLE_NAME).first()


def tenant_config(customer_id: int) -> dict:
    """The tenant's overlay with the secret masked. Absent row = defaults (proposals only, nothing sent)."""
    gov = governance()
    t = _toggle(customer_id)
    cfg = dict((t.config or {}) if t else {})
    level = int(cfg.get('automation_level', gov['default_automation_level']))
    by_class = {k: int(v) for k, v in (cfg.get('automation_levels_by_class') or {}).items()}
    return {
        'webhook_url': cfg.get('webhook_url'),
        'webhook_secret_set': bool(cfg.get('webhook_secret')),
        'slack_webhook_url_set': bool(cfg.get('slack_webhook_url')),      # the URL is the secret: never returned
        'disabled_playbooks': sorted(cfg.get('disabled_playbooks') or []),
        'automation_level': level,
        'automation_level_meaning': gov['automation_levels'][str(level)],
        'automation_levels_by_class': by_class,
        'effective_levels': {ac: effective_level({'automation_level': level, 'automation_levels_by_class': by_class}, ac)
                             for ac in gov['action_classes']},
        'ceiling': int(gov['autonomy']['ceiling']),
        'approved_plans': dict(cfg.get('approved_plans') or {}),
        'autonomy_log': list(cfg.get('autonomy_log') or []),
        'kill_switch': bool(t is not None and not t.enabled),
    }


def ceiling_for(action_class: str) -> int:
    """The platform's limit for an action class: the lower of autonomy.ceiling and its ceiling_by_class entry."""
    a = governance()['autonomy']
    return min(int(a['ceiling']), int((a.get('ceiling_by_class') or {}).get(action_class, a['ceiling'])))


def effective_level(cfg: dict, action_class: str) -> int:
    """The level that governs one action class for a tenant: its per-class level when set, else the tenant's
    level, never above the platform ceiling for that class."""
    by_class = cfg.get('automation_levels_by_class') or {}
    level = by_class.get(action_class, cfg.get('automation_level', governance()['default_automation_level']))
    return max(0, min(int(level), ceiling_for(action_class)))


EVENTS_KEY = '_events'             # settings changes waiting to be written as business events; never stored


def _event(cfg: dict, entry: dict) -> None:
    """Queue a settings change for the business event log. _flush_events writes it in the commit that
    stores the change, so the two exist together or not at all."""
    cfg.setdefault(EVENTS_KEY, []).append(entry)


def _flush_events(customer_id: int, cfg: dict, ctx: Optional[dict] = None) -> None:
    from playbooks import events
    for entry in cfg.pop(EVENTS_KEY, []):
        events.record_setting(customer_id, entry, ctx)


def _log(cfg: dict, entry: dict) -> None:
    keep = int(governance()['autonomy']['log_keep'])
    cfg['autonomy_log'] = (list(cfg.get('autonomy_log') or []) + [entry])[-keep:]
    _event(cfg, entry)


def _write(customer_id: int, mutate, event_ctx: Optional[dict] = None) -> dict:
    """Read-modify-write of the overlay's config (creates the row when absent). mutate(cfg) edits in place.
    event_ctx = {actor, row}: who caused the change and for which intervention, for the business event."""
    from extensions import db
    from models import FeatureToggle
    t = _toggle(customer_id)
    if not t:
        t = FeatureToggle(customer_id=int(customer_id), feature_name=TOGGLE_NAME, enabled=True, config={}, description=TOGGLE_DESCRIPTION)
        db.session.add(t)
    cfg = dict(t.config or {})
    mutate(cfg)
    _flush_events(customer_id, cfg, event_ctx)
    t.config = cfg
    db.session.commit()
    return tenant_config(customer_id)


def record_plan_approval(customer_id: int, playbook_id: str, version: str, by: str, event_ctx: Optional[dict] = None) -> bool:
    """A person approved this playbook for the tenant: remembered per playbook version, which is what lets
    policy approve the same plan later at the levels in autonomy.needs_plan_approval. True when newly recorded."""
    from datetime import datetime
    cur = tenant_config(customer_id)['approved_plans'].get(playbook_id) or {}
    if cur.get('version') == version:
        return False
    at = datetime.utcnow().isoformat()

    def mutate(cfg):
        cfg['approved_plans'] = {**(cfg.get('approved_plans') or {}), playbook_id: {'version': version, 'by': by, 'at': at}}
        _log(cfg, {'at': at, 'event': 'plan_approved', 'playbook_id': playbook_id, 'version': version, 'by': by})
    _write(customer_id, mutate, event_ctx)
    return True


def lower_level(customer_id: int, action_class: str, reason: str, detail: str, playbook_id: Optional[str] = None,
                event_ctx: Optional[dict] = None) -> Optional[dict]:
    """A declined proposal or a failed report: the level for that action class drops by autonomy.lower_by, never
    below lower_floor, and the playbook's plan approval (if named) is withdrawn. Returns {action_class, from, to,
    reason, plan_approval_withdrawn} or None when nothing changed (already at the floor, no plan approval). Only configure_tenant (a person) raises it again."""
    from datetime import datetime
    a = governance()['autonomy']
    if reason not in a['lower_on']:
        return None
    before = tenant_config(customer_id)
    cur = effective_level(before, action_class)
    new = max(int(a['lower_floor']), cur - int(a['lower_by']))
    had_plan = bool(playbook_id and playbook_id in before['approved_plans'])
    if new >= cur and not had_plan:
        return None
    at = datetime.utcnow().isoformat()
    change = {'action_class': action_class, 'from': cur, 'to': min(new, cur), 'reason': reason,
              'plan_approval_withdrawn': playbook_id if had_plan else None}

    def mutate(cfg):
        if new < cur:
            cfg['automation_levels_by_class'] = {**(cfg.get('automation_levels_by_class') or {}), action_class: new}
        if had_plan:
            cfg['approved_plans'] = {k: v for k, v in (cfg.get('approved_plans') or {}).items() if k != playbook_id}
        _log(cfg, {'at': at, 'event': 'lowered', **change, 'detail': detail})
    _write(customer_id, mutate, event_ctx)
    return change


def tenant_secret(customer_id: int) -> Optional[str]:
    t = _toggle(customer_id)
    return ((t.config or {}).get('webhook_secret') or None) if t else None


def insecure_http_allowed() -> bool:
    """The one switch that lets a plain-http target through (webhook_url, the Slack URL): the env named
    by governance → webhook.insecure_http_env. Tests and a local fake endpoint; never set on the box."""
    return os.environ.get(governance()['webhook']['insecure_http_env'], '').lower() in ('true', '1', 'yes')


def tenant_slack_url(customer_id: int) -> Optional[str]:
    """The Slack incoming-webhook URL (adapters/slack_notify) — for the send path only, never a read surface."""
    t = _toggle(customer_id)
    return ((t.config or {}).get('slack_webhook_url') or None) if t else None


def configure_tenant(customer_id: int, *, webhook_url=None, webhook_secret=None, disabled_playbooks=None,
                     automation_level=None, kill_switch=None, slack_webhook_url=None,
                     automation_levels_by_class=None, changed_by: Optional[str] = None) -> dict:
    """Write the overlay. Only the fields given change. Validates the URL schemes (webhook: https unless the
    insecure-http env is set; Slack: adapters.slack_notify.validate_url) and the level."""
    from extensions import db
    from models import FeatureToggle
    from urllib.parse import urlparse
    gov = governance()
    t = _toggle(customer_id)
    if not t:
        t = FeatureToggle(customer_id=int(customer_id), feature_name=TOGGLE_NAME, enabled=True, config={}, description=TOGGLE_DESCRIPTION)
        db.session.add(t)
    cfg = dict(t.config or {})
    if webhook_url is not None:
        url = str(webhook_url).strip()
        if url:
            u = urlparse(url)
            allowed = list(gov['webhook']['allowed_schemes'])
            if insecure_http_allowed():
                allowed.append('http')
            if u.scheme not in allowed or not u.netloc:
                raise ValueError(f'webhook_url must be an absolute {"/".join(allowed)} URL')
            cfg['webhook_url'] = url
        else:
            cfg.pop('webhook_url', None)
    if webhook_secret is not None:
        if str(webhook_secret).strip():
            cfg['webhook_secret'] = str(webhook_secret).strip()
        else:
            cfg.pop('webhook_secret', None)
    if cfg.get('webhook_url') and not cfg.get('webhook_secret'):
        raise ValueError('a webhook_secret is required with a webhook_url (payloads are signed)')
    if slack_webhook_url is not None:
        if str(slack_webhook_url).strip():
            from adapters.slack_notify import validate_url
            cfg['slack_webhook_url'] = validate_url(str(slack_webhook_url))
        else:
            cfg.pop('slack_webhook_url', None)
    if disabled_playbooks is not None:
        vertical = _vertical(customer_id)
        known = {p['id'] for p in load_vertical(vertical)['playbooks']}
        bad = [p for p in disabled_playbooks if p not in known]
        if bad:
            raise ValueError(f'unknown playbook ids for vertical {vertical!r}: {bad} (known: {sorted(known)})')
        was, now_off = set(cfg.get('disabled_playbooks') or []), set(disabled_playbooks)
        cfg['disabled_playbooks'] = sorted(now_off)
    from datetime import datetime
    at = datetime.utcnow().isoformat()
    if disabled_playbooks is not None:                        # not in the automation log; the business event log has them
        for p in sorted(now_off - was):
            _event(cfg, {'at': at, 'event': 'playbook_disabled', 'playbook_id': p, 'by': changed_by})
        for p in sorted(was - now_off):
            _event(cfg, {'at': at, 'event': 'playbook_enabled', 'playbook_id': p, 'by': changed_by})
    ceiling = int(gov['autonomy']['ceiling'])
    if automation_level is not None:
        if isinstance(automation_level, bool) or str(int(automation_level)) not in gov['automation_levels']:
            raise ValueError(f"automation_level must be one of {sorted(gov['automation_levels'])}")
        if int(automation_level) > ceiling:
            raise ValueError(f'automation_level {int(automation_level)} is above the platform ceiling ({ceiling})')
        if int(automation_level) != int(cfg.get('automation_level', gov['default_automation_level'])):
            _log(cfg, {'at': at, 'event': 'level_set', 'from': int(cfg.get('automation_level', gov['default_automation_level'])),
                       'to': int(automation_level), 'by': changed_by})
        cfg['automation_level'] = int(automation_level)
    if automation_levels_by_class is not None:
        if not isinstance(automation_levels_by_class, dict):
            raise ValueError('automation_levels_by_class must be an object {action_class: level | null}')
        by_class = dict(cfg.get('automation_levels_by_class') or {})
        for ac, lvl in automation_levels_by_class.items():
            if ac not in gov['action_classes']:
                raise ValueError(f"unknown action class {ac!r} (known: {gov['action_classes']})")
            if lvl is None:                                   # null = follow the tenant's level again
                if ac in by_class:
                    _log(cfg, {'at': at, 'event': 'class_level_cleared', 'action_class': ac, 'from': by_class[ac], 'by': changed_by})
                by_class.pop(ac, None)
                continue
            if isinstance(lvl, bool) or str(int(lvl)) not in gov['automation_levels']:
                raise ValueError(f"level for {ac!r} must be one of {sorted(gov['automation_levels'])}")
            if int(lvl) > ceiling_for(ac):
                raise ValueError(f'level {int(lvl)} for {ac!r} is above the platform ceiling for that class ({ceiling_for(ac)})')
            if by_class.get(ac) != int(lvl):
                _log(cfg, {'at': at, 'event': 'class_level_set', 'action_class': ac, 'from': by_class.get(ac), 'to': int(lvl), 'by': changed_by})
            by_class[ac] = int(lvl)
        cfg['automation_levels_by_class'] = by_class
    if kill_switch is not None:
        if bool(kill_switch) != (not t.enabled):
            _log(cfg, {'at': at, 'event': 'stop_switch_on' if kill_switch else 'stop_switch_off', 'by': changed_by})
        t.enabled = not bool(kill_switch)
    _flush_events(customer_id, cfg)
    t.config = cfg
    db.session.commit()
    return tenant_config(customer_id)


def _vertical(customer_id: int) -> str:
    from utils.vertical_registry import get_vertical_for_customer
    return get_vertical_for_customer(int(customer_id))


def playbooks_for_customer(customer_id: int, suggestions: bool = True) -> dict:
    """The vertical's validated playbooks with the tenant's switched-off ones removed (listed under 'disabled')."""
    vertical = _vertical(customer_id)
    base = load_vertical(vertical)
    cfg = tenant_config(customer_id)
    off = set(cfg['disabled_playbooks'])
    return {
        'vertical': vertical, 'version': base['version'], 'source': base['source'], 'note': base.get('note'),
        'playbooks': [p for p in base['playbooks'] if p['id'] not in off],
        'disabled': sorted(off), 'tenant': cfg, 'always_human': base.get('always_human') or {'account_flags': []},
        'autonomy': {'suggested_raises': _suggestions(customer_id) if suggestions else []},
    }


def _suggestions(customer_id: int) -> list:
    from playbooks.governance import raise_suggestions
    return raise_suggestions(customer_id)
