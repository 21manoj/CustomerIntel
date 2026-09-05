"""
Playbook definitions and the governance config.

    governance()                         config/playbook_governance.json (every number the layer uses)
    load_vertical(vertical)              validated playbooks for a vertical ([] + note when no file)
    tenant_config(customer_id)           the tenant's overlay: webhook target, disabled playbooks, automation level, kill switch
    playbooks_for_customer(customer_id)  the vertical's playbooks minus the ones the tenant switched off
    validate_all()                       boot / test check over every config/playbooks/*.json

Validation at load (design §2): roles must exist in the vertical's taxonomy,
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
from typing import Dict, List, Optional

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config')
GOVERNANCE_PATH = os.path.join(_CONFIG_DIR, 'playbook_governance.json')
PLAYBOOKS_DIR = os.path.join(_CONFIG_DIR, 'playbooks')
TOGGLE_NAME = 'playbooks'          # FeatureToggle.feature_name for the tenant overlay


class PlaybookConfigError(ValueError):
    pass


@lru_cache(maxsize=1)
def governance() -> dict:
    with open(GOVERNANCE_PATH, encoding='utf-8') as f:
        return json.load(f)


def reset_cache() -> None:
    governance.cache_clear()
    load_vertical.cache_clear()


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
    if not roles or not isinstance(roles, list):
        raise PlaybookConfigError(f'{name}: trigger.roles must be a non-empty list')
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
    rwd = trig.get('renewal_within_days')
    if rwd is not None and (not isinstance(rwd, int) or rwd <= 0):
        raise PlaybookConfigError(f'{name}: renewal_within_days must be a positive integer')
    ac = pb.get('action_class')
    if ac not in gov['action_classes']:
        raise PlaybookConfigError(f"{name}: action_class must be one of {gov['action_classes']}")
    ap = pb.get('approval')
    if ap not in gov['approval_modes']:
        raise PlaybookConfigError(f"{name}: approval must be one of {gov['approval_modes']}")
    if ap == 'auto' and ac not in gov['auto_approval_allowed_for']:
        raise PlaybookConfigError(f"{name}: approval=auto is allowed only for {gov['auto_approval_allowed_for']}, not {ac!r}")
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
        'trigger': {'roles': list(roles), 'roles_match': match, 'urgency_floor': floor, 'renewal_within_days': rwd},
        'action_class': ac, 'approval': ap,
        'expected_outcome': {'types': list(types), 'window_days': win},
    }


@lru_cache(maxsize=16)
def load_vertical(vertical: str) -> dict:
    """{'vertical', 'version', 'playbooks': [...], 'source': path | None}. No file → no playbooks (and says so)."""
    from utils.taxonomy_loader import get_taxonomy
    path = os.path.join(PLAYBOOKS_DIR, f'{vertical}.json')
    if not os.path.exists(path):
        return {'vertical': vertical, 'version': None, 'playbooks': [], 'source': None,
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
    return {'vertical': vertical, 'version': version, 'playbooks': playbooks, 'source': path}


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
    return {
        'webhook_url': cfg.get('webhook_url'),
        'webhook_secret_set': bool(cfg.get('webhook_secret')),
        'disabled_playbooks': sorted(cfg.get('disabled_playbooks') or []),
        'automation_level': int(cfg.get('automation_level', gov['default_automation_level'])),
        'automation_level_meaning': gov['automation_levels'][str(int(cfg.get('automation_level', gov['default_automation_level'])))],
        'kill_switch': bool(t is not None and not t.enabled),
    }


def tenant_secret(customer_id: int) -> Optional[str]:
    t = _toggle(customer_id)
    return ((t.config or {}).get('webhook_secret') or None) if t else None


def configure_tenant(customer_id: int, *, webhook_url=None, webhook_secret=None, disabled_playbooks=None,
                     automation_level=None, kill_switch=None) -> dict:
    """Write the overlay. Only the fields given change. Validates the URL scheme and the level."""
    from extensions import db
    from models import FeatureToggle
    from urllib.parse import urlparse
    gov = governance()
    t = _toggle(customer_id)
    if not t:
        t = FeatureToggle(customer_id=int(customer_id), feature_name=TOGGLE_NAME, enabled=True, config={},
                          description='playbook governance: webhook target, switched-off playbooks, automation level; enabled=false is the kill switch')
        db.session.add(t)
    cfg = dict(t.config or {})
    if webhook_url is not None:
        url = str(webhook_url).strip()
        if url:
            u = urlparse(url)
            allowed = list(gov['webhook']['allowed_schemes'])
            if os.environ.get(gov['webhook']['insecure_http_env'], '').lower() in ('true', '1', 'yes'):
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
    if disabled_playbooks is not None:
        vertical = _vertical(customer_id)
        known = {p['id'] for p in load_vertical(vertical)['playbooks']}
        bad = [p for p in disabled_playbooks if p not in known]
        if bad:
            raise ValueError(f'unknown playbook ids for vertical {vertical!r}: {bad} (known: {sorted(known)})')
        cfg['disabled_playbooks'] = sorted(set(disabled_playbooks))
    if automation_level is not None:
        if str(int(automation_level)) not in gov['automation_levels']:
            raise ValueError(f"automation_level must be one of {sorted(gov['automation_levels'])}")
        cfg['automation_level'] = int(automation_level)
    if kill_switch is not None:
        t.enabled = not bool(kill_switch)
    t.config = cfg
    db.session.commit()
    return tenant_config(customer_id)


def _vertical(customer_id: int) -> str:
    from utils.vertical_registry import get_vertical_for_customer
    return get_vertical_for_customer(int(customer_id))


def playbooks_for_customer(customer_id: int) -> dict:
    """The vertical's validated playbooks with the tenant's switched-off ones removed (listed under 'disabled')."""
    vertical = _vertical(customer_id)
    base = load_vertical(vertical)
    cfg = tenant_config(customer_id)
    off = set(cfg['disabled_playbooks'])
    return {
        'vertical': vertical, 'version': base['version'], 'source': base['source'], 'note': base.get('note'),
        'playbooks': [p for p in base['playbooks'] if p['id'] not in off],
        'disabled': sorted(off), 'tenant': cfg,
    }
