"""
Compare two signal-reading models on the same data, before switching.

Builds the same demo manifest twice as two throwaway tenants — once read by model A, once by model B — and reports
where they disagree on what matters downstream:
  • the role(s) read out of each communication,
  • each account's journey (arc, health band),
  • the playbooks the platform would propose (dry run).

A side is a model id (paid: needs --allow-spend), or one of the free readers:
  stub    the engine's own no-key path        oracle  the manifest's intended labels (the answer key)
Comparing `stub` with `oracle` costs nothing and proves the plumbing; comparing a model with `oracle` scores it.

    python scripts/compare_models.py --a oracle --b stub
    python scripts/compare_models.py --a claude-sonnet-5 --b <new-model-id> --allow-spend [--manifest path.json] [--out file.json]

Run against a scratch database: each run creates two synthetic tenants (domain <prefix>-cmpa…/cmpb….demo).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FREE = ('stub', 'oracle')
MODEL_ENV = 'SIGNAL_ENRICHMENT_MODEL'


def is_paid(side: str) -> bool:
    return side not in FREE


def build_side(manifest: dict, side: str, tag: str) -> dict:
    """Register the manifest as a new tenant read by `side`; return what the platform concluded. Needs an app context."""
    from demo.generate import generate, register
    from models import Account, JourneyData, QualitativeSignal
    from playbooks import governance as gov
    saved = os.environ.get(MODEL_ENV)
    try:
        if is_paid(side):
            os.environ[MODEL_ENV] = side
        result = register(manifest, generate(manifest), name_suffix=tag, extractor='model' if is_paid(side) else side, out_dir=None)
    finally:
        if saved is None:
            os.environ.pop(MODEL_ENV, None)
        else:
            os.environ[MODEL_ENV] = saved
    cid = result['customer_id']
    names = {a.account_id: a.account_name for a in Account.query.filter_by(customer_id=cid).all()}
    signals = {}
    for s in QualitativeSignal.query.filter_by(customer_id=cid).order_by(QualitativeSignal.id).all():
        key = f"{names.get(s.account_id)} | {s.signal_date} | {(s.raw_text or s.content or '')[:60]}"
        signals[key] = {'roles': sorted({str(x.get('role')) for x in (s.extractions or []) if isinstance(x, dict) and x.get('role')}),
                        'subtypes': sorted({str(x.get('subtype')) for x in (s.extractions or []) if isinstance(x, dict) and x.get('subtype')}),
                        'model': s.llm_model_version, 'needs_review': bool(s.requires_review)}
    accounts = {}
    for jd in JourneyData.query.filter_by(customer_id=cid).all():
        j = jd.journey_json or {}
        accounts[names.get(jd.account_id)] = {'arc': (j.get('arc') or {}).get('arc_type'), 'arc_state': (j.get('arc') or {}).get('state'),
                                              'health_band': (j.get('features') or {}).get('health_band'),
                                              'phase': j.get('current_phase')}
    ev = gov.evaluate(cid, dry_run=True)
    playbooks = sorted(f"{names.get(p.get('account_id'))} → {p.get('playbook_id')}" for p in ev.get('proposed', []) + ev.get('auto_approved', []))
    return {'side': side, 'customer_id': cid, 'status': result['status'], 'signals': signals, 'accounts': accounts, 'playbooks': playbooks,
            'unclassified': (result.get('signals') or {}).get('unclassified'),
            'readers_recorded': sorted({v['model'] for v in signals.values() if v['model']})}


def diff(a: dict, b: dict) -> dict:
    sig_keys = sorted(set(a['signals']) | set(b['signals']))
    sig_diff = [{'communication': k, 'a': (a['signals'].get(k) or {}).get('roles'), 'b': (b['signals'].get(k) or {}).get('roles')}
                for k in sig_keys if (a['signals'].get(k) or {}).get('roles') != (b['signals'].get(k) or {}).get('roles')]
    acct_keys = sorted(set(a['accounts']) | set(b['accounts']), key=str)
    acct_diff = [{'account': k, 'a': a['accounts'].get(k), 'b': b['accounts'].get(k)}
                 for k in acct_keys if a['accounts'].get(k) != b['accounts'].get(k)]
    pa, pb = set(a['playbooks']), set(b['playbooks'])
    n = len(sig_keys)
    return {
        'communications': n, 'communications_read_differently': len(sig_diff),
        'role_agreement': round(1 - len(sig_diff) / n, 4) if n else None,
        'accounts': len(acct_keys), 'accounts_concluded_differently': len(acct_diff),
        'playbooks_a': len(pa), 'playbooks_b': len(pb),
        'playbooks_only_a': sorted(pa - pb), 'playbooks_only_b': sorted(pb - pa),
        'signal_differences': sig_diff, 'account_differences': acct_diff,
    }


def compare(manifest: dict, a: str, b: str, *, allow_spend: bool = False) -> dict:
    paid = [s for s in (a, b) if is_paid(s)]
    if paid and not allow_spend:
        raise PermissionError(f"{', '.join(paid)} would call a paid model: pass --allow-spend to agree to the spend")
    if paid and not os.environ.get('ANTHROPIC_API_KEY') and not _provider_available():
        raise ValueError('no model is configured in this environment, so only stub and oracle can be compared')
    stamp = datetime.utcnow().strftime('%d%H%M%S')
    side_a = build_side(manifest, a, f'cmpa{stamp}')
    side_b = build_side(manifest, b, f'cmpb{stamp}')
    return {'at': datetime.utcnow().isoformat(), 'manifest': manifest['manifest_id'], 'vertical': manifest['vertical'],
            'a': {k: side_a[k] for k in ('side', 'customer_id', 'status', 'unclassified', 'readers_recorded', 'playbooks')},
            'b': {k: side_b[k] for k in ('side', 'customer_id', 'status', 'unclassified', 'readers_recorded', 'playbooks')},
            'spend': 'none — free readers only' if not paid else 'paid model calls were made',
            'diff': diff(side_a, side_b)}


def _provider_available() -> bool:
    from utils import model_provider
    return model_provider.available()


def format_report(r: dict) -> str:
    d = r['diff']
    lines = [f"Model comparison — manifest {r['manifest']} ({r['vertical']}) — spend: {r['spend']}",
             f"  A = {r['a']['side']} (tenant {r['a']['customer_id']})   B = {r['b']['side']} (tenant {r['b']['customer_id']})",
             f"  communications: {d['communications']}, read differently: {d['communications_read_differently']}"
             f" (role agreement {d['role_agreement']})",
             f"  accounts: {d['accounts']}, concluded differently: {d['accounts_concluded_differently']}",
             f"  playbooks proposed: A {d['playbooks_a']}, B {d['playbooks_b']};"
             f" only A {len(d['playbooks_only_a'])}, only B {len(d['playbooks_only_b'])}"]
    for s in d['signal_differences'][:15]:
        lines.append(f"    ~ {s['communication']}\n        A: {s['a']}   B: {s['b']}")
    for s in d['account_differences'][:15]:
        lines.append(f"    ~ account {s['account']}\n        A: {s['a']}\n        B: {s['b']}")
    for p in d['playbooks_only_a']:
        lines.append(f'    only A proposes: {p}')
    for p in d['playbooks_only_b']:
        lines.append(f'    only B proposes: {p}')
    return '\n'.join(lines)


def main(argv=None) -> int:
    from demo.generate import MANIFESTS_DIR, load_manifest
    ap = argparse.ArgumentParser(description='Compare two signal-reading models on the same demo data.')
    ap.add_argument('--manifest', default=str(Path(MANIFESTS_DIR) / 'demo_signals_only_saas.json'))
    ap.add_argument('--a', required=True, help='model id, or stub / oracle')
    ap.add_argument('--b', required=True, help='model id, or stub / oracle')
    ap.add_argument('--allow-spend', action='store_true', help='required when either side is a paid model')
    ap.add_argument('--out', help='write the full comparison JSON here')
    args = ap.parse_args(argv)
    from mcp_server import auth
    from mcp_server.common import get_flask_app
    auth._current_api_key_var.set(os.environ.get('MCP_SERVER_API_KEY', ''))
    with get_flask_app().app_context():
        try:
            r = compare(load_manifest(args.manifest), args.a, args.b, allow_spend=args.allow_spend)
        except (PermissionError, ValueError) as e:
            print(f'refused: {e}', file=sys.stderr)
            return 2
    print(format_report(r))
    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=2, default=str))
        print(f'  full comparison: {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
