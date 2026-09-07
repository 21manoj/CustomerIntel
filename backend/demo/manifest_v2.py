"""
Manifest schema v2 — communications, not typed signals.

A v2 manifest (``"version": 2`` or any account carrying ``communications``)
declares per account the COMMUNICATIONS the demo needs — raw text with a
source, a day, the people on it and the subtypes a correct extractor
should read out of it. The generator submits each one through the real
signal engine (``signal_engine.pipeline.ingest`` → ``process_pending``);
the taxonomy-typed evidence the journey sees is whatever the engine
extracted, and the manifest's ``expected_subtypes`` are the LABELS the
scorecard compares against. Nothing here writes typed signal rows.

    {
      "version": 2,
      "vertical": "saas_premium", "kpis": "none",        # kpis: omit for the KPI layer, "none" for signals-only (P1)
      "accounts": [{
        "source_account_id": "NORTHWIND", "name": ..., "arr": ..., "champion": ..., "renewal_day": 0,
        "health": {...},                                  # required unless kpis == "none"
        "communications": [{
          "day": -106, "source_type": "crm_activity",     # manual|email|slack|ticket|transcript|meeting|crm_activity|external
          "text": "1-6 sentences in the vertical's own language",
          "participants": [{"name": "Elena Rossi", "title": "VP Data"}],
          "source_ref": "crm:evt:8812",                   # optional
          "expected_subtypes": ["champion_departure"],    # every one must exist in the vertical's taxonomy
          "expected_sentiment": -0.7                      # optional label; the oracle uses it, a model ignores it
        }],
        "crm_flag_day": -40,                              # the CSM's own flag: structured path, stays declared
        "events": [{"day": 0, "type": "contraction", "amount": -144000, "linked_communication_index": 0, ...}]
      }],
      "background": {...},                                # as v1; background accounts get generated communications

      # OPTIONAL — the driven story (scripts/drive_tenant_story.py). A playbook
      # evaluates the LATEST leading month only, so a tenant loaded in one shot can
      # fire on its final month and nothing else. Tranches feed it the way a real
      # tenant lives, and the product reacts to each arrival before the next lands.
      "tranches":      [{"id": "t1", "through_day": -294}, ...],     # ascending; demo.generate.slice_manifest
      "interventions": [{"tranche": "t1", "source_account_id": "CEREBRIX",
                         "playbook_id": "incident_escalation",       # must exist for the vertical
                         "approve_note": "...",
                         "report": {"state": "done",                 # done | failed | cancelled
                                    "outcome_type": "revenue_protected",   # the vertical's revenue vocabulary
                                    "outcome_day": -268, "revenue": 8400000, "note": "..."}}]
    }

Validation is at load and fails loudly: unknown subtype for the
vertical, unknown source type, unsorted days, a communication with no
named participant, duplicate text on one account (the engine would
dedup it silently), a linked event index out of range, a KPI-layer
account without a health curve, an unknown or malformed health shape,
and — for the driven story — a tranche out of order, an intervention
naming a playbook the vertical does not have, an account the manifest
does not have, or an outcome type outside the vertical's revenue buckets.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Dict, List

from signal_engine.pipeline import SOURCE_TYPES, normalize_text

SCHEMA_VERSION = 2
KPIS_NONE = 'none'
COMM_TIME_OF_DAY = {'hour': 10}      # occurred_at = t0 + day at 10:00 (deterministic, inside the month)


class ManifestError(ValueError):
    """A manifest that must not be generated from. Message says exactly what and where."""


def is_v2(manifest: dict) -> bool:
    if manifest.get('version') == SCHEMA_VERSION:
        return True
    return any('communications' in a for a in manifest.get('accounts', []))


def signals_only(manifest: dict) -> bool:
    return str(manifest.get('kpis', '')).lower() == KPIS_NONE


def comm_ref(source_account_id: str, index: int) -> str:
    """Deterministic id of a communication inside the manifest — what
    outcomes.csv links to before the engine assigns the real signal id."""
    return f'{source_account_id.lower()}_comm_{index + 1}'


def occurred_at(t0: datetime, day: int) -> datetime:
    return (t0 + timedelta(days=int(day))).replace(**COMM_TIME_OF_DAY, minute=0, second=0, microsecond=0)


# ═══════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════

def _sentence_count(text: str) -> int:
    return len([s for s in re.split(r'[.!?]+(?:\s|$)', text.strip()) if s.strip()])


HEALTH_SHAPES = ('flat', 'ramp', 'decline', 'dip_recover', 'waypoints')
_SHAPE_REQUIRES = {'decline': ('start', 'trailing_cross_day'), 'dip_recover': ('start', 'dip_day', 'dip'),
                   'ramp': ('start', 'end'), 'flat': ('start',), 'waypoints': ('points',)}


def validate_health(spec: dict, where: str) -> None:
    """The curve demo.generate.health_at will be asked to walk. Checked here so a
    bad shape fails at load with the account's name, not mid-generation."""
    shape = spec.get('shape', 'flat')
    if shape not in HEALTH_SHAPES:
        raise ManifestError(f'{where}: health.shape {shape!r} is not one of {list(HEALTH_SHAPES)}')
    missing = [k for k in _SHAPE_REQUIRES[shape] if k not in spec]
    if missing:
        raise ManifestError(f'{where}: health.shape {shape!r} needs {missing}')
    if shape != 'waypoints':
        return
    pts = spec['points']
    if not isinstance(pts, list) or len(pts) < 2:
        raise ManifestError(f'{where}: health.points must be a list of at least two [day, health] pairs')
    days = []
    for p in pts:
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            raise ManifestError(f'{where}: health.points entries must be [day, health], got {p!r}')
        if not 0 <= float(p[1]) <= 100:
            raise ManifestError(f'{where}: health.points health {p[1]!r} must be within [0, 100]')
        days.append(int(p[0]))
    if len(set(days)) != len(days):
        raise ManifestError(f'{where}: health.points has duplicate days {sorted(days)}')


