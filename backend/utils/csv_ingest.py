"""
CSV ingest — reads staged CSVs (models.CsvUploadStaging) into the ORM.
Companion to utils/csv_upload.py (which stages them).

Ported 2026-09-01 (Tier 2A-3) from the CSV-loading half of the old repo's
_process_data_impl (mcp_server/cs_pulse_onboarding.py, ~1000 inline lines).
Rewritten rather than copied — the logic is the same per file type, the
implementation is not:

- stdlib csv, not pandas. The old loader ran on pandas, whose blank cells
  come back as float NaN — truthy, and str()-ing to the literal 'nan'. That
  single property was the root of a whole bug class there (stakeholder
  names showing "nan" live on 3 tenants; ~40 `str(x) != 'nan'` guards
  scattered through the loader; a `_clean_csv_str` helper written to paper
  over it). csv.DictReader returns '' for a blank cell, so the class can't
  recur. One `cell()` helper replaces every guard.
- Reads from the staging table, not a per-customer disk directory. Staged
  rows are CONSUMED on a fully successful load (deleted), which is also
  what replaces the old repo's "is any CSV's mtime newer than the last KPI
  insert" incremental detection (itself the site of a UTC-offset bug). If
  any phase records an error the staged rows are kept so a retry can
  re-run — every loader below is idempotent, so a retry never duplicates
  what the first attempt already committed.
- Every loader is idempotent on its own, and the old `_skip_cg_reload`
  gate (skip *all* context-graph files if any CG node exists) is gone. That
  gate was the only thing preventing duplicate STAKEHOLDER/engagement/
  benchmark nodes on re-load — and it did so by silently ignoring
  legitimately re-uploaded files. Per-loader dedup keys are the honest fix.

Bugs found in the old loader and fixed here rather than carried forward:
- signal_edges.csv's "clear existing csv_import edges, re-insert" step
  deleted EVERY csv_import edge for the customer — including the
  linked_signal_id LED_TO edges outcomes.csv had written moments earlier in
  the same run (item 37a). Never observed live only because standard
  registration doesn't upload signal_edges.csv. Now scoped by created_by.
- industry_benchmarks.csv's loader read columns named industry_p25/p50/p75
  and benchmark_source; the schema (config/csv_schemas.json) defines
  p25/p50/p75/p90 and source. Every benchmark node was written with empty
  percentile strings. Reads the schema's names now (old names as fallback).
- account_business_profiles.csv REPLACED Account.profile_metadata, wiping
  the products/champion/contract fields account_details.csv had just
  populated. Now merges.
- The CSV→DB account-id map did int(csv_id), crashing on any non-numeric
  source_account_id (e.g. 'ACC-1' — the shape upload_csv's own tests use).
  Keyed by string now.
- kpi_measurements.csv wrote target=100 when the column was absent. No
  scorer reads KPIMeasurement.target (grep-verified in the old repo's
  process_data_pipeline/generic_scorer/account_health/vertical_health), so
  that was a fabricated number with no reader. NULL when absent.
- qualitative signals defaulted sentiment_score to 0.5 when absent — on a
  -1..1 scale. NULL when absent.

Not carried forward:
- The Product table write (products extracted from account_details.csv's
  products JSON). Product has no reader anywhere in this build; the same
  data stays in Account.profile_metadata['products'], which is what the
  adoption back-fill actually reads. Port the table when an API that
  reads it is scoped.
- The `enhanced_qualitative_signals.csv` second pass. The old loader
  matched `'qualitative_signals' in filename` — which the enhanced file
  also matches — so it always went through the first (QualitativeSignal +
  SIGNAL-node) path and the dedicated "enhanced" branch below it was
  unreachable for the canonical file. One path here.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ACCOUNT_FILES = ('account_details.csv', 'accounts.csv')  # preference order

SIGNAL_EDGES_CREATED_BY = 'process_data.signal_edges'
LINKED_SIGNAL_CREATED_BY = 'process_data.linked_signal_id'

PROFILE_KEYS = (
    'contract_start', 'contract_end', 'renewal_date',
    'csm_name', 'csm_email', 'csm_manager',
    'executive_sponsor', 'tier',
    'primary_champion_name', 'primary_champion_title',
    'primary_champion_email', 'primary_champion_engagement_score',
    'employee_count', 'tech_stack', 'cloud_provider',
    'deployment_type', 'competitive_landscape',
    'strategic_initiatives', 'budget_cycle', 'fiscal_year_end',
)

# (name column, STAKEHOLDER node_subtype, title column, email column)
STAKEHOLDER_FIELDS = (
    ('primary_champion_name', 'champion', 'primary_champion_title', 'primary_champion_email'),
    ('executive_sponsor', 'executive_sponsor', None, None),
    ('csm_name', 'csm', None, 'csm_email'),
    ('csm_manager', 'cs_manager', None, None),
)

BUSINESS_PROFILE_KEYS = (
    'arr', 'employee_count', 'industry', 'fiscal_year_end',
    'tech_stack', 'cloud_provider', 'competitive_landscape',
    'strategic_initiatives', 'budget_cycle', 'assigned_csm',
    'csm_manager', 'executive_sponsor', 'mrr',
    'primary_champion_name', 'primary_champion_title',
    'primary_champion_email', 'primary_champion_engagement_score',
)


# ═══════════════════════════════════════════════════════════════════════
# Cell helpers
# ═══════════════════════════════════════════════════════════════════════

_EMPTY_TOKENS = frozenset(('', 'nan', 'none', 'null'))


def parse_rows(csv_content: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(csv_content)))


def cell(row: dict, *keys: str, default: str = '') -> str:
    """First non-blank value among `keys`, stripped. Blank/NaN-ish → default."""
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if v.lower() not in _EMPTY_TOKENS:
            return v
    return default


def num(row: dict, *keys: str, default=None):
    v = cell(row, *keys)
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def when(row: dict, *keys: str, default=None) -> Optional[datetime]:
    v = cell(row, *keys)
    if not v:
        return default
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        pass
    for fmt in ('%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return default


def coerce(v: str):
    """Match what pandas would have typed a clean numeric column as."""
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


# ═══════════════════════════════════════════════════════════════════════
# Account resolution
# ═══════════════════════════════════════════════════════════════════════

class AccountResolver:
    """Maps a CSV row to a DB account_id: by account_name first, then by
    source_account_id.

    The source id map comes from Account.external_account_id (persisted by
    load_accounts), not only from the accounts file in the current batch.
    The old repo rebuilt this map by re-reading accounts.csv from disk on
    every run; here staged files are consumed after a successful load, so
    an incremental kpi_measurements.csv-only upload has no accounts file
    to read — the DB row is the durable place for the mapping.
    """

    def __init__(self, customer_id: int, account_rows: list[dict]):
        from models import Account
        accounts = Account.query.filter_by(customer_id=customer_id).all()
        self.by_name = {a.account_name: a.account_id for a in accounts}
        self.db_ids = {a.account_id for a in accounts}
        self.csv_to_db: dict[str, int] = {
            a.external_account_id: a.account_id for a in accounts if a.external_account_id
        }
        for r in account_rows:
            csv_id = cell(r, 'source_account_id', 'account_id')
            name = cell(r, 'account_name', 'name')
            if csv_id and name in self.by_name:
                self.csv_to_db.setdefault(csv_id, self.by_name[name])
        if not self.csv_to_db:
            # Accounts created before external_account_id was persisted:
            # load-driver convention, source id = customer_id*1000+ordinal.
            for i, a in enumerate(sorted(accounts, key=lambda a: a.account_id), 1):
                self.csv_to_db[str(customer_id * 1000 + i)] = a.account_id

    def __call__(self, row: dict) -> Optional[int]:
        name = cell(row, 'account_name', 'account')
        if name and name in self.by_name:
            return self.by_name[name]
        raw = cell(row, 'source_account_id', 'account_id')
        if not raw:
            return None
        mapped = self.csv_to_db.get(raw)
        if mapped is not None:
            return mapped
        try:
            if int(raw) in self.db_ids:
                return int(raw)
        except ValueError:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════
# Per-file loaders — each idempotent, each returns a count
# ═══════════════════════════════════════════════════════════════════════

def load_accounts(customer_id: int, vertical: str, rows: list[dict]) -> tuple[int, int]:
    """Create Accounts (or fill missing profile_metadata on existing ones).
    Returns (created, updated)."""
    from models import Account
    from extensions import db

    created = updated = 0
    for row in rows:
        name = cell(row, 'account_name', 'name')
        if not name:
            continue
        profile = {}
        for key in PROFILE_KEYS:
            v = cell(row, key)
            if v:
                profile[key] = coerce(v)
        products_raw = cell(row, 'products')
        if products_raw:
            try:
                prods = json.loads(products_raw)
                if isinstance(prods, list):
                    profile['products'] = prods
            except (ValueError, TypeError):
                pass

        source_id = cell(row, 'source_account_id', 'account_id') or None
        existing = Account.query.filter_by(customer_id=customer_id, account_name=name).first()
        if existing:
            merged = dict(existing.profile_metadata or {})
            changed = False
            for k, v in profile.items():
                if k not in merged:
                    merged[k] = v
                    changed = True
            if changed:
                existing.profile_metadata = merged
            if source_id and not existing.external_account_id:
                existing.external_account_id = source_id
                changed = True
            if changed:
                updated += 1
            continue

        db.session.add(Account(
            customer_id=customer_id,
            account_name=name,
            external_account_id=source_id,
            revenue=num(row, 'arr', 'annual_revenue', 'revenue', default=0),
            vertical=vertical,
            industry=cell(row, 'industry') or None,
            region=cell(row, 'region') or None,
            account_status=cell(row, 'account_status', default='active'),
            profile_metadata=profile or None,
        ))
        created += 1
    db.session.flush()
    return created, updated


def extract_stakeholders_from_profiles(customer_id: int) -> int:
    """STAKEHOLDER nodes from account_details.csv's champion/sponsor/csm fields."""
    from models import Account, ContextNode
    from extensions import db

    count = 0
    for acct in Account.query.filter_by(customer_id=customer_id).all():
        pm = acct.profile_metadata or {}
        for name_field, role, title_field, email_field in STAKEHOLDER_FIELDS:
            name_val = pm.get(name_field)
            if not name_val or str(name_val).strip().lower() in _EMPTY_TOKENS:
                continue
            if ContextNode.query.filter_by(
                customer_id=customer_id, account_id=acct.account_id,
                node_type='STAKEHOLDER', node_subtype=role,
            ).first():
                continue
            # Roles with a title column use it (blank → name only); roles
            # without one are labelled by role. Same as the old loader.
            title_val = (pm.get(title_field) or '') if title_field else role.replace('_', ' ').title()
            email_val = (pm.get(email_field) if email_field else None) or ''
            db.session.add(ContextNode(
                customer_id=customer_id, account_id=acct.account_id,
                node_type='STAKEHOLDER', node_subtype=role,
                source='observed',
                title=f'{name_val} ({title_val})' if title_val else str(name_val),
                properties={
                    'name': str(name_val), 'role': role,
                    'job_title': str(title_val), 'email': str(email_val),
                    'auto_created': True, 'source_field': name_field,
                },
                tier=1, occurred_at=datetime.utcnow(),
                source_platform='account_details_extraction',
            ))
            count += 1
    if count:
        db.session.flush()
    return count


