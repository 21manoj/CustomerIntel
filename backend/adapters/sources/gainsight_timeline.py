"""
Gainsight Timeline export → communications (docs/design/adapters.md §2.4).

The export this reads is the Timeline activity report a Gainsight admin downloads as CSV
(Timeline → export, or a report over the Activity Timeline object). Assumed columns — the
names Gainsight uses, plus the aliases listed in config/adapters.json → sources.gainsight_timeline.columns,
matched case- and space-insensitively:

    Activity ID          the activity's GSID → source_ref 'gainsight:timeline:<id>' (idempotency)
    Activity Type        Update | Call | Meeting | Email | Milestone | … → source_type via activity_type_map
    Subject, Notes       the communication text (Notes is HTML in Gainsight exports; tags are stripped)
    Activity Date        when it happened (ISO-8601 or one of date_formats; no zone = UTC)
    Company ID / Name    the account: external id first (Account.external_account_id), then the name
    Author               who wrote it → the accountability participant
    Internal / External Attendees   ';' or ',' separated names → participants

Every other column folds into `attributes` (kept on the signal, never scored, never sent to the
model). Gainsight's scorecard / health / NPS columns are deliberately not read here: the conference
notes keep the incumbent's score as the backtest comparator, never as ours. No Gainsight field is a
taxonomy subtype, so every row goes through extraction (free text), the same as an email.
"""
from __future__ import annotations

import csv
import html
import io
import re
from datetime import datetime, timezone
from typing import Optional

from adapters import settings

SOURCE = 'gainsight_timeline'
DESCRIPTION = 'Gainsight Timeline activity export (CSV): one row per activity → one communication'


def _cfg() -> dict:
    return settings.get('sources', SOURCE)


COLUMNS = {field: list(aliases) for field, aliases in settings.get('sources', SOURCE, 'columns').items()}

_TAG = re.compile(r'<[^>]+>')
_WS = re.compile(r'[ \t\r\f\v]+')
_NL = re.compile(r'\n{3,}')


def _norm(header: str) -> str:
    return re.sub(r'\s+', ' ', (header or '').strip().lower())


def _map_headers(headers: list) -> tuple:
    """(our field → their header, unmapped headers)."""
    by_norm = {_norm(h): h for h in headers if h is not None}
    mapped = {}
    for field, aliases in COLUMNS.items():
        for alias in aliases:
            if _norm(alias) in by_norm:
                mapped[field] = by_norm[_norm(alias)]
                break
    used = set(mapped.values())
    return mapped, [h for h in headers if h is not None and h not in used]


def clean_text(value: Optional[str]) -> str:
    """Strip HTML (Gainsight notes are rich text), unescape entities, collapse whitespace."""
    s = html.unescape(_TAG.sub(' ', (value or '').replace('<br>', '\n').replace('<br/>', '\n').replace('</p>', '\n')))
    s = _WS.sub(' ', s)
    s = '\n'.join(line.strip() for line in s.splitlines())
    return _NL.sub('\n\n', s).strip()


def parse_date(value: Optional[str]) -> Optional[str]:
    """ISO-8601 first, then the configured formats. Naive values are UTC. None when unreadable."""
    s = (value or '').strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        dt = None
        for fmt in _cfg()['date_formats']:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'


def _people(row: dict, mapped: dict) -> list:
    cfg = _cfg()
    out, seen = [], set()

    def add(name: str, title: str):
        name = (name or '').strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({'name': name, 'title': title})

    add(row.get(mapped.get('author', ''), ''), cfg['author_title'])
    for field in ('internal_attendees', 'external_attendees'):
        raw = row.get(mapped.get(field, ''), '') or ''
        for sep in cfg['attendee_separators']:
            raw = raw.replace(sep, '\n')
        for name in raw.splitlines():
            add(name, cfg['attendee_title'])
    return out


def parse(content: str) -> dict:
    """{'rows', 'items', 'rejected': [{'row', 'reason'}], 'columns': {'seen', 'mapped', 'unmapped', 'missing'}}.
    Row numbers are 1-based data rows (the header is row 0)."""
    cfg = _cfg()
    reader = csv.DictReader(io.StringIO(content or ''))
    headers = list(reader.fieldnames or [])
    mapped, unmapped = _map_headers(headers)
    missing = [f for f in ('activity_date',) if f not in mapped]
    if 'company_id' not in mapped and 'company_name' not in mapped:
        missing.append('company_id|company_name')
    if 'subject' not in mapped and 'notes' not in mapped:
        missing.append('subject|notes')
    out = {'rows': 0, 'items': [], 'rejected': [],
           'columns': {'seen': headers, 'mapped': mapped, 'unmapped': unmapped, 'missing': missing}}
    if missing:
        out['rejected'].append({'row': 0, 'reason': f'export is missing required columns: {missing} (seen: {headers})'})
        return out
    type_map = {k.lower(): v for k, v in cfg['activity_type_map'].items()}
    n_chars = int(cfg['text_chars'])
    first_row_of: dict = {}                # Activity ID → row: a repeated id inside one export is reported, not imported twice
    for n, row in enumerate(reader, start=1):
        out['rows'] += 1
        get = lambda f: (row.get(mapped[f]) or '').strip() if f in mapped else ''
        subject, notes = clean_text(get('subject')), clean_text(get('notes'))
        text = (f'{subject}\n\n{notes}' if subject and notes else subject or notes).strip()
        when = parse_date(get('activity_date'))
        ext, name = get('company_id'), get('company_name')
        if not text:
            out['rejected'].append({'row': n, 'reason': 'no subject and no notes'})
            continue
        if not when:
            out['rejected'].append({'row': n, 'reason': f'unreadable activity date {get("activity_date")!r}'})
            continue
        if not ext and not name:
            out['rejected'].append({'row': n, 'reason': 'no company id and no company name'})
            continue
        aid = get('activity_id')
        if aid and aid in first_row_of:
            out['rejected'].append({'row': n, 'reason': f'duplicate Activity ID {aid!r} (first at row {first_row_of[aid]})'})
            continue
        if aid:
            first_row_of[aid] = n
        atype = get('activity_type')
        attributes = {'gainsight': {'activity_type': atype or None, 'activity_id': aid or None}}
        for h in unmapped:
            v = (row.get(h) or '').strip()
            if v:
                attributes[h] = v
        item = {
            'source_account_id': ext or None, 'account_name': name or None,
            'source_type': type_map.get(atype.lower(), cfg['default_source_type']) if atype else cfg['default_source_type'],
            'text': text[:n_chars], 'occurred_at': when, 'participants': _people(row, mapped),
            'source_ref': f"{cfg['source_ref_prefix']}{aid}" if aid else None, 'attributes': attributes, '_row': n,
        }
        out['items'].append(item)
    return out