def validate_manifest(manifest: dict) -> None:
    """Raise ManifestError on the first problem. v1 manifests pass through."""
    if not is_v2(manifest):
        return
    from utils.taxonomy_loader import get_taxonomy
    where = manifest.get('manifest_id', '<manifest>')
    vertical = manifest.get('vertical')
    if not vertical:
        raise ManifestError(f'{where}: vertical is required')
    taxonomy = get_taxonomy(vertical)
    known = set(taxonomy.all_subtypes())
    if 'kpis' in manifest and not signals_only(manifest):
        raise ManifestError(f'{where}: "kpis" may only be "{KPIS_NONE}" (omit it for the KPI layer)')
    if 'timeline' not in manifest or 't0' not in manifest['timeline'] or 'history_days' not in manifest['timeline']:
        raise ManifestError(f'{where}: timeline.t0 and timeline.history_days are required')
    if not manifest.get('accounts'):
        raise ManifestError(f'{where}: at least one account is required')

    seen_ids = set()
    for a in manifest['accounts']:
        sid = a.get('source_account_id') or a.get('name')
        if not sid:
            raise ManifestError(f'{where}: an account has neither source_account_id nor name')
        if sid in seen_ids:
            raise ManifestError(f'{where}: duplicate source_account_id {sid!r}')
        seen_ids.add(sid)
        if 'arr' not in a:
            raise ManifestError(f'{where}/{sid}: arr is required')
        if not signals_only(manifest) and not a.get('health'):
            raise ManifestError(f'{where}/{sid}: health curve is required unless "kpis": "{KPIS_NONE}"')
        if a.get('health'):
            validate_health(a['health'], f'{where}/{sid}')
        comms = a.get('communications')
        if comms is None:
            raise ManifestError(f'{where}/{sid}: v2 accounts declare "communications" (an empty list is allowed)')
        if 'signals' in a:
            raise ManifestError(f'{where}/{sid}: v2 accounts must not carry typed "signals" — say it in a communication')
        last_day = None
        texts = set()
        for i, c in enumerate(comms):
            tag = f'{where}/{sid}/communications[{i}]'
            if not isinstance(c.get('day'), int):
                raise ManifestError(f'{tag}: day must be an integer (relative to t0)')
            if last_day is not None and c['day'] < last_day:
                raise ManifestError(f'{tag}: days must be sorted ascending (got {c["day"]} after {last_day})')
            last_day = c['day']
            st = (c.get('source_type') or '').strip().lower()
            if st not in SOURCE_TYPES:
                raise ManifestError(f'{tag}: source_type {c.get("source_type")!r} not one of {SOURCE_TYPES}')
            text = (c.get('text') or '').strip()
            if not text:
                raise ManifestError(f'{tag}: text is required')
            if _sentence_count(text) > 6:
                raise ManifestError(f'{tag}: text should be 1-6 sentences ({_sentence_count(text)} found)')
            norm = normalize_text(text)
            if norm in texts:
                raise ManifestError(f'{tag}: duplicate text on this account — the engine would dedup it silently')
            texts.add(norm)
            people = c.get('participants') or []
            if not people or not all(isinstance(p, dict) and p.get('name') and p.get('title') for p in people):
                raise ManifestError(f'{tag}: participants must be a non-empty list of {{name, title}}')
            exp = c.get('expected_subtypes')
            if not isinstance(exp, list):
                raise ManifestError(f'{tag}: expected_subtypes must be a list (empty = "carries no signal")')
            for s in exp:
                if s not in known:
                    raise ManifestError(f'{tag}: expected subtype {s!r} is not in the {vertical} taxonomy')
            if 'expected_sentiment' in c and not -1.0 <= float(c['expected_sentiment']) <= 1.0:
                raise ManifestError(f'{tag}: expected_sentiment must be within [-1, 1]')
        for j, e in enumerate(a.get('events', [])):
            tag = f'{where}/{sid}/events[{j}]'
            if 'linked_signal_index' in e:
                raise ManifestError(f'{tag}: v2 events link with linked_communication_index, not linked_signal_index')
            li = e.get('linked_communication_index')
            if li is not None and not (0 <= int(li) < len(comms)):
                raise ManifestError(f'{tag}: linked_communication_index {li} out of range (0..{len(comms) - 1})')
            for k in ('day', 'type', 'amount'):
                if k not in e:
                    raise ManifestError(f'{tag}: {k} is required')
    validate_story(manifest, seen_ids)