def load_kpis(rows: list[dict], resolve: Callable) -> int:
    """Bulk INSERT ... ON CONFLICT DO NOTHING on (account_id, kpi_code, measured_at)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from models import KPIMeasurement
    from extensions import db

    seen = set()
    values = []
    now = datetime.utcnow()
    for row in rows:
        acct_id = resolve(row)
        code = cell(row, 'kpi_code', 'kpi_id')
        measured = when(row, 'measured_at', 'date')
        if not acct_id or not code or not measured:
            continue
        key = (acct_id, code, measured)
        if key in seen:
            continue
        seen.add(key)
        values.append({
            'account_id': acct_id,
            'kpi_code': code,
            'value': num(row, 'value', default=0.0),
            'target': num(row, 'target'),
            'pillar': cell(row, 'pillar') or None,
            'weight': num(row, 'weight'),
            'status': cell(row, 'status') or None,
            'measured_at': measured,
            'created_at': now,
        })
    if not values:
        return 0
    stmt = pg_insert(KPIMeasurement.__table__).values(values).on_conflict_do_nothing(
        index_elements=['account_id', 'kpi_code', 'measured_at'],
    )
    res = db.session.execute(stmt)
    return res.rowcount if res.rowcount is not None else len(values)


def load_signals(customer_id: int, rows: list[dict], resolve: Callable) -> tuple[int, int]:
    """The typed-signals CSV is the structured lane INTO the signal engine
    (signals-first, 2026-09-04): every row goes through pipeline.ingest with
    its declared subtype, its recorded sentiment, its own id and ref, and the
    system it came from. The engine writes the evidence node (materialize),
    so a CSV signal and an extracted one carry the same provenance, urgency
    and review state. A subtype the taxonomy does not know falls through to
    extraction from the row's text, like any free text would.

    Returns (signals_queued, duplicates_skipped). Nodes are written by
    materialize_pending_signals() right after, before phase 2 links outcomes
    to them.
    """
    from signal_engine.pipeline import ingest

    prefix = f'c{customer_id}_'
    queued = skipped = 0
    for row in rows:
        acct_id = resolve(row)
        if not acct_id:
            continue
        signal_dt = when(row, 'signal_date', 'date')
        if not signal_dt:
            continue
        raw_id = cell(row, 'signal_id') or f'sig_{uuid.uuid4().hex[:12]}'
        # Customer-scoped so two tenants loaded from the same manifest
        # template can't collide on the (customer_id, signal_id) unique key.
        sig_id = (raw_id if raw_id.startswith(prefix) else prefix + raw_id)[:50]
        sh_name = cell(row, 'stakeholder_name')
        sh_title = cell(row, 'stakeholder_title')
        content = cell(row, 'content', 'signal_text') or cell(row, 'signal_type')
        if not content:
            continue
        try:
            res = ingest(
                customer_id, acct_id, 'csv_import', content,
                occurred_at=signal_dt, signal_type=cell(row, 'signal_type') or None,
                participants=[{'name': sh_name, 'role': sh_title or 'contact'}] if sh_name else None,
                source_ref=cell(row, 'signal_ref') or raw_id, signal_id=sig_id,
                sentiment_score=cell(row, 'sentiment_score') or None,
                origin_platform=cell(row, 'source_platform') or None, consent_verified=True,
            )
        except ValueError as e:
            logger.warning('csv signal row skipped (%s): %s', sig_id, e)
            continue
        if res['status'] == 'queued':
            queued += 1
        else:
            skipped += 1
    return queued, skipped


def materialize_pending_signals(customer_id: int) -> dict:
    """Drain the engine for this customer without rebuilding journeys (the
    Wizard A stage follows). Returns the pipeline's totals."""
    from signal_engine.pipeline import process_pending
    totals = {'processed': 0, 'structured': 0, 'enriched': 0, 'unclassified': 0, 'nodes_written': 0, 'errors': 0}
    while True:
        res = process_pending(customer_id=customer_id, limit=200, rebuild_journeys=False)
        for k in totals:
            totals[k] += res.get(k, 0)
        if not res['processed']:
            break
    return totals


