"""
Inbound source adapters → the communications lane (docs/design/adapters.md §2.4).

    SOURCES                                   registry: source name → module with SOURCE, DESCRIPTION, COLUMNS, parse(content)
    import_from_source(customer_id, source, content, process_now=True, dry_run=False)

Every adapter is a declared transform (config/adapters.json → sources.<name>): it turns an export
into import_communications items — source_account_id / account_name, source_type, text, occurred_at,
participants, source_ref — and nothing else decides what the platform makes of them. The result is
import_communications' shape plus `parse` (rows, rejected rows, columns seen / mapped / unmapped) and
`already_imported` (rows whose source_ref is already on a signal of this tenant: skipped before
ingest, so a second import of the same file writes nothing).
"""
from __future__ import annotations

import logging
from types import ModuleType
from typing import Dict

from adapters import settings
from adapters.sources import gainsight_timeline

logger = logging.getLogger(__name__)

SOURCES: Dict[str, ModuleType] = {gainsight_timeline.SOURCE: gainsight_timeline}


def describe(source: str) -> dict:
    mod = SOURCES.get(source)
    if mod is None:
        raise ValueError(f'unknown source {source!r}; one of {sorted(SOURCES)}')
    return {'source': mod.SOURCE, 'description': mod.DESCRIPTION, 'columns': mod.COLUMNS}


def _existing_refs(customer_id: int, refs: list) -> dict:
    """source_ref → signal_id for refs this tenant already holds."""
    from models import QualitativeSignal
    if not refs:
        return {}
    rows = (QualitativeSignal.query.with_entities(QualitativeSignal.source_ref, QualitativeSignal.signal_id)
            .filter(QualitativeSignal.customer_id == int(customer_id), QualitativeSignal.source_ref.in_(refs)).all())
    return {r[0]: r[1] for r in rows}


def import_from_source(customer_id: int, source: str, content: str, process_now: bool = True, dry_run: bool = False) -> dict:
    """Parse → skip rows already imported (by source_ref) → import_communications in batches →
    process once at the end. dry_run parses and reports only. Raises ValueError on an unknown source,
    an oversized export, or a malformed one."""
    from signal_engine.pipeline import import_communications, process_pending
    from mcp_server import audit
    mod = SOURCES.get(source)
    if mod is None:
        raise ValueError(f'unknown source {source!r}; one of {sorted(SOURCES)}')
    content = content or ''
    max_bytes = int(settings.get('sources', 'max_content_bytes'))
    if len(content.encode('utf-8')) > max_bytes:
        raise ValueError(f'export larger than {max_bytes} bytes')
    parsed = mod.parse(content)
    max_rows = int(settings.get('sources', 'max_rows'))
    if parsed['rows'] > max_rows:
        raise ValueError(f'at most {max_rows} rows per import (got {parsed["rows"]})')
    items = parsed.pop('items')
    out = {'customer_id': int(customer_id), 'source': source, 'dry_run': bool(dry_run), 'parse': parsed,
           'received': len(items), 'already_imported': 0, 'queued': 0, 'duplicates': 0,
           'unknown_accounts': [], 'rejected': [], 'signal_ids': [], 'by_ref': {}}
    if dry_run:
        out['items_preview'] = items[: int(settings.get('sources', 'preview_rows'))]
        return out
    existing = _existing_refs(customer_id, [it['source_ref'] for it in items if it.get('source_ref')])
    fresh = []
    for it in items:
        ref = it.get('source_ref')
        if ref and ref in existing:
            out['already_imported'] += 1
            out['by_ref'][ref] = existing[ref]
        else:
            fresh.append(it)
    batch = int(settings.get('sources', 'import_batch'))
    for start in range(0, len(fresh), batch):
        chunk = fresh[start:start + batch]
        res = import_communications(int(customer_id), chunk, process_now=False)
        out['queued'] += res['queued']
        out['duplicates'] += res['duplicates']
        out['signal_ids'].extend(res['signal_ids'])
        out['by_ref'].update(res['by_ref'])
        for k in ('unknown_accounts', 'rejected'):
            for e in res[k]:
                out[k].append({**e, 'row': chunk[e['index']]['_row'], 'index': start + e['index']})
    if process_now and out['queued']:
        totals = {'processed': 0, 'nodes_written': 0, 'unclassified': 0, 'errors': 0, 'journeys_rebuilt': 0}
        while True:
            res = process_pending(customer_id=int(customer_id), limit=int(settings.get('sources', 'process_batch')), rebuild_journeys=True)
            for k in totals:
                totals[k] += res.get(k, 0)
            if not res['processed']:
                break
        out['processed'] = totals
    audit.record('adapter', f'import_from_source.{source}', customer_id, key_kind='n/a', outcome='allowed',
                 detail=f"rows={parsed['rows']} queued={out['queued']} already={out['already_imported']} dup={out['duplicates']} "
                        f"unknown={len(out['unknown_accounts'])} rejected={len(out['rejected']) + len(parsed['rejected'])}")
    logger.info('import_from_source %s customer=%s rows=%d queued=%d already=%d dup=%d unknown=%d rejected=%d', source, customer_id,
                parsed['rows'], out['queued'], out['already_imported'], out['duplicates'], len(out['unknown_accounts']), len(out['rejected']))
    return out