# ═══════════════════════════════════════════════════════════════════════
# The driven story — tranches and the intervention cycles between them
# ═══════════════════════════════════════════════════════════════════════

def validate_story(manifest: dict, account_ids: set) -> None:
    """`tranches` and `interventions`: the manifest's OWN account ids and the
    vertical's OWN playbooks, checked at load so the driver
    (scripts/drive_tenant_story.py) never discovers a typo half way through a
    tenant it has already created."""
    where = manifest.get('manifest_id', '<manifest>')
    tranches = manifest.get('tranches') or []
    if not tranches:
        if manifest.get('interventions'):
            raise ManifestError(f'{where}: "interventions" needs "tranches" — an intervention fires on a tranche')
        return
    ids, last = set(), None
    for i, t in enumerate(tranches):
        tag = f'{where}/tranches[{i}]'
        tid = t.get('id')
        if not tid:
            raise ManifestError(f'{tag}: id is required')
        if tid in ids:
            raise ManifestError(f'{tag}: duplicate tranche id {tid!r}')
        ids.add(tid)
        if not isinstance(t.get('through_day'), int):
            raise ManifestError(f'{tag}: through_day must be an integer (relative to t0)')
        if last is not None and t['through_day'] <= last:
            raise ManifestError(f'{tag}: through_day {t["through_day"]} must be after the previous tranche ({last})')
        last = t['through_day']

    from playbooks.definitions import load_vertical
    known_pb = {p['id'] for p in load_vertical(manifest['vertical'])['playbooks']}
    from utils.taxonomy_loader import get_taxonomy
    taxonomy = get_taxonomy(manifest['vertical'])
    for i, iv in enumerate(manifest.get('interventions') or []):
        tag = f'{where}/interventions[{i}]'
        if iv.get('tranche') not in ids:
            raise ManifestError(f'{tag}: tranche {iv.get("tranche")!r} is not one of {sorted(ids)}')
        if iv.get('source_account_id') not in account_ids:
            raise ManifestError(f'{tag}: source_account_id {iv.get("source_account_id")!r} is not an account in this manifest')
        if iv.get('playbook_id') not in known_pb:
            raise ManifestError(f'{tag}: playbook_id {iv.get("playbook_id")!r} is not a {manifest["vertical"]} playbook '
                                f'(known: {sorted(known_pb)})')
        rep = iv.get('report') or {}
        if rep.get('state') not in ('done', 'failed', 'cancelled'):
            raise ManifestError(f'{tag}: report.state must be done | failed | cancelled (got {rep.get("state")!r})')
        ot = rep.get('outcome_type')
        if ot and not taxonomy.revenue_bucket(ot):
            raise ManifestError(f'{tag}: report.outcome_type {ot!r} is not in the {manifest["vertical"]} revenue buckets')
        if 'outcome_day' in rep and not isinstance(rep['outcome_day'], int):
            raise ManifestError(f'{tag}: report.outcome_day must be an integer (relative to t0)')