def load_stakeholders(customer_id: int, rows: list[dict], resolve: Callable) -> int:
    """STAKEHOLDER nodes from stakeholders.csv. Dedup on (account_id, name)."""
    from models import ContextNode
    from extensions import db

    existing = set()
    for n in ContextNode.query.filter_by(customer_id=customer_id, node_type='STAKEHOLDER').all():
        nm = (n.properties or {}).get('name') or n.title or ''
        existing.add((n.account_id, nm.strip().lower()))

    count = 0
    for row in rows:
        acct_id = resolve(row)
        name = cell(row, 'stakeholder_name', 'name')
        if not acct_id or not name:
            continue
        key = (acct_id, name.lower())
        if key in existing:
            continue
        existing.add(key)
        db.session.add(ContextNode(
            customer_id=customer_id, account_id=acct_id,
            node_type='STAKEHOLDER', node_subtype=cell(row, 'role', default='contact'),
            source='observed',
            title=name,
            properties={
                'name': name,
                'job_title': cell(row, 'title'),
                'email': cell(row, 'email'),
                'department': cell(row, 'department'),
                'influence_score': cell(row, 'influence_score'),
                'engagement_frequency': cell(row, 'engagement_frequency'),
                'sentiment': cell(row, 'sentiment'),
            },
            tier=1,
            occurred_at=when(row, 'first_observed_at', default=datetime.utcnow()),
            source_platform=cell(row, 'source_platform', default='csv_import'),
        ))
        count += 1
    if count:
        db.session.flush()
    return count


