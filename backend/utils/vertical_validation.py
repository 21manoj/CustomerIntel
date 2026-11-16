"""
validate-my-vertical — does a vertical's config hang together, before any data is loaded?

    from utils.vertical_validation import validate_vertical
    validate_vertical('my_vertical')   # {'vertical', 'ok', 'checks': [{id, status, detail}]}

    python scripts/validate_vertical.py my_vertical        # the same, printed; exit 1 when not ok

A vertical is config only (catalog, taxonomy, playbooks, economics, investment, optional Ask AI
questions). Each file has its own loader that refuses a bad file; this runs every loader for one
vertical, adds the cross-file checks no single loader can make (every playbook has a cost entry;
the economics file matches the money basis the catalog declares), and reports all of it in one list
so an FDE fixes their own files without reading platform code. No database, no network.

status: pass | fail | warn. `ok` is false when any check fails. A warn is something that works
but is probably not what was meant (no playbooks, a playbook with no cost entry).
"""
from __future__ import annotations

from typing import Callable, List


def validate_vertical(vertical: str) -> dict:
    from utils import vertical_registry as reg
    checks: List[dict] = []

    def run(check_id: str, fn: Callable[[], object]) -> bool:
        """fn returns a detail string (pass), ('warn', detail), or raises (fail)."""
        try:
            got = fn()
        except Exception as e:                       # every loader's refusal is the FDE's message
            checks.append({'id': check_id, 'status': 'fail', 'detail': f'{type(e).__name__}: {e}'})
            return False
        status, detail = got if isinstance(got, tuple) else ('pass', got)
        checks.append({'id': check_id, 'status': status, 'detail': detail})
        return status != 'fail'

    v = reg.normalize_vertical(vertical)

    def registered():
        if v not in reg.SUPPORTED_VERTICALS:
            raise ValueError(f'no catalog file config/{v}_kpi_catalog.json (known: {", ".join(sorted(reg.SUPPORTED_VERTICALS))})')
        return f'catalog file found for {v}' + (f' (alias of {vertical})' if v != vertical else '')
    if not run('catalog_file', registered):
        return {'vertical': v, 'ok': False, 'checks': checks}

    def catalog():
        pillars, kpis = reg.get_pillars(v) or {}, reg.get_kpis(v) or {}
        if not pillars or not kpis:
            if v in reg.SIGNALS_ONLY_VERTICALS:
                return ('warn', 'catalog is empty: signals-only vertical, no health score')
            raise ValueError('catalog has no pillars or no KPIs')
        orphans = sorted(c for c, k in kpis.items() if k.get('pillar') not in pillars)
        if orphans:
            raise ValueError(f'KPIs name a pillar the catalog does not define: {", ".join(orphans)}')
        empty = sorted(p for p in pillars if not any(k.get('pillar') == p for k in kpis.values()))
        if empty:
            raise ValueError(f'pillars with no KPI: {", ".join(empty)}')
        core = sum(1 for k in kpis.values() if k.get('core'))
        return f'{len(pillars)} pillars, {len(kpis)} KPIs ({core} core)'
    run('catalog', catalog)

    def weights():
        w = reg.get_default_pillar_weights(v) or {}
        total = sum(float(x) for x in w.values())
        if w and abs(total - 1.0) > 0.01 and abs(total - 100.0) > 1.0:
            raise ValueError(f'default pillar weights sum to {total:g}, expected 1 (or 100)')
        return f'pillar weights sum to {total:g}' if w else ('warn', 'no default pillar weights declared')
    run('pillar_weights', weights)

    run('capabilities', lambda: ', '.join(f'{k}={val}' for k, val in sorted(reg.capabilities(v).items())))
    run('labels', lambda: ', '.join(f'{k}="{val}"' for k, val in sorted(reg.labels(v).items())))

    def taxonomy():
        from utils.taxonomy_loader import get_taxonomy
        t = get_taxonomy(v)
        if not t:
            raise ValueError(f'no taxonomy for {v}')
        return 'taxonomy loads'
    run('taxonomy', taxonomy)

    playbook_ids: List[str] = []

    def playbooks():
        from playbooks import definitions
        definitions.reset_cache()
        loaded = definitions.load_vertical(v)
        playbook_ids.extend(p['id'] for p in loaded['playbooks'])
        if not loaded['playbooks']:
            return ('warn', loaded.get('note') or 'the playbook file defines no playbooks')
        fact = [p['id'] for p in loaded['playbooks'] if (p.get('trigger') or {}).get('kpi') or (p.get('trigger') or {}).get('date')]
        multi = [p for p in loaded['playbooks'] if p.get('steps')]
        flags = (loaded.get('always_human') or {}).get('account_flags') or []
        effects = [p['id'] for p in loaded['playbooks'] if p.get('effects') or any(st.get('effects') for st in p.get('steps') or [])]
        return (f"{len(loaded['playbooks'])} playbooks (version {loaded['version']}); {len(fact)} fire on KPI or date facts; "
                f"{len(multi)} multi-step; always with a person: "
                f"{', '.join(['accounts flagged ' + f for f in flags] + ['playbook ' + e for e in effects]) or 'only what the platform reserves'}")
    playbooks_ok = run('playbooks', playbooks)

    basis = None
    try:
        basis = reg.capability(v, 'money_basis')
    except Exception:
        pass

    def economics():
        from roi import settings
        if basis == 'exposure':
            econ = settings.exposure_economics(v)
            return f"exposure economics: {len(econ.get('failure_events') or {})} failure events, fields read from the account: " \
                   f"{', '.join(sorted((econ.get('account_fields') or {}).values()))}"
        if not reg.capability(v, 'revenue_economics'):
            return ('warn', 'the vertical declares revenue_economics=false and money_basis=revenue: it has no money view at all')
        settings.economics(v)
        return 'revenue economics load'
    if basis:
        run('economics', economics)

    def investment():
        from roi import settings
        inv = settings.investment(v)
        priced = set(inv['playbooks'])
        missing = sorted(set(playbook_ids) - priced)
        extra = sorted(priced - set(playbook_ids))
        if not playbooks_ok:
            return ('warn', f'{len(priced)} playbooks priced; not compared with the playbook file because it did not load')
        if extra:
            raise ValueError(f'the investment file prices playbooks the playbook file does not define: {", ".join(extra)}')
        if missing:
            return ('warn', f'playbooks with no cost entry (their cost shows as unknown): {", ".join(missing)}')
        return f'{len(priced)} playbooks priced'
    run('investment', investment)

    def questions():
        from ask_ai import settings
        path = settings.QUESTIONS_DIR / f'{v}.json'
        settings.curated_questions.cache_clear()
        sets = settings.curated_questions(v)
        if not path.exists():
            return 'no question file: the platform question set is used'
        return f"own questions for: {', '.join(sorted(_read_roles(path)))}; {sum(len(q) for q in sets.values())} questions in all"
    run('ask_ai_questions', questions)

    return {'vertical': v, 'ok': not any(c['status'] == 'fail' for c in checks), 'checks': checks}


def _read_roles(path) -> list:
    import json
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return [k for k, val in data.items() if isinstance(val, list)] or list((data.get('roles') or {}).keys())


def render(report: dict) -> str:
    mark = {'pass': 'PASS', 'fail': 'FAIL', 'warn': 'WARN'}
    lines = [f"Vertical {report['vertical']}: {'OK' if report['ok'] else 'NOT OK'}"]
    lines += [f"  [{mark[c['status']]}] {c['id']:<18} {c['detail']}" for c in report['checks']]
    return '\n'.join(lines)