# ═══════════════════════════════════════════════════════════════════════
# Background accounts (generated communications)
# ═══════════════════════════════════════════════════════════════════════

# Plain, vertical-neutral wording built from base-taxonomy subtypes every
# vertical carries. Background accounts exist to make the portfolio look
# real, not to tell a story; their labels are still scored.
_BG_ROUTINE = (
    "Routine quarterly review with {who} ({title}) at {name}: walked through the dashboards and the open requests, "
    "nothing flagged on either side."
)
_BG_POSITIVE = {
    'advocacy': "{who} ({title}, {name}) said the team is happy with where things are and offered to speak to a peer "
                "who is evaluating the platform.",
    'executive_engagement': "{who} ({title}) joined the {name} review for the first time and asked to be kept on the "
                            "monthly update going forward.",
    'health_improvement': "{who} ({title}) noted that the {name} team's adoption numbers have picked up since the "
                          "last training session and the earlier friction is gone.",
}
_BG_PEOPLE = (('Platform Lead', 'Director'), ('VP Engineering', 'VP'))


def background_accounts(manifest: dict) -> List[dict]:
    """Background accounts for a v2 manifest: flat/ramp health, two generated
    communications each (a routine review and one positive note)."""
    bg = manifest.get('background') or {}
    rng = random.Random(manifest.get('seed', 1) + 7)
    out = []
    for i, name in enumerate(bg.get('names', [])):
        start = rng.uniform(*bg.get('health_range', [74, 88]))
        d1, d2 = -rng.randint(20, 150), -rng.randint(10, 120)
        pos_sub = rng.choice(sorted(_BG_POSITIVE))
        first = f'{name.split()[0]} Contact'
        lead_name, exec_name = f'{first} Lead', f'{first} Exec'
        comms = sorted([
            {'day': d1, 'source_type': 'meeting', 'participants': [{'name': lead_name, 'title': _BG_PEOPLE[0][0]}],
             'text': _BG_ROUTINE.format(who=lead_name, title=_BG_PEOPLE[0][0], name=name),
             'expected_subtypes': ['routine_review'], 'expected_sentiment': 0.2},
            {'day': d2, 'source_type': rng.choice(['email', 'meeting']),
             'participants': [{'name': exec_name, 'title': _BG_PEOPLE[1][0]}],
             'text': _BG_POSITIVE[pos_sub].format(who=exec_name, title=_BG_PEOPLE[1][0], name=name),
             'expected_subtypes': [pos_sub], 'expected_sentiment': 0.5},
        ], key=lambda c: c['day'])
        out.append({
            'source_account_id': f'BG-{i + 1}', 'name': name,
            'arr': rng.choice(bg.get('arr_choices', [900000, 1400000, 2100000, 3300000])),
            'industry': rng.choice(bg.get('industries', ['Technology'])),
            'region': rng.choice(bg.get('regions', ['North America'])),
            'role': 'background',
            'health': {'shape': rng.choice(['flat', 'ramp']), 'start': round(start, 1),
                       'end': round(start + rng.uniform(-3, 4), 1), 'noise': 1.2},
            'renewal_day': rng.randint(120, 330),
            'communications': comms, 'events': [],
        })
    return out


# ═══════════════════════════════════════════════════════════════════════
# Planning — the flat list the generator submits
# ═══════════════════════════════════════════════════════════════════════

def plan_communications(manifest: dict, accounts: List[dict]) -> List[dict]:
    """Every communication of every (featured + background) account, with
    its deterministic ref and absolute timestamp, in submission order."""
    t0 = datetime.fromisoformat(manifest['timeline']['t0'])
    out = []
    for a in accounts:
        sid = a.get('source_account_id') or a['name'].upper().replace(' ', '-')[:12]
        for i, c in enumerate(a.get('communications', [])):
            out.append({
                'ref': comm_ref(sid, i), 'source_account_id': sid, 'account_name': a['name'], 'index': i,
                'day': int(c['day']), 'occurred_at': occurred_at(t0, c['day']),
                'source_type': c['source_type'].strip().lower(), 'text': c['text'].strip(),
                'participants': [{'name': p['name'], 'role': p.get('title')} for p in c.get('participants', [])],
                'source_ref': c.get('source_ref') or f'demo:{comm_ref(sid, i)}',
                'expected_subtypes': list(c.get('expected_subtypes') or []),
                'expected_sentiment': c.get('expected_sentiment'),
            })
    return out