def load_outcomes(customer_id: int, rows: list[dict], resolve: Callable) -> tuple[int, int]:
    """OUTCOME nodes (I3'-clamped) + LED_TO edges from linked_signal_id (item 37a).
    Returns (nodes_added, edges_added).

    Constructs ContextNode directly rather than via upsert_node(): the
    source_event_id here is degenerate ('outcome:<type>', shared across
    same-type rows), so upsert_node's dedup on that key would silently
    overwrite distinct outcomes. Dedup is on (account, title, revenue,
    type) instead, and the I3' clamp upsert_node would have applied is
    applied directly.
    """
    from models import ContextNode, ContextEdge
    from extensions import db
    from utils.context_graph_invariants import clamp_unearned_confidence
    from utils.provenance import UNKNOWN as EVIDENCE_TIER_UNKNOWN
    from utils.edge_factory import CSV_IMPORT_DERIVATION

    def _key(acct_id, title, rev, otype):
        return (acct_id, title, ('%.2f' % float(rev)) if rev is not None else 'None', otype)

    existing = {
        _key(n.account_id, n.title, n.revenue_impact, n.revenue_impact_type)
        for n in ContextNode.query.filter_by(customer_id=customer_id, node_type='OUTCOME').all()
    }

    pending_edges: list[tuple] = []
    nodes_added = 0
    for row in rows:
        acct_id = resolve(row)
        if not acct_id:
            continue
        rev = num(row, 'revenue_value', 'revenue_impact')
        outcome_type = cell(row, 'outcome_type', default='revenue')
        title = cell(row, 'title', 'outcome_name')
        key = _key(acct_id, title, rev, outcome_type)
        if key in existing:
            continue
        existing.add(key)

        source_platform = cell(row, 'source_platform', default='csv_import')
        props = {'evidence': cell(row, 'evidence'), 'confidence': cell(row, 'confidence')}
        conf, props, tier, _clamped = clamp_unearned_confidence(
            node_type='OUTCOME',
            source_platform=source_platform,
            source_ref=None,
            confidence=1.0,
            properties=props,
            tier=1,
        )
        node = ContextNode(
            customer_id=customer_id, account_id=acct_id,
            node_type='OUTCOME', node_subtype=outcome_type,
            source='observed',
            title=title,
            revenue_impact=rev, revenue_impact_type=outcome_type,
            properties=props, tier=tier, confidence=conf,
            occurred_at=when(row, 'outcome_date', default=datetime.utcnow()),
            source_platform=source_platform,
            source_event_id=f'outcome:{outcome_type}',
        )
        db.session.add(node)
        nodes_added += 1
        linked = cell(row, 'linked_signal_id')
        if linked:
            pending_edges.append((node, acct_id, linked))
    db.session.flush()

    edges_added = 0
    if pending_edges:
        sig_lookup = {}
        for n in ContextNode.query.filter(
                ContextNode.customer_id == customer_id,
                ContextNode.node_type == 'SIGNAL',
                ContextNode.account_id.in_({a for _, a, _ in pending_edges})).all():
            for ref in (n.source_event_id, n.source_ref, (n.properties or {}).get('signal_ref')):
                if ref:
                    sig_lookup.setdefault((n.account_id, str(ref)), n.node_id)
        for node, acct_id, linked in pending_edges:
            from_id = sig_lookup.get((acct_id, linked))
            if not from_id or not node.node_id:
                continue
            db.session.add(ContextEdge(
                customer_id=customer_id,
                from_node_id=from_id, to_node_id=node.node_id,
                edge_type='LED_TO', weight=1.0, confidence=1.0,
                source_platform='csv_import', created_by=LINKED_SIGNAL_CREATED_BY,
                properties={
                    'evidence': f'linked_signal_id={linked}',
                    'evidence_tier': EVIDENCE_TIER_UNKNOWN,
                    'derivation': CSV_IMPORT_DERIVATION,
                },
            ))
            edges_added += 1
        if edges_added:
            db.session.flush()
    return nodes_added, edges_added


