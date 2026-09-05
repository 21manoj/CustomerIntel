"""
Extraction scorecard — what the engine read vs what the manifest labelled.

For every submitted communication: the subtypes on the OBSERVED SIGNAL
nodes the engine wrote for it (what the journey actually sees) against
``expected_subtypes``. Reported per communication (exact / partial /
miss), per role (precision / recall), and overall, labelled with the
``model_version`` that answered — a keyword stub's numbers are reported
as the stub's, never dressed up.

The scorecard is also the seed of the labelled extraction eval set: the
JSONL written beside it carries text, source, labels and what was
extracted, one line per communication.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from signal_engine.pipeline import UNCLASSIFIED_SUBTYPE


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = (2 * p * r / (p + r)) if p and r else (0.0 if p is not None and r is not None else None)
    return {'tp': tp, 'fp': fp, 'fn': fn,
            'precision': round(p, 3) if p is not None else None,
            'recall': round(r, 3) if r is not None else None,
            'f1': round(f1, 3) if f1 is not None else None}


def build_scorecard(manifest: dict, comms: List[dict], ingested: Dict[str, dict], taxonomy,
                    *, pending: int = 0, errors: Optional[List[dict]] = None) -> dict:
    """`ingested`: comm ref → {'signal_id', 'status', 'extracted_subtypes', 'extracted_roles',
    'model_version', 'unclassified'} as read back from the DB after processing."""
    per_comm, versions = [], set()
    sub_tp = sub_fp = sub_fn = 0
    role_stats: Dict[str, dict] = {}
    exact = partial = miss = unclassified = duplicates = 0
    for c in comms:
        got = ingested.get(c['ref']) or {}
        exp = set(c['expected_subtypes'])
        ext = set(got.get('extracted_subtypes') or [])
        exp_roles = {taxonomy.signal_role(s) for s in exp} - {None}
        ext_roles = set(got.get('extracted_roles') or []) - {None}
        if got.get('model_version'):
            versions.add(got['model_version'])
        if got.get('status') == 'duplicate':
            duplicates += 1
        if got.get('unclassified'):
            unclassified += 1
        inter = exp & ext
        verdict = 'exact' if exp == ext else 'partial' if inter else 'miss'
        exact += verdict == 'exact'
        partial += verdict == 'partial'
        miss += verdict == 'miss'
        sub_tp += len(inter)
        sub_fp += len(ext - exp)
        sub_fn += len(exp - ext)
        for r in exp_roles | ext_roles:
            st = role_stats.setdefault(r, {'tp': 0, 'fp': 0, 'fn': 0})
            st['tp'] += r in exp_roles and r in ext_roles
            st['fp'] += r in ext_roles and r not in exp_roles
            st['fn'] += r in exp_roles and r not in ext_roles
        per_comm.append({
            'ref': c['ref'], 'account': c['account_name'], 'day': c['day'], 'source_type': c['source_type'],
            'signal_id': got.get('signal_id'), 'status': got.get('status'),
            'expected': sorted(exp), 'extracted': sorted(ext), 'verdict': verdict,
            'expected_roles': sorted(exp_roles), 'extracted_roles': sorted(ext_roles),
        })
    n = len(comms)
    return {
        'manifest_id': manifest.get('manifest_id'), 'vertical': manifest.get('vertical'),
        'model_version': sorted(versions)[0] if len(versions) == 1 else sorted(versions),
        'generated_at': datetime.utcnow().isoformat(),
        'label': ('oracle — manifest labels played back, 100% by construction, not a model result'
                  if versions == {'oracle_manifest_labels'} else
                  'keyword stub — no API key, expect misses' if versions == {'stub_keyword_v2'} else
                  'model extraction'),
        'communications': n, 'exact': exact, 'partial': partial, 'miss': miss,
        'hit_rate': round(exact / n, 3) if n else None,
        'unclassified': unclassified, 'duplicates': duplicates, 'pending': pending,
        'errors': errors or [],
        'subtype': _prf(sub_tp, sub_fp, sub_fn),
        'roles': {r: _prf(**st) for r, st in sorted(role_stats.items())},
        'per_communication': per_comm,
    }


def read_back(customer_id: int, submissions: Dict[str, dict]) -> Dict[str, dict]:
    """After processing: for each comm ref → what the engine wrote. Must run
    inside an app context. `submissions`: ref → ingest() result."""
    from models import QualitativeSignal, ContextNode
    out = {}
    ids = [s['signal_id'] for s in submissions.values() if s.get('signal_id')]
    sigs = {s.signal_id: s for s in QualitativeSignal.query.filter(QualitativeSignal.signal_id.in_(ids)).all()} if ids else {}
    nodes: Dict[str, list] = {}
    if ids:
        for nd in ContextNode.query.filter(ContextNode.customer_id == customer_id, ContextNode.node_type == 'SIGNAL',
                                           ContextNode.source_event_id.in_(ids)).all():
            nodes.setdefault(nd.source_event_id, []).append(nd)
    for ref, sub in submissions.items():
        sid = sub.get('signal_id')
        sig = sigs.get(sid)
        my_nodes = nodes.get(sid, [])
        subtypes = [nd.node_subtype for nd in my_nodes if nd.node_subtype != UNCLASSIFIED_SUBTYPE]
        out[ref] = {
            'signal_id': sid, 'status': sub.get('status'),
            'processed': bool(my_nodes),
            'extracted_subtypes': subtypes,
            'extracted_roles': [(nd.properties or {}).get('role') for nd in my_nodes if nd.node_subtype != UNCLASSIFIED_SUBTYPE],
            'unclassified': bool(my_nodes) and not subtypes,
            'model_version': (sig.llm_model_version if sig else None),
            'requires_review': (bool(sig.requires_review) if sig else None),
        }
    return out


def write_outputs(out_dir, tenant_tag: str, scorecard: dict, comms: List[dict], ingested: Dict[str, dict]) -> dict:
    """<out_dir>/<tenant>_scorecard.json and <tenant>_labelled.jsonl."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sc_path = out_dir / f'{tenant_tag}_scorecard.json'
    sc_path.write_text(json.dumps(scorecard, indent=1, default=str))
    lab_path = out_dir / f'{tenant_tag}_labelled.jsonl'
    with lab_path.open('w') as fh:
        for c in comms:
            got = ingested.get(c['ref']) or {}
            fh.write(json.dumps({
                'manifest_id': scorecard.get('manifest_id'), 'vertical': scorecard.get('vertical'),
                'ref': c['ref'], 'account': c['account_name'], 'day': c['day'],
                'occurred_at': c['occurred_at'].isoformat() if hasattr(c['occurred_at'], 'isoformat') else c['occurred_at'],
                'source_type': c['source_type'], 'text': c['text'],
                'participants': c['participants'],
                'expected_subtypes': c['expected_subtypes'], 'expected_sentiment': c.get('expected_sentiment'),
                'extracted_subtypes': got.get('extracted_subtypes'), 'extracted_roles': got.get('extracted_roles'),
                'requires_review': got.get('requires_review'),
                'model_version': got.get('model_version'), 'signal_id': got.get('signal_id'),
                'data_origin': 'synthetic_demo',
            }) + '\n')
    return {'scorecard': str(sc_path), 'labelled': str(lab_path)}


def format_scorecard(sc: dict) -> str:
    s = sc['subtype']
    lines = [f"### {sc['manifest_id']} — extraction scorecard [{sc['model_version']}] {sc['label']}",
             f"  communications={sc['communications']} exact={sc['exact']} partial={sc['partial']} miss={sc['miss']} "
             f"hit_rate={sc['hit_rate']} unclassified={sc['unclassified']} duplicates={sc['duplicates']} pending={sc['pending']}",
             f"  subtype  P={s['precision']} R={s['recall']} F1={s['f1']} (tp={s['tp']} fp={s['fp']} fn={s['fn']})"]
    for r, st in sc['roles'].items():
        lines.append(f"  role {r:<20} P={st['precision']!s:<6} R={st['recall']!s:<6} tp={st['tp']} fp={st['fp']} fn={st['fn']}")
    if sc['errors']:
        lines.append(f"  extraction errors: {len(sc['errors'])} (signals left queued)")
    return '\n'.join(lines)
