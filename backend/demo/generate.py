"""
Protocol-shaped demo generator.

A manifest declares, per account, the story the demo needs to show — a
health curve (with the day trailing health should cross at-risk), dated
behavioral signals, the CSM's own risk flag, and financial events with
decision dates — plus noise and background accounts. This module turns
that into the four canonical CSVs and (optionally) registers the tenant
through the real MCP tools, stamped `data_origin='synthetic_demo'`.

What it deliberately does NOT do (the reasons the old load-driver was
not extended — docs/design/demo-narratives.md §6):
  - derive signals from story phases: signals are declared events with
    dates and sentiment, so the lead time is whatever the manifest set,
    and the harness reads it back — a check of the mechanism, not proof;
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
import math
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import utils.health_thresholds as ht

MANIFESTS_DIR = Path(__file__).resolve().parent / 'manifests'
DATA_ORIGIN = 'synthetic_demo'


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


def expand_accounts(manifest: dict) -> List[dict]:
    """Featured accounts as declared + background accounts synthesised from
    the `background` block (flat/ramp, light routine signals)."""
    accounts = list(manifest.get('accounts', []))
    bg = manifest.get('background') or {}
    rng = random.Random(manifest.get('seed', 1) + 7)
    for i, name in enumerate(bg.get('names', [])):
        start = rng.uniform(*bg.get('health_range', [74, 88]))
        accounts.append({
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
    return accounts


def generate(manifest: dict) -> Dict[str, str]:
    """Manifest → {canonical filename: csv text}. Deterministic for a seed."""
    from utils.vertical_registry import get_kpis
    vertical = manifest['vertical']
    kpis = get_kpis(vertical)
    t0 = datetime.fromisoformat(manifest['timeline']['t0'])
    start_day = -int(manifest['timeline']['history_days'])
    end_day = int(manifest['timeline'].get('future_days', 14))
    cadence = int(manifest['timeline'].get('kpi_cadence_days', 7))
    rng = random.Random(manifest.get('seed', 1))
    at_risk = ht.at_risk_min()

    accounts = expand_accounts(manifest)
    acct_rows, kpi_rows, sig_rows, out_rows = [], [], [], []

    for a in accounts:
        sid = a.get('source_account_id') or a['name'].upper().replace(' ', '-')[:12]
        renewal = (t0 + timedelta(days=a.get('renewal_day', 180))).date().isoformat()
        products = a.get('products') or [{'name': manifest.get('default_product', 'Platform'), 'category': 'platform', 'arr': a['arr']}]
        acct_rows.append({
            'source_account_id': sid, 'account_name': a['name'], 'industry': a.get('industry', 'Technology'),
            'region': a.get('region', 'North America'), 'arr': a['arr'], 'account_status': 'active',
            'csm_name': a.get('csm', manifest.get('default_csm', 'Sarah Rivera')),
            'csm_email': a.get('csm_email', 'sarah.rivera@example.com'), 'csm_manager': manifest.get('csm_manager', 'Sam Rivera'),
            'executive_sponsor': a.get('executive_sponsor', ''),
            'primary_champion_name': a.get('champion', ''), 'primary_champion_title': a.get('champion_title', ''),
            'products': json.dumps(products), 'contract_start': (t0 - timedelta(days=365 + a.get('renewal_day', 180))).date().isoformat(),
            'contract_end': renewal, 'renewal_date': renewal, 'tier': a.get('tier', 'Enterprise'),
            'employee_count': a.get('employee_count', rng.randint(200, 5000)),
        })

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
                'sentiment': 'negative', 'sentiment_score': -0.5, 'stakeholder_name': acct_rows[-1]['csm_name'],
                'stakeholder_title': 'CSM', 'signal_ref': sig_id, 'source_platform': 'crm',
            })

        for e in a.get('events', []):
            linked = None
            if e.get('linked_signal_index') is not None:
                linked = f"{sid.lower()}_sig_{int(e['linked_signal_index']) + 1}"
            out_rows.append({
                'source_account_id': sid, 'outcome_date': (t0 + timedelta(days=int(e['day']))).date().isoformat(),
                'outcome_type': e['type'], 'title': e.get('title', e['type'].replace('_', ' ').title()),
                'revenue_value': e['amount'], 'evidence': e.get('evidence', ''), 'linked_signal_id': linked or '',
                'confidence': e.get('confidence', ''),
            })

    return {
        'account_details.csv': _csv(acct_rows, ['source_account_id', 'account_name', 'industry', 'region', 'arr', 'account_status',
                                                'csm_name', 'csm_email', 'csm_manager', 'executive_sponsor', 'primary_champion_name',
                                                'primary_champion_title', 'products', 'contract_start', 'contract_end', 'renewal_date',
                                                'tier', 'employee_count']),
        'kpi_measurements.csv': _csv(kpi_rows, ['source_account_id', 'kpi_code', 'kpi_name', 'pillar', 'measured_at', 'value']),
        'enhanced_qualitative_signals.csv': _csv(sig_rows, ['signal_id', 'source_account_id', 'signal_date', 'signal_type', 'content',
                                                            'sentiment', 'sentiment_score', 'stakeholder_name', 'stakeholder_title',
                                                            'signal_ref', 'source_platform']),
        'outcomes.csv': _csv(out_rows, ['source_account_id', 'outcome_date', 'outcome_type', 'title', 'revenue_value', 'evidence',
                                        'linked_signal_id', 'confidence']),
    }


def register(manifest: dict, files: Dict[str, str], *, name_suffix: str = '') -> dict:
    """Create the tenant through the real MCP tools, stamp data_origin, run the pipeline."""
    from models import Customer
    from extensions import db
    from mcp_server.cs_pulse_onboarding import create_customer, upload_csv, process_data
    tag = name_suffix or datetime.utcnow().strftime('%m%d%H%M%S')
    res = create_customer(
        name=f"{manifest['customer_name']} {tag}".strip(), domain=f"{manifest['domain_prefix']}-{tag}.demo",
        vertical=manifest['vertical'], admin_email=f"admin-{tag}@{manifest['domain_prefix']}.demo",
        admin_name='Demo Admin',
    )
    cid = res['customer_id']
    c = db.session.get(Customer, cid)
    c.data_origin = DATA_ORIGIN
    db.session.commit()
    for ft, content in files.items():
        r = upload_csv(cid, ft, content)
        if r.get('warnings'):
            for w in r['warnings']:
                if 'Unknown columns' in w:
                    print(f'  note: {ft}: {w}', file=sys.stderr)
    out = process_data(cid)
    return {'customer_id': cid, 'status': out['status'], 'steps': out['steps_completed'], 'errors': out['errors'],
            'wizard_a': out.get('wizard_a')}


def load_manifest(path) -> dict:
    return json.loads(Path(path).read_text())


def main(argv=None):
    ap = argparse.ArgumentParser(description='Generate (and optionally register) a protocol-shaped demo tenant.')
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out-dir', help='write the four CSVs here')
    ap.add_argument('--register', action='store_true', help='create the tenant via MCP tools (needs DATABASE_URL)')
    args = ap.parse_args(argv)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    manifest = load_manifest(args.manifest)
    files = generate(manifest)
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        for ft, content in files.items():
            Path(args.out_dir, ft).write_text(content)
        print(f"wrote {len(files)} files to {args.out_dir}: " + ', '.join(f"{k} ({content.count(chr(10)) - 1} rows)" for k, content in files.items()))
    if args.register:
        from flask import Flask
        from extensions import db
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
        db.init_app(app)
        import mcp_server.common as _common
        _common._flask_app = app
        with app.app_context():
            print(json.dumps(register(manifest, files), indent=1, default=str))


if __name__ == '__main__':
    main()