def load_decisions(customer_id: int, rows: list[dict], resolve: Callable) -> int:
    """DECISION nodes. Dedup on (account_id, source_event_id) when decision_id
    is present, else (account_id, title)."""
    from models import ContextNode
    from extensions import db

    existing = set()
    for n in ContextNode.query.filter_by(customer_id=customer_id, node_type='DECISION').all():
        existing.add((n.account_id, n.source_event_id or n.title))

    count = 0
    for row in rows:
        acct_id = resolve(row)
        if not acct_id:
            continue
        dec_id = cell(row, 'decision_id')
        title = cell(row, 'title', 'decision_name')
        # 'decision:<id>' prefix is what signal_edges.csv refs resolve against
        src_eid = f'decision:{dec_id}' if dec_id else None
        key = (acct_id, src_eid or title)
        if key in existing:
            continue
        existing.add(key)
        db.session.add(ContextNode(
            customer_id=customer_id, account_id=acct_id,
            node_type='DECISION', node_subtype=cell(row, 'decision_maker_role', default='action'),
            source='observed',
            title=title,
            properties={
                'chosen_option': cell(row, 'chosen_option'),
                'outcome_description': cell(row, 'outcome_description'),
                'risk_level': cell(row, 'risk_level'),
                'decision_id': dec_id,
            },
            tier=1, occurred_at=when(row, 'decision_date', default=datetime.utcnow()),
            source_platform=cell(row, 'source_platform', default='csv_import'),
            source_event_id=src_eid,
        ))
        count += 1
    if count:
        db.session.flush()
    return count


