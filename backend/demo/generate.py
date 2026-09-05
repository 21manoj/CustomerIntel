"""
Protocol-shaped demo generator (v1 typed signals, v2 communications).

A manifest declares, per account, the story the demo needs to show — a
health curve (with the day trailing health should cross at-risk), the
CSM's own risk flag, and financial events with decision dates — plus
noise and background accounts. This module turns that into the canonical
CSVs and (optionally) registers the tenant through the real MCP tools,
stamped `data_origin='synthetic_demo'`.

v1 manifests (`signals: [{day, type, sentiment, ...}]`) write typed rows
to enhanced_qualitative_signals.csv — the CSV path.

v2 manifests (`"version": 2`, demo/manifest_v2.py) author COMMUNICATIONS
instead: raw text with a source, a time, the people on it, and the
subtypes a correct extractor should read. They are submitted through the
signal engine (`signal_engine.pipeline.ingest` → `process_pending`) AFTER
the CSVs are processed, so the evidence the journey sees is whatever the
engine extracted. The manifest's labels feed the scorecard
(demo/scorecard.py) and the labelled JSONL — the seed of the extraction
eval set. Signals are first-class: nothing in v2 writes a typed
behavioral signal row. What stays on CSV: accounts, KPIs, the CSM risk
flag (structured, `signal_type='csm_risk_flag'`), and — for now —
outcomes, behind `emit_outcomes()` (the seam for the outcome-logging
tool). `"kpis": "none"` makes a signals-only tenant (P1): no KPI rows,
the journey builds from evidence alone.

What it deliberately does NOT do (the reasons the old load-driver was
not extended — docs/design/demo-narratives.md §6):
  - derive signals from story phases: every signal is a declared event
    with a date, so the lead time is whatever the manifest set, and the
    harness reads it back — a check of the mechanism, not proof;
  - use a private arc vocabulary: nothing here mentions arcs; the
    classifier decides from the evidence like it would for a real tenant;
  - write account_id / skip data_origin / omit decision dates.

KPI values are produced by inverting the real scorer's 4-band curve
(`health_to_kpi_value`), so the trailing layer crosses at-risk in the
month the manifest asked for — through the catalog, not around it.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import utils.health_thresholds as ht
from demo.manifest_v2 import (ManifestError, background_accounts, comm_ref, is_v2, plan_communications,
                              signals_only, validate_manifest)

MANIFESTS_DIR = Path(__file__).resolve().parent / 'manifests'
OUT_DIR = Path(__file__).resolve().parent / 'out'
DATA_ORIGIN = 'synthetic_demo'
PENDING_BATCH = 100
MAX_PENDING_PASSES = 50      # a pass that processes nothing ends the loop; this is the ceiling, not the plan

ACCOUNT_COLUMNS = ['source_account_id', 'account_name', 'industry', 'region', 'arr', 'account_status',
                   'csm_name', 'csm_email', 'csm_manager', 'executive_sponsor', 'primary_champion_name',
                   'primary_champion_title', 'products', 'contract_start', 'contract_end', 'renewal_date',
                   'tier', 'use_cases', 'attributes']
KPI_COLUMNS = ['source_account_id', 'kpi_code', 'kpi_name', 'pillar', 'measured_at', 'value']
SIGNAL_COLUMNS = ['signal_id', 'source_account_id', 'signal_date', 'signal_type', 'content', 'sentiment',
                  'sentiment_score', 'stakeholder_name', 'stakeholder_title', 'signal_ref', 'source_platform']
OUTCOME_COLUMNS = ['source_account_id', 'outcome_date', 'outcome_type', 'title', 'revenue_value', 'evidence',
                   'linked_signal_id', 'confidence']


# ═══════════════════════════════════════════════════════════════════════
# Inverse of utils.generic_scorer.score_kpi
# ═══════════════════════════════════════════════════════════════════════

def health_to_kpi_value(h: float, kpi_def: dict) -> float:
    """The KPI value that score_kpi() maps to health h (0-100)."""
    h = max(0.0, min(100.0, h))
    target_raw = kpi_def.get('target', 100)
    target = target_raw.get('value', 100) if isinstance(target_raw, dict) else target_raw
    operator = target_raw.get('operator', '>') if isinstance(target_raw, dict) else '>'
    ranges = kpi_def.get('ranges', {})
    hib = kpi_def.get('higher_is_better', operator in ('>', '>='))
    at_target, at_risk = ht.healthy_min(), ht.at_risk_min()
    healthy, risk, critical = ranges.get('healthy', {}), ranges.get('risk', {}), ranges.get('critical', {})
    if hib:
        floor = critical.get('min', 0)
        rb = risk.get('min', critical.get('max', floor))
        hmax = healthy.get('max', target * 1.2)
        if h < at_risk:
            return floor + (rb - floor) * h / at_risk
        if h < at_target:
            return rb + (target - rb) * (h - at_risk) / (at_target - at_risk)
        return target + (hmax - target) * (h - at_target) / (100 - at_target)
    ceiling = critical.get('max', target * 4)
    rb = risk.get('max', critical.get('min', ceiling))
    hmin = healthy.get('min', 0)
    if h < at_risk:
        return ceiling - (ceiling - rb) * h / at_risk
    if h < at_target:
        return rb - (rb - target) * (h - at_risk) / (at_target - at_risk)
    return target - (target - hmin) * (h - at_target) / (100 - at_target)


# ═══════════════════════════════════════════════════════════════════════
# Health curves
# ═══════════════════════════════════════════════════════════════════════

def health_at(day: int, spec: dict, start_day: int, at_risk: float) -> float:
    """Health for a relative day from the account's curve spec.
    shapes: flat | ramp | decline (crosses at-risk at trailing_cross_day) |
            dip_recover (min at dip_day, back to `end`)."""
    shape = spec.get('shape', 'flat')
    s, e = float(spec['start']), float(spec.get('end', spec['start']))
    if shape == 'flat':
        return s
    if shape == 'ramp':
        t = (day - start_day) / max(1, (0 - start_day))
        return s + (e - s) * max(0.0, min(1.0, t))
    if shape == 'decline':
        cross = spec['trailing_cross_day']
        cross_h = at_risk - 3   # decisively below the band on the crossing day
        if day <= cross:
            t = (day - start_day) / max(1, (cross - start_day))
            return s + (cross_h - s) * max(0.0, min(1.0, t))
        t = (day - cross) / max(1, (0 - cross))
        return cross_h + (e - cross_h) * max(0.0, min(1.0, t))
    if shape == 'dip_recover':
        dip, dip_h = spec['dip_day'], float(spec['dip'])
        if day <= dip:
            t = (day - start_day) / max(1, (dip - start_day))
            return s + (dip_h - s) * max(0.0, min(1.0, t))
        t = (day - dip) / max(1, (0 - dip))
        return dip_h + (e - dip_h) * max(0.0, min(1.0, t))
    raise ValueError(f'unknown health shape {shape!r}')


# ═══════════════════════════════════════════════════════════════════════
# Generation
# ═══════════════════════════════════════════════════════════════════════

def _csv(rows: List[dict], columns: List[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def _v1_background_accounts(manifest: dict) -> List[dict]:
    bg = manifest.get('background') or {}
    rng = random.Random(manifest.get('seed', 1) + 7)
    out = []
    for i, name in enumerate(bg.get('names', [])):
        start = rng.uniform(*bg.get('health_range', [74, 88]))
        out.append({
            'source_account_id': f'BG-{i + 1}',
            'name': name, 'arr': rng.choice(bg.get('arr_choices', [900000, 1400000, 2100000, 3300000])),
            'industry': rng.choice(bg.get('industries', ['Technology'])), 'region': rng.choice(bg.get('regions', ['North America'])),
            'role': 'background',
            'health': {'shape': rng.choice(['flat', 'ramp']), 'start': round(start, 1), 'end': round(start + rng.uniform(-3, 4), 1), 'noise': 1.2},
            'renewal_day': rng.randint(120, 330),
            'signals': [
                {'day': -rng.randint(20, 150), 'type': 'routine_review', 'sentiment': 0.2,
                 'content': f'Routine quarterly review completed ({name})', 'stakeholder': 'Platform Lead', 'title': 'Director'},
                {'day': -rng.randint(10, 120), 'type': rng.choice(['advocacy', 'executive_engagement', 'health_improvement']),
                 'sentiment': 0.5, 'content': f'Positive engagement noted ({name})', 'stakeholder': 'VP Engineering', 'title': 'VP'},
            ],
            'events': [],
        })
    return out


def expand_accounts(manifest: dict) -> List[dict]:
    """Featured accounts as declared + background accounts synthesised from
    the `background` block (flat/ramp; light routine evidence — typed
    signals for v1, generated communications for v2)."""
    accounts = list(manifest.get('accounts', []))
    accounts.extend(background_accounts(manifest) if is_v2(manifest) else _v1_background_accounts(manifest))
    return accounts


def _account_row(a: dict, manifest: dict, t0: datetime, rng: random.Random) -> dict:
    sid = a.get('source_account_id') or a['name'].upper().replace(' ', '-')[:12]
    renewal = (t0 + timedelta(days=a.get('renewal_day', 180))).date().isoformat()
    products = a.get('products') or [{'name': manifest.get('default_product', 'Platform'), 'category': 'platform', 'arr': a['arr']}]
    return {
        'source_account_id': sid, 'account_name': a['name'], 'industry': a.get('industry', 'Technology'),
        'region': a.get('region', 'North America'), 'arr': a['arr'], 'account_status': 'active',
        'csm_name': a.get('csm', manifest.get('default_csm', 'Sarah Rivera')),
        'csm_email': a.get('csm_email', 'sarah.rivera@example.com'), 'csm_manager': manifest.get('csm_manager', 'Sam Rivera'),
        'executive_sponsor': a.get('executive_sponsor', ''),
        'primary_champion_name': a.get('champion', ''), 'primary_champion_title': a.get('champion_title', ''),
        'products': json.dumps(products), 'contract_start': (t0 - timedelta(days=365 + a.get('renewal_day', 180))).date().isoformat(),
        'contract_end': renewal, 'renewal_date': renewal, 'tier': a.get('tier', 'Enterprise'),
        'use_cases': json.dumps(a.get('use_cases') or []),                      # what they bought us for — read by the extractor + journey
        'attributes': json.dumps({'employee_count': a.get('employee_count', rng.randint(200, 5000)), **(a.get('attributes') or {})}),
    }


def generate(manifest: dict) -> Dict[str, str]:
    """Manifest → {canonical filename: csv text}. Deterministic for a seed.

    v1: the four canonical CSVs. v2: account_details always; kpi_measurements
    unless "kpis": "none"; enhanced_qualitative_signals only for CSM risk
    flags (structured path); outcomes.csv whose linked_signal_id is the
    communication's manifest ref (rewritten to the engine's signal id by
    emit_outcomes). Communications are not files — plan_communications()."""
    from utils.vertical_registry import get_kpis
    validate_manifest(manifest)
    v2 = is_v2(manifest)
    vertical = manifest['vertical']
    kpis = get_kpis(vertical) if not (v2 and signals_only(manifest)) else {}
    t0 = datetime.fromisoformat(manifest['timeline']['t0'])
    start_day = -int(manifest['timeline']['history_days'])
    end_day = int(manifest['timeline'].get('future_days', 14))
    cadence = int(manifest['timeline'].get('kpi_cadence_days', 7))
    rng = random.Random(manifest.get('seed', 1))
    at_risk = ht.at_risk_min()

    accounts = expand_accounts(manifest)
    acct_rows, kpi_rows, sig_rows, out_rows = [], [], [], []

    for a in accounts:
        row = _account_row(a, manifest, t0, rng)
        sid = row['source_account_id']
        acct_rows.append(row)

        if kpis:
            spec = dict(a['health'])
            noise = float(spec.get('noise', 1.5))
            day = start_day
            while day <= end_day:
                h_day = health_at(day, spec, start_day, at_risk)
                when = (t0 + timedelta(days=day)).date().isoformat()
                for code, kdef in kpis.items():
                    h = h_day + rng.gauss(0, noise)
                    kpi_rows.append({
                        'source_account_id': sid, 'kpi_code': code, 'kpi_name': kdef.get('name', code),
                        'pillar': kdef.get('pillar'), 'measured_at': when,
                        'value': round(health_to_kpi_value(h, kdef), 3),
                    })
                day += cadence

        if not v2:
            for i, s in enumerate(a.get('signals', [])):
                sig_id = f'{sid.lower()}_sig_{i + 1}'
                sig_rows.append({
                    'signal_id': sig_id, 'source_account_id': sid,
                    'signal_date': (t0 + timedelta(days=int(s['day']))).date().isoformat(),
                    'signal_type': s['type'], 'content': s.get('content', s['type'].replace('_', ' ')),
                    'sentiment': 'positive' if s.get('sentiment', 0) > 0.1 else 'negative' if s.get('sentiment', 0) < -0.1 else 'neutral',
                    'sentiment_score': s.get('sentiment', 0.0), 'stakeholder_name': s.get('stakeholder', ''),
                    'stakeholder_title': s.get('title', ''), 'signal_ref': sig_id, 'source_platform': s.get('source', 'crm'),
                })
        if a.get('crm_flag_day') is not None:
            sig_id = f'{sid.lower()}_crm_flag'
            sig_rows.append({
                'signal_id': sig_id, 'source_account_id': sid,
                'signal_date': (t0 + timedelta(days=int(a['crm_flag_day']))).date().isoformat(),
                'signal_type': 'csm_risk_flag', 'content': f"CSM marked renewal at risk ({a['name']})",
                'sentiment': 'negative', 'sentiment_score': -0.5, 'stakeholder_name': row['csm_name'],
                'stakeholder_title': 'CSM', 'signal_ref': sig_id, 'source_platform': 'crm',
            })

        for e in a.get('events', []):
            linked = None
            if v2 and e.get('linked_communication_index') is not None:
                linked = comm_ref(sid, int(e['linked_communication_index']))
            elif not v2 and e.get('linked_signal_index') is not None:
                linked = f"{sid.lower()}_sig_{int(e['linked_signal_index']) + 1}"
            out_rows.append({
                'source_account_id': sid, 'outcome_date': (t0 + timedelta(days=int(e['day']))).date().isoformat(),
                'outcome_type': e['type'], 'title': e.get('title', e['type'].replace('_', ' ').title()),
                'revenue_value': e['amount'], 'evidence': e.get('evidence', ''), 'linked_signal_id': linked or '',
                'confidence': e.get('confidence', ''),
            })

    files = {'account_details.csv': _csv(acct_rows, ACCOUNT_COLUMNS)}
    if not v2 or kpi_rows:
        files['kpi_measurements.csv'] = _csv(kpi_rows, KPI_COLUMNS)
    if not v2 or sig_rows:
        files['enhanced_qualitative_signals.csv'] = _csv(sig_rows, SIGNAL_COLUMNS)
    if not v2 or out_rows:
        files['outcomes.csv'] = _csv(out_rows, OUTCOME_COLUMNS)
    return files


# ═══════════════════════════════════════════════════════════════════════
# Registration — v1 (all CSV)
# ═══════════════════════════════════════════════════════════════════════

def _create_tenant(manifest: dict, tag: str) -> int:
    from mcp_server.cs_pulse_onboarding import create_customer
    res = create_customer(
        name=f"{manifest['customer_name']} {tag}".strip(), domain=f"{manifest['domain_prefix']}-{tag}.demo",
        vertical=manifest['vertical'], admin_email=f"admin-{tag}@{manifest['domain_prefix']}.demo",
        admin_name='Demo Admin', data_origin=DATA_ORIGIN,        # declared at creation, disclosed everywhere
    )
    return res['customer_id']


def _upload(cid: int, files: Dict[str, str]) -> None:
    from mcp_server.cs_pulse_onboarding import upload_csv
    for ft, content in files.items():
        r = upload_csv(cid, ft, content)
        for w in r.get('warnings') or []:
            if 'Unknown columns' in w:
                print(f'  note: {ft}: {w}', file=sys.stderr)


def _register_v1(manifest: dict, files: Dict[str, str], tag: str) -> dict:
    from mcp_server.cs_pulse_onboarding import process_data
    cid = _create_tenant(manifest, tag)
    _upload(cid, files)
    out = process_data(cid)
    return {'customer_id': cid, 'status': out['status'], 'steps': out['steps_completed'], 'errors': out['errors'],
            'wizard_a': out.get('wizard_a')}


# ═══════════════════════════════════════════════════════════════════════
# Registration — v2 (CSV for roster/KPIs/flag, the engine for communications)
# ═══════════════════════════════════════════════════════════════════════

def submit_communications(customer_id: int, comms: List[dict]) -> Dict[str, dict]:
    """ingest() every planned communication, dated by the event. Returns
    comm ref → ingest result (status queued | duplicate)."""
    from models import Account
    from signal_engine.pipeline import ingest
    by_source = {a.external_account_id: a.account_id
                 for a in Account.query.filter_by(customer_id=customer_id).all() if a.external_account_id}
    out = {}
    for c in comms:
        aid = by_source.get(c['source_account_id'])
        if aid is None:
            raise RuntimeError(f"account {c['source_account_id']!r} was not created from account_details.csv")
        out[c['ref']] = ingest(customer_id, aid, c['source_type'], c['text'], occurred_at=c['occurred_at'],
                               participants=c['participants'], source_ref=c['source_ref'],
                               consent_verified=True if c['source_type'] == 'transcript' else None)
    return out


def drain_pending(customer_id: int) -> dict:
    """process_pending() until a pass processes nothing. Extraction errors
    leave signals queued (not evidence) — counted, never forced. Journeys
    are rebuilt once at the end, not per batch."""
    from signal_engine.pipeline import process_pending
    from models import QualitativeSignal
    totals = {'processed': 0, 'structured': 0, 'enriched': 0, 'unclassified': 0, 'nodes_written': 0,
              'errors': 0, 'error_signals': [], 'passes': 0}
    for _ in range(MAX_PENDING_PASSES):
        out = process_pending(customer_id=customer_id, limit=PENDING_BATCH, rebuild_journeys=False)
        totals['passes'] += 1
        for k in ('processed', 'structured', 'enriched', 'unclassified', 'nodes_written', 'errors'):
            totals[k] += out[k]
        totals['error_signals'].extend(out['error_signals'])
        if out['processed'] == 0:
            break
    totals['pending'] = (QualitativeSignal.query
                         .filter(QualitativeSignal.customer_id == customer_id, QualitativeSignal.source_type.isnot(None),
                                 QualitativeSignal.cg_node_id.is_(None)).count())
    return totals


def emit_outcomes(customer_id: int, outcomes_csv: Optional[str], link_map: Dict[str, str]) -> dict:
    """SEAM — financial events (outcomes) enter here and nowhere else.

    Today: the outcomes CSV, with each linked_signal_id rewritten from the
    manifest's communication ref to the engine's signal id, is staged
    through upload_csv; the single process_data that follows in
    register_v2 ingests it (OUTCOME nodes + LED_TO edges) with everything
    else in place. When the outcome-logging MCP tool lands, this body
    becomes one call per event (account, date, type, amount, evidence,
    linked signal id) and the CSV disappears; callers and the manifest do
    not change, and process_data stays for the KPI layer."""
    if not outcomes_csv:
        return {'emitted': 0, 'path': 'none'}
    from mcp_server.cs_pulse_onboarding import upload_csv
    rows = list(csv.DictReader(io.StringIO(outcomes_csv)))
    for r in rows:
        r['linked_signal_id'] = link_map.get(r['linked_signal_id'], '') if r['linked_signal_id'] else ''
    upload_csv(customer_id, 'outcomes.csv', _csv(rows, OUTCOME_COLUMNS))
    return {'emitted': len(rows), 'path': 'outcomes.csv (staged; ingested by process_data)'}


def register_v2(manifest: dict, files: Dict[str, str], tag: str, *, extractor: Union[str, Callable, None] = None,
                out_dir: Optional[Union[str, Path]] = OUT_DIR) -> dict:
    """create_customer → CSVs (roster, KPIs, CSM flag) staged and ingested
    → communications through the engine → journeys from the evidence →
    outcomes (seam) → ONE process_data (health scores, journeys over all
    evidence, Wizard B) → scorecard + labelled set.

    Evidence lands before the pipeline runs, the way it would for a real
    tenant, so Wizard B's persisted Hindsight run sees the whole story and
    runs once. A signals-only tenant with no events never needs
    process_data at all (nothing to score); its journeys come from the
    evidence-only Wizard A pass.

    `extractor`: demo.oracle.EXTRACTORS or a callable; None = 'auto' (the
    model with an API key, the oracle without — the scorecard says which)."""
    from utils.taxonomy_loader import get_taxonomy
    from utils.vertical_registry import get_vertical_for_customer
    from utils.csv_ingest import ingest_staged_csvs
    from journeys.wizard_a import run_wizard_a
    from mcp_server.cs_pulse_onboarding import process_data
    from demo.oracle import extractor_override
    from demo.scorecard import build_scorecard, read_back, write_outputs, format_scorecard

    comms = plan_communications(manifest, expand_accounts(manifest))
    cid = _create_tenant(manifest, tag)
    _upload(cid, {k: v for k, v in files.items() if k != 'outcomes.csv'})
    ingest = ingest_staged_csvs(cid, get_vertical_for_customer(cid))     # roster, KPI rows, CSM flag — no scoring yet
    if ingest.errors:
        raise RuntimeError(f'csv ingest failed for {manifest["manifest_id"]}: {ingest.errors}')

    with extractor_override(extractor, comms):
        submissions = submit_communications(cid, comms)
        drained = drain_pending(cid)
    wa = run_wizard_a(cid)                      # journeys from the evidence alone (before any score or outcome)

    link_map = {ref: (s.get('signal_id') or s.get('duplicate_of')) for ref, s in submissions.items()}
    outcomes = emit_outcomes(cid, files.get('outcomes.csv'), link_map)
    pipeline = None
    if 'kpi_measurements.csv' in files or outcomes['emitted']:
        pipeline = process_data(cid)            # the one pipeline run: scores, journeys, Wizard B

    ingested = read_back(cid, submissions)
    sc = build_scorecard(manifest, comms, ingested, get_taxonomy(manifest['vertical']),
                         pending=drained['pending'], errors=drained['error_signals'])
    print(format_scorecard(sc))
    paths = write_outputs(out_dir, f"{manifest['manifest_id']}_{tag}" if tag else manifest['manifest_id'],
                          sc, comms, ingested) if out_dir else {}
    final_wa = (pipeline or {}).get('wizard_a') or {'coverage': wa['coverage'], 'arcs': wa['arcs']}
    errors = list((pipeline or {}).get('errors') or [])
    steps = list(ingest.steps) + [
        f"communications_submitted_{len(submissions)}",
        f"signals_processed_{drained['processed']}_unclassified_{drained['unclassified']}_pending_{drained['pending']}",
        f"wizard_a_{wa['processed']}_journeys_from_evidence",
        f"outcomes_emitted_{outcomes['emitted']}",
    ] + list((pipeline or {}).get('steps_completed') or [])
    return {'customer_id': cid, 'status': 'success' if not errors else 'partial', 'steps': steps,
            'errors': errors, 'wizard_a': final_wa, 'scorecard': sc, 'signals': drained, 'outputs': paths,
            'signals_only': signals_only(manifest)}


def register(manifest: dict, files: Dict[str, str], *, name_suffix: str = '', extractor=None, out_dir=OUT_DIR) -> dict:
    """Create the tenant through the real MCP tools, stamp data_origin, run
    the pipeline. v2 manifests go through the signal engine (register_v2)."""
    tag = name_suffix or datetime.utcnow().strftime('%m%d%H%M%S')
    if is_v2(manifest):
        return register_v2(manifest, files, tag, extractor=extractor, out_dir=out_dir)
    return _register_v1(manifest, files, tag)


def load_manifest(path) -> dict:
    """Read + validate (v2 fails loudly on an unknown subtype, source type, etc.)."""
    manifest = json.loads(Path(path).read_text())
    validate_manifest(manifest)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description='Generate (and optionally register) a protocol-shaped demo tenant.')
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out-dir', help='write the CSVs here')
    ap.add_argument('--register', action='store_true', help='create the tenant via MCP tools (needs DATABASE_URL)')
    ap.add_argument('--extractor', choices=['auto', 'model', 'stub', 'oracle'], default='auto',
                    help='v2 only: auto (model with ANTHROPIC_API_KEY, else oracle), model (engine default), '
                         'stub (keyword floor), oracle (manifest labels — not a model result)')
    ap.add_argument('--scorecard-dir', default=str(OUT_DIR), help='v2 only: where the scorecard + labelled JSONL go')
    args = ap.parse_args(argv)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    manifest = load_manifest(args.manifest)
    files = generate(manifest)
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        for ft, content in files.items():
            Path(args.out_dir, ft).write_text(content)
        print(f"wrote {len(files)} files to {args.out_dir}: " + ', '.join(f"{k} ({content.count(chr(10)) - 1} rows)" for k, content in files.items()))
        if is_v2(manifest):
            comms = plan_communications(manifest, expand_accounts(manifest))
            Path(args.out_dir, 'communications.jsonl').write_text(''.join(json.dumps(c, default=str) + '\n' for c in comms))
            print(f"wrote communications.jsonl ({len(comms)} communications) — submitted through the engine on --register")
    if args.register:
        from flask import Flask
        from extensions import db
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
        db.init_app(app)
        import mcp_server.common as _common
        _common._flask_app = app
        with app.app_context():
            res = register(manifest, files, extractor=args.extractor, out_dir=args.scorecard_dir)
            res.pop('scorecard', None)      # printed above; full detail is in the scorecard file
            print(json.dumps(res, indent=1, default=str))


if __name__ == '__main__':
    main()
