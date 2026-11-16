"""
Pre-flight data report — what a tenant's staged files would do, before anything is loaded.

    from utils.preflight import preflight
    preflight(customer_id)            # reads staging + the vertical's config; writes nothing

An FDE runs this after upload_csv and before process_data. It answers, in one place:

  files            what is staged, how many rows, which columns the platform reads and which
                   fold into `attributes`
  accounts         how many are new, how many already exist
  unmatched rows   rows in every account-keyed file that name an account the platform cannot
                   resolve — these are DROPPED at load, so they are listed, not just counted
  kpis             codes the vertical's catalog does not define, rows with no value or no date,
                   core KPIs with no reading, months covered
  vertical fields  the account fields the vertical's own config reads (playbook date triggers,
                   the exposure economics file) and how many accounts carry each
  playbook facts   fact-trigger playbooks whose KPI has no reading at all (they can never fire)

The same `unmatched_rows` function runs inside the real load (utils.csv_ingest) so a run that
drops rows says so in its result, its steps and the log — it does not just load fewer rows.

Nothing here names a vertical: the fields and KPI codes come from the vertical's config.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from utils.csv_ingest import ACCOUNT_FILES, cell, num, parse_rows, staged_files, when

# every staged file whose rows are attached to an account (the loaders skip a row they cannot resolve)
ACCOUNT_KEYED_FILES = (
    'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'stakeholders.csv', 'outcomes.csv',
    'decisions.csv', 'engagement_events.csv', 'account_business_profiles.csv',
)
EXAMPLES = 20       # how many distinct offending values a report lists before it says "and N more"


def _examples(values) -> dict:
    distinct = sorted({str(v) for v in values})
    return {'distinct': len(distinct), 'examples': distinct[:EXAMPLES], 'more': max(0, len(distinct) - EXAMPLES)}


def _row_ref(row: dict) -> str:
    return cell(row, 'account_name', 'account') or cell(row, 'source_account_id', 'account_id') or '(no account named)'


def unmatched_rows(rows: Dict[str, List[dict]], resolve: Callable, vertical: Optional[str] = None) -> dict:
    """Rows the load would drop or could not score, per file.

    {file: {rows, unmatched_account: {rows, distinct, examples, more}}} for every account-keyed file,
    plus for kpi_measurements.csv: unknown_kpi_code (codes the vertical's catalog does not define),
    no_date and blank_value. Only non-zero findings are returned; {} means every row is usable."""
    out: dict = {}
    catalog = None
    if vertical:
        from utils.vertical_registry import get_kpis
        catalog = set(get_kpis(vertical) or {})
    for ft in ACCOUNT_KEYED_FILES:
        file_rows = rows.get(ft)
        if not file_rows:
            continue
        found: dict = {}
        missing = [r for r in file_rows if not resolve(r)]
        if missing:
            found['unmatched_account'] = {'rows': len(missing), **_examples(_row_ref(r) for r in missing)}
        if ft == 'kpi_measurements.csv':
            no_date = [r for r in file_rows if not when(r, 'measured_at', 'date')]
            if no_date:
                found['no_date'] = {'rows': len(no_date)}
            blank = [r for r in file_rows if num(r, 'value') is None]
            if blank:
                found['blank_value'] = {'rows': len(blank)}
            no_code = [r for r in file_rows if not cell(r, 'kpi_code', 'kpi_id')]
            if no_code:
                found['no_kpi_code'] = {'rows': len(no_code)}
            if catalog:
                unknown = [r for r in file_rows if cell(r, 'kpi_code', 'kpi_id') and cell(r, 'kpi_code', 'kpi_id') not in catalog]
                if unknown:
                    found['unknown_kpi_code'] = {'rows': len(unknown), **_examples(cell(r, 'kpi_code', 'kpi_id') for r in unknown)}
        if found:
            out[ft] = {'rows': len(file_rows), **found}
    return out


def unmatched_warnings(unmatched: dict) -> List[str]:
    """One plain sentence per finding, for a run result and the log."""
    lines = []
    for ft, f in unmatched.items():
        if 'unmatched_account' in f:
            u = f['unmatched_account']
            more = f" and {u['more']} more" if u['more'] else ''
            lines.append(f"{ft}: {u['rows']} of {f['rows']} rows name an account that is not in this tenant and were NOT loaded "
                         f"({u['distinct']} distinct: {', '.join(u['examples'])}{more}).")
        if 'unknown_kpi_code' in f:
            u = f['unknown_kpi_code']
            more = f" and {u['more']} more" if u['more'] else ''
            lines.append(f"{ft}: {u['rows']} rows carry a KPI code the vertical's catalog does not define, so they are stored but "
                         f"never scored ({', '.join(u['examples'])}{more}).")
        if 'no_date' in f:
            lines.append(f"{ft}: {f['no_date']['rows']} rows have no readable date and were NOT loaded.")
        if 'no_kpi_code' in f:
            lines.append(f"{ft}: {f['no_kpi_code']['rows']} rows have no KPI code and were NOT loaded.")
        if 'blank_value' in f:
            lines.append(f"{ft}: {f['blank_value']['rows']} rows have no value; a blank is a missing reading, not zero, and is skipped.")
    return lines


def vertical_fields(vertical: str) -> Dict[str, List[str]]:
    """{account field: [what in the vertical's config reads it]} — playbook date triggers and, for an
    exposure-basis vertical, the economics file's account fields."""
    from playbooks import definitions
    from utils.vertical_registry import capability
    fields: Dict[str, List[str]] = {}
    try:
        playbooks = definitions.load_vertical(vertical)['playbooks']
    except Exception:
        playbooks = []
    for p in playbooks:
        d = (p.get('trigger') or {}).get('date')
        if d and d.get('field'):
            fields.setdefault(d['field'], []).append(f"playbook {p['id']} (date trigger)")
    if capability(vertical, 'money_basis') == 'exposure':
        from roi import settings
        try:
            econ = settings.exposure_economics(vertical)
        except Exception:
            econ = None
        if econ:
            for role, name in (econ.get('account_fields') or {}).items():
                # an account's own recorded failure cost only replaces the published figure: no account need carry it
                note = ', optional: replaces the published figure' if role == 'recorded_failure_cost' else ''
                fields.setdefault(name, []).append(f'exposure economics ({role}{note})')
            for eid, ev in (econ.get('failure_events') or {}).items():
                if ev.get('applies_when'):
                    fields.setdefault(ev['applies_when'], []).append(f'exposure economics (failure event {eid} applies when set)')
    return fields


def _file_field(row: dict, name: str):
    """A field as the load will see it on an account row: its own column, else inside the attributes JSON."""
    v = cell(row, name)
    if v:
        return v
    raw = (row.get('attributes') or '').strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get(name) not in (None, ''):
                return parsed[name]
        except (ValueError, TypeError):
            pass
    return None


class _PreflightResolver:
    """Resolves a row the way the load will once the staged accounts file has been loaded: against the
    accounts already in the tenant plus the names and source ids in the staged accounts file."""

    def __init__(self, customer_id: int, account_rows: List[dict]):
        from utils.csv_ingest import AccountResolver
        self.db = AccountResolver(customer_id, account_rows)
        self.names = {cell(r, 'account_name', 'name') for r in account_rows} - {''}
        self.ids = {cell(r, 'source_account_id', 'account_id') for r in account_rows} - {''}

    def __call__(self, row: dict) -> bool:
        if self.db(row):
            return True
        name = cell(row, 'account_name', 'account')
        if name and name in self.names:
            return True
        raw = cell(row, 'source_account_id', 'account_id')
        return bool(raw and raw in self.ids)


def preflight(customer_id: int, vertical: Optional[str] = None, rows: Optional[Dict[str, List[dict]]] = None) -> dict:
    """The pre-flight report for what is staged for this tenant. Reads only."""
    from models import Account
    from utils.account_profile import profile_field
    from utils.csv_upload import _missing_required, get_registry, known_columns
    from utils.vertical_registry import get_kpis, get_vertical_for_customer

    vertical = vertical or get_vertical_for_customer(customer_id)
    if rows is None:
        rows = {ft: parse_rows(content) for ft, content in staged_files(customer_id).items()}
    problems: List[dict] = []

    def problem(severity: str, code: str, message: str):
        problems.append({'severity': severity, 'code': code, 'message': message})

    out = {'customer_id': int(customer_id), 'vertical': vertical, 'staged_files': sorted(rows)}
    if not rows:
        problem('error', 'nothing_staged', 'No file is staged for this tenant. Upload with upload_csv first.')
        out.update({'ok': False, 'problems': problems, 'summary': ['Nothing is staged.']})
        return out

    # ── files ──
    registry = get_registry()
    files = {}
    for ft, file_rows in sorted(rows.items()):
        headers = set(file_rows[0].keys()) if file_rows else set()
        known = known_columns(ft) if ft in registry else set()
        schema = registry.get(ft)
        missing = _missing_required(schema, headers) if schema is not None and file_rows else []
        files[ft] = {'rows': len(file_rows), 'columns_read': sorted(h for h in headers if h in known),
                     'columns_kept_as_attributes': sorted(h for h in headers if h and h not in known and h != 'attributes'),
                     'missing_required_columns': missing}
        if not file_rows:
            problem('warning', 'empty_file', f'{ft} is staged but has no rows.')
        if missing:
            problem('error', 'missing_required_columns', f'{ft} is missing required columns: {", ".join(missing)}.')
    out['files'] = files

    # ── accounts ──
    account_file = next((f for f in ACCOUNT_FILES if f in rows), None)
    account_rows = rows.get(account_file, []) if account_file else []
    existing = Account.query.filter_by(customer_id=customer_id).all()
    existing_names = {a.account_name for a in existing}
    names = [cell(r, 'account_name', 'name') for r in account_rows]
    named = [n for n in names if n]
    dupes = sorted({n for n in named if named.count(n) > 1})
    out['accounts'] = {'file': account_file, 'rows': len(account_rows), 'already_in_tenant': len(existing),
                       'new': len({n for n in named if n not in existing_names}),
                       'already_loaded': len({n for n in named if n in existing_names}),
                       'rows_without_a_name': len(names) - len(named), 'duplicate_names': dupes}
    if not account_rows and not existing:
        problem('error', 'no_accounts', 'No accounts file is staged and the tenant has no accounts, so no other row can be attached.')
    if len(names) - len(named):
        problem('error', 'account_rows_without_name', f'{len(names) - len(named)} account rows have no account_name and will not be loaded.')
    if dupes:
        problem('warning', 'duplicate_account_names', f'Account names appear more than once in {account_file}: {", ".join(dupes[:EXAMPLES])}. Only the first row of each is used.')

    # ── unmatched rows ──
    resolve = _PreflightResolver(customer_id, account_rows)
    unmatched = unmatched_rows(rows, resolve, vertical)
    out['unmatched'] = unmatched
    for line in unmatched_warnings(unmatched):
        problem('warning' if 'no value' in line else 'error', 'rows_not_usable',
                line.replace('were NOT loaded', 'will NOT be loaded').replace('they are stored but never scored', 'they will be stored but never scored'))

    # ── kpis ──
    catalog = get_kpis(vertical) or {}
    kpi_rows = rows.get('kpi_measurements.csv', [])
    if kpi_rows:
        codes = {}
        months = set()
        accounts_with = set()
        for r in kpi_rows:
            code, d = cell(r, 'kpi_code', 'kpi_id'), when(r, 'measured_at', 'date')
            if code and d and num(r, 'value') is not None and resolve(r):
                codes.setdefault(code, set()).add(d.strftime('%Y-%m'))
                months.add(d.strftime('%Y-%m'))
                accounts_with.add(_row_ref(r))
        core = sorted(c for c, k in catalog.items() if k.get('core'))
        core_missing = [c for c in core if c not in codes]
        out['kpis'] = {'rows': len(kpi_rows), 'codes_in_file': len(codes), 'codes_in_catalog': len(catalog),
                       'months': {'first': min(months) if months else None, 'last': max(months) if months else None, 'count': len(months)},
                       'accounts_with_readings': len(accounts_with), 'core_kpis': len(core), 'core_kpis_without_a_reading': core_missing}
        if core_missing:
            problem('warning', 'core_kpis_missing', f'{len(core_missing)} of the vertical\'s {len(core)} core KPIs have no usable reading: {", ".join(core_missing)}. '
                    'The health score is computed without them.')
        ids_with = accounts_with
        without = sorted(n for r in account_rows if (n := cell(r, 'account_name', 'name'))
                         and n not in ids_with and cell(r, 'source_account_id', 'account_id') not in ids_with)
        out['kpis']['staged_accounts_without_a_reading'] = _examples(without)
        if without:
            problem('warning', 'accounts_without_kpis', f'{len(without)} staged accounts have no usable KPI reading, so they get no health score: '
                    f'{", ".join(without[:EXAMPLES])}{" and more" if len(without) > EXAMPLES else ""}.')
        # fact-trigger playbooks whose KPI has no reading can never fire
        from playbooks import definitions
        dead = []
        try:
            for p in definitions.load_vertical(vertical)['playbooks']:
                for cond in (p.get('trigger') or {}).get('kpi') or []:
                    have = len(codes.get(cond['code'], ()))
                    if have < int(cond.get('periods') or 1):
                        dead.append({'playbook_id': p['id'], 'kpi': cond['code'], 'months_needed': int(cond.get('periods') or 1), 'months_in_file': have})
        except Exception as e:                                   # a broken playbook file is validate_vertical's job to explain
            problem('warning', 'playbooks_unreadable', f'The vertical\'s playbook file could not be read: {e}')
        out['playbook_facts'] = {'kpi_triggers_without_enough_readings': dead}
        for d in dead:
            problem('warning', 'playbook_cannot_fire', f"Playbook {d['playbook_id']} watches {d['kpi']} over {d['months_needed']} month(s); "
                    f"the file has {d['months_in_file']}. Unless the tenant already holds readings, it cannot fire.")
    else:
        out['kpis'] = None
        problem('warning', 'no_kpi_file', 'No kpi_measurements.csv is staged: no health score will be computed from this upload.')

    # ── fields the vertical's own config reads ──
    fields = vertical_fields(vertical)
    file_names = {cell(r, 'account_name', 'name') for r in account_rows}
    coverage = {}
    for name, used_by in sorted(fields.items()):
        have_file = [cell(r, 'account_name', 'name') for r in account_rows if _file_field(r, name) is not None]
        have_db = [a.account_name for a in existing if a.account_name not in file_names and profile_field(a, name) is not None]
        total = len({n for n in file_names if n}) + sum(1 for a in existing if a.account_name not in file_names)
        missing = sorted(({n for n in file_names if n} - set(have_file))
                         | {a.account_name for a in existing if a.account_name not in file_names and profile_field(a, name) is None})
        optional = all(', optional' in u for u in used_by)
        coverage[name] = {'used_by': used_by, 'optional': optional, 'accounts_with_a_value': len(have_file) + len(have_db), 'accounts': total,
                          'accounts_missing': _examples(missing)}
        if optional:
            continue
        if total and not (have_file or have_db):
            problem('error', 'vertical_field_absent', f'No account carries "{name}", which is read by {"; ".join(used_by)}. '
                    f'Add it as a column in {account_file or "the accounts file"}.')
        elif missing:
            problem('warning', 'vertical_field_partial', f'{len(missing)} of {total} accounts have no "{name}" (read by {"; ".join(used_by)}).')
    out['vertical_fields'] = coverage

    errors = [p for p in problems if p['severity'] == 'error']
    warnings = [p for p in problems if p['severity'] == 'warning']
    out.update({
        'ok': not errors, 'problems': problems,
        'summary': [f"{len(rows)} file(s) staged for vertical {vertical}: " + ', '.join(f"{ft} ({files[ft]['rows']} rows)" for ft in sorted(files)) + '.',
                    f"Accounts: {out['accounts']['new']} new, {out['accounts']['already_loaded']} already loaded.",
                    f"{len(errors)} error(s), {len(warnings)} warning(s)." + ('' if errors else ' Safe to run process_data.')],
    })
    return out