def load_engagement_events(customer_id: int, rows: list[dict], resolve: Callable) -> int:
    """SIGNAL/engagement nodes. Dedup on (account_id, title, occurred_at)."""
    from models import ContextNode
    from extensions import db

    existing = {
        (n.account_id, n.title, n.occurred_at)
        for n in ContextNode.query.filter_by(
            customer_id=customer_id, node_type='SIGNAL', node_subtype='engagement').all()
    }
    count = 0
    for row in rows:
        acct_id = resolve(row)
        if not acct_id:
            continue
        title = cell(row, 'title') or cell(row, 'description')
        if not title:
            title = cell(row, 'event_type', default='engagement').replace('_', ' ').title()
        title = title[:200]
        occurred = when(row, 'event_date', default=datetime.utcnow())
        key = (acct_id, title, occurred)
        if key in existing:
            continue
        existing.add(key)
        db.session.add(ContextNode(
            customer_id=customer_id, account_id=acct_id,
            node_type='SIGNAL', node_subtype='engagement',
            source='observed',
            title=title,
            properties={
                'event_type': cell(row, 'event_type'),
                'channel': cell(row, 'channel'),
                'duration_minutes': cell(row, 'duration_minutes'),
                'outcome': cell(row, 'outcome'),
                'participants': cell(row, 'participants'),
                'notes': cell(row, 'notes'),
                'stakeholder_name': cell(row, 'stakeholder_name'),
                'sentiment_shift': cell(row, 'sentiment_shift'),
            },
            tier=2, occurred_at=occurred,
            source_platform=cell(row, 'source_platform', default='csv_import'),
        ))
        count += 1
    if count:
        db.session.flush()
    return count


def load_business_profiles(customer_id: int, rows: list[dict], resolve: Callable) -> int:
    """Merge account_business_profiles.csv into Account.profile_metadata."""
    from models import Account
    from extensions import db

    accounts = {a.account_id: a for a in Account.query.filter_by(customer_id=customer_id).all()}
    count = 0
    for row in rows:
        acct_id = resolve(row)
        acct = accounts.get(acct_id)
        if not acct:
            continue
        profile = dict(acct.profile_metadata or {})
        for key in BUSINESS_PROFILE_KEYS:
            v = cell(row, key)
            if v:
                profile[key] = coerce(v)
        acct.profile_metadata = profile
        arr = num(row, 'arr')
        if arr is not None:
            acct.revenue = arr
        count += 1
    if count:
        db.session.flush()
    return count


def load_benchmarks(customer_id: int, rows: list[dict]) -> int:
    """EXTERNAL_CONTEXT/industry_benchmark nodes. The node model requires an
    account_id, so these attach to the customer's first account (as the old
    loader did). Dedup on source_event_id."""
    from models import Account, ContextNode
    from extensions import db

    first = Account.query.filter_by(customer_id=customer_id).order_by(Account.account_id).first()
    if not first:
        return 0
    existing = {
        r[0] for r in ContextNode.query.filter_by(
            customer_id=customer_id, node_type='EXTERNAL_CONTEXT', node_subtype='industry_benchmark',
        ).with_entities(ContextNode.source_event_id).all()
    }
    count = 0
    for row in rows:
        kpi_code = cell(row, 'kpi_code')
        if not kpi_code:
            continue
        src = f'bench_{kpi_code}'
        if src in existing:
            continue
        existing.add(src)
        db.session.add(ContextNode(
            customer_id=customer_id, account_id=first.account_id,
            node_type='EXTERNAL_CONTEXT', node_subtype='industry_benchmark',
            source='observed',
            title=f'Benchmark: {kpi_code}',
            properties={
                'kpi_code': kpi_code,
                'kpi_name': cell(row, 'kpi_name'),
                'pillar': cell(row, 'pillar'),
                'unit': cell(row, 'unit'),
                'benchmark_source': cell(row, 'source', 'benchmark_source'),
                'p25': cell(row, 'p25', 'industry_p25'),
                'p50': cell(row, 'p50', 'industry_p50'),
                'p75': cell(row, 'p75', 'industry_p75'),
                'p90': cell(row, 'p90', 'industry_p90'),
                'methodology': cell(row, 'methodology'),
            },
            tier=1, occurred_at=datetime.utcnow(),
            source_platform='csv_import',
            source_event_id=src,
        ))
        count += 1
    if count:
        db.session.flush()
    return count


def load_signal_edges(customer_id: int, rows: list[dict], resolve: Callable) -> int:
    """ContextEdges from signal_edges.csv. Must run after every node loader.
    Idempotent by clearing this loader's own prior edges (scoped by
    created_by — NOT every csv_import edge, see module docstring)."""
    from models import ContextNode, ContextEdge
    from extensions import db
    from utils.provenance import UNKNOWN as EVIDENCE_TIER_UNKNOWN
    from utils.edge_factory import CSV_IMPORT_DERIVATION

    ContextEdge.query.filter_by(
        customer_id=customer_id, created_by=SIGNAL_EDGES_CREATED_BY,
    ).delete(synchronize_session='fetch')
    db.session.flush()

    title_to_node, sigref_to_node, srcid_to_node, acct_srcid_to_node = {}, {}, {}, {}
    for n in ContextNode.query.filter_by(customer_id=customer_id).all():
        if n.title:
            t = n.title.strip()
            title_to_node[t] = n.node_id
            title_to_node[t[:60]] = n.node_id
        if n.source_event_id:
            srcid_to_node[n.source_event_id] = n.node_id
            acct_srcid_to_node[(n.account_id, n.source_event_id)] = n.node_id
        if n.source_ref:
            srcid_to_node.setdefault(n.source_ref, n.node_id)
            acct_srcid_to_node.setdefault((n.account_id, n.source_ref), n.node_id)
        sr = (n.properties or {}).get('signal_ref') if isinstance(n.properties, dict) else None
        if sr:
            sigref_to_node[str(sr)] = n.node_id
            acct_srcid_to_node[(n.account_id, str(sr))] = n.node_id

    def _resolve_ref(ref: str, account_id=None) -> Optional[int]:
        if not ref:
            return None
        if account_id:
            nid = acct_srcid_to_node.get((account_id, ref))
            if nid:
                return nid
        nid = sigref_to_node.get(ref) or srcid_to_node.get(ref)
        if nid:
            return nid
        # CSV refs without prefix → DB source_event_ids with prefix
        for prefix in ('decision:', 'outcome:', 'signal:'):
            prefixed = prefix + ref
            if account_id:
                nid = acct_srcid_to_node.get((account_id, prefixed))
                if nid:
                    return nid
            nid = srcid_to_node.get(prefixed)
            if nid:
                return nid
        # "phase_ref — title" composite refs
        phase_ref = title_part = None
        for sep in (' — ', ' – ', ' - '):
            if sep in ref:
                phase_ref, title_part = (s.strip() for s in ref.split(sep, 1))
                break
        if phase_ref:
            if account_id:
                nid = acct_srcid_to_node.get((account_id, phase_ref))
                if nid:
                    return nid
            nid = sigref_to_node.get(phase_ref) or srcid_to_node.get(phase_ref)
            if nid:
                return nid
        if title_part:
            for t in (title_part, title_part[:200], title_part[:60]):
                nid = title_to_node.get(t)
                if nid:
                    return nid
        return title_to_node.get(ref)

    count = 0
    for row in rows:
        edge_acct = resolve(row)
        from_id = _resolve_ref(cell(row, 'from_signal_ref'), edge_acct)
        to_id = _resolve_ref(cell(row, 'to_signal_ref'), edge_acct)
        if not from_id or not to_id or from_id == to_id:
            continue
        props = {
            'evidence': cell(row, 'evidence'),
            'evidence_tier': EVIDENCE_TIER_UNKNOWN,
            'derivation': CSV_IMPORT_DERIVATION,
        }
        csv_created_by = cell(row, 'created_by')
        if csv_created_by:
            props['csv_created_by'] = csv_created_by
        edge = ContextEdge(
            customer_id=customer_id,
            from_node_id=from_id, to_node_id=to_id,
            edge_type=cell(row, 'edge_type', default='LED_TO'),
            weight=num(row, 'weight', default=1.0),
            confidence=num(row, 'confidence', default=1.0),
            source_platform=cell(row, 'source_platform', default='csv_import'),
            created_by=SIGNAL_EDGES_CREATED_BY,
            properties=props,
        )
        rev = num(row, 'revenue_impact')
        if rev is not None:
            edge.revenue_impact = rev
        lag = num(row, 'lag_days')
        if lag is not None:
            edge.lag_days = int(lag)
        db.session.add(edge)
        count += 1
    db.session.flush()
    return count


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class IngestResult:
    files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    consumed: bool = False


def staged_files(customer_id: int) -> dict[str, str]:
    """{canonical file_type: csv_content} for everything staged for this customer."""
    from models import CsvUploadStaging
    return {
        s.file_type: s.csv_content
        for s in CsvUploadStaging.query.filter_by(customer_id=customer_id).all()
    }


def ingest_staged_csvs(customer_id: int, vertical: str) -> IngestResult:
    """Load every staged CSV for a customer into the ORM.

    Phase 1 (regular model): accounts → stakeholder extraction → KPIs
    (committed on their own so a later failure can't roll them back) →
    qualitative signals.
    Phase 2 (context graph): stakeholders, outcomes, decisions, engagement
    events, business profiles, benchmarks, then signal edges last (needs
    every node to exist).
    Each phase is its own try/except: an error is recorded, the session is
    rolled back to the last commit, and the next phase still runs. Staged
    rows are deleted only when both phases finished without error.
    """
    import time
    from models import CsvUploadStaging
    from extensions import db

    result = IngestResult()
    files = staged_files(customer_id)
    result.files = sorted(files)
    if not files:
        return result

    rows = {ft: parse_rows(content) for ft, content in files.items()}
    account_file = next((f for f in ACCOUNT_FILES if f in rows), None)
    account_rows = rows.get(account_file, []) if account_file else []

    # ── Phase 1 ──
    t0 = time.time()
    try:
        if account_rows:
            created, updated = load_accounts(customer_id, vertical, account_rows)
            result.steps.append(f'accounts_loaded_{created}_created_{updated}_updated')
            n = extract_stakeholders_from_profiles(customer_id)
            if n:
                result.steps.append(f'stakeholders_extracted_from_account_details({n})')
            db.session.commit()

        resolve = AccountResolver(customer_id, account_rows)

        if 'kpi_measurements.csv' in rows:
            n = load_kpis(rows['kpi_measurements.csv'], resolve)
            result.steps.append(f'kpis_loaded_{n}')
            db.session.commit()

        if 'enhanced_qualitative_signals.csv' in rows:
            s, dup = load_signals(customer_id, rows['enhanced_qualitative_signals.csv'], resolve)
            result.steps.append(f'signals_queued_{s}_skipped_{dup}')
            db.session.commit()
            m = materialize_pending_signals(customer_id)
            result.steps.append(f"signals_materialized_{m['processed']}_nodes_{m['nodes_written']}"
                                f"_structured_{m['structured']}_extracted_{m['enriched']}_unclassified_{m['unclassified']}"
                                + (f"_errors_{m['errors']}" if m['errors'] else ''))
            db.session.commit()
    except Exception as e:
        logger.exception('csv ingest phase 1 failed for customer %s', customer_id)
        result.errors.append(f'csv_loading: {e}')
        db.session.rollback()
        resolve = AccountResolver(customer_id, account_rows)
    result.timings['csv_load'] = round(time.time() - t0, 2)

    # ── Phase 2 ──
    t0 = time.time()
    try:
        if 'stakeholders.csv' in rows:
            result.steps.append(f"stakeholders_loaded_{load_stakeholders(customer_id, rows['stakeholders.csv'], resolve)}")
        if 'outcomes.csv' in rows:
            n, e = load_outcomes(customer_id, rows['outcomes.csv'], resolve)
            result.steps.append(f'outcomes_loaded_{n}')
            result.steps.append(f'outcome_edges_loaded_{e}')
        if 'decisions.csv' in rows:
            result.steps.append(f"decisions_loaded_{load_decisions(customer_id, rows['decisions.csv'], resolve)}")
        if 'engagement_events.csv' in rows:
            result.steps.append(f"engagement_events_loaded_{load_engagement_events(customer_id, rows['engagement_events.csv'], resolve)}")
        if 'account_business_profiles.csv' in rows:
            result.steps.append(f"profiles_loaded_{load_business_profiles(customer_id, rows['account_business_profiles.csv'], resolve)}")
        if 'industry_benchmarks.csv' in rows:
            result.steps.append(f"benchmarks_loaded_{load_benchmarks(customer_id, rows['industry_benchmarks.csv'])}")
        db.session.commit()
        if 'signal_edges.csv' in rows:
            result.steps.append(f"edges_loaded_{load_signal_edges(customer_id, rows['signal_edges.csv'], resolve)}")
            db.session.commit()
        result.steps.append('context_graph_loaded')
    except Exception as e:
        logger.exception('csv ingest phase 2 failed for customer %s', customer_id)
        result.errors.append(f'context_graph: {e}')
        db.session.rollback()
    result.timings['cg_load'] = round(time.time() - t0, 2)

    if not result.errors:
        CsvUploadStaging.query.filter_by(customer_id=customer_id).delete(synchronize_session='fetch')
        db.session.commit()
        result.consumed = True
        result.steps.append('staging_consumed')
    return result
