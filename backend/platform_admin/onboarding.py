"""
Tenant onboarding from the platform console. The same steps scripts/onboard_tenant.py
drives over MCP — create the tenant, stage its files, read the pre-flight report, run the
pipeline — for an operator signed in to the console.

    options(op)                               what the form offers: verticals, tiers, data origins, files
    create_tenant(op, data)                   the tenant, its admin and a key; token and key shown once
    stage_file(op, customer_id, ...)          validate a file (dry_run) or stage it
    status(op, customer_id)                   where this tenant is in the flow
    preflight_report(op, customer_id)         what the staged files would do; reads only
    process(op, customer_id, mode)            run the pipeline on what is staged
    retention_settings(op, customer_id)       the retention figure's settings, with defaults, and its latest result
    save_retention_settings(op, cid, data)    store the tenant's overrides; optionally recompute hindsight

Nothing here calls a model: process_data's stages are ingest, scoring and the journey,
hindsight and foresight wizards.

Every function is audited as platform.onboard_<step>. The audit row never carries the
setup token, the key or any file content. Choices and limits live in config/app_api.json
platform.onboarding; verticals, tiers, data origins and file columns are read from the
same registries the MCP tools use, so a new vertical needs no change here.
"""
from __future__ import annotations

import re
from typing import Optional

from app_api import settings
from platform_admin.service import _audit, _iso, _run_view, _tenant_status

_DOMAIN = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$')
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _cfg() -> dict:
    return settings.get('platform', 'onboarding')


def _tool_errors(fn):
    """The shared impls raise the MCP layer's ToolError; here it is a bad request."""
    from fastmcp.exceptions import ToolError
    try:
        return fn()
    except ToolError as e:
        raise ValueError(str(e))


def _tenant(customer_id: int):
    from extensions import db
    from models import Customer
    c = db.session.get(Customer, int(customer_id))
    if c is None:
        raise LookupError('no such tenant')
    return c


def _expected_files() -> list:
    return list(_cfg().get('files') or [])


def _file_type(file_type) -> str:
    """The canonical name of a file the onboarding flow accepts."""
    from utils.csv_upload import resolve_file_type
    try:
        canonical = resolve_file_type(str(file_type or '')).canonical_filename
    except Exception:
        canonical = None
    allowed = [f['file_type'] for f in _expected_files()]
    if canonical not in allowed:
        raise ValueError(f"file_type must be one of: {', '.join(allowed)}")
    return canonical


# ── what the form offers ──

def _tiers(vertical: str) -> dict:
    from mcp_server.cs_pulse_onboarding import _load_tier_config
    from utils.vertical_registry import capability
    tiers_file = capability(vertical, 'kpi_tiers_file')
    config = _load_tier_config(tiers_file) if tiers_file else None
    if not config:
        return {'default': None, 'choices': []}
    return {'default': config.get('default_tier'),
            'choices': [{'id': tid, 'name': t.get('display_name') or tid, 'description': t.get('description'),
                         'kpi_count': t.get('kpi_count'), 'prerequisites': t.get('prerequisites')}
                        for tid, t in (config.get('tiers') or {}).items()]}


def _verticals() -> list:
    from utils.vertical_registry import SUPPORTED_VERTICALS, capability, get_vertical_description
    out = []
    for v in sorted(SUPPORTED_VERTICALS):
        out.append({'id': v, 'description': get_vertical_description(v), 'money_basis': capability(v, 'money_basis'),
                    'tiers': _tiers(v)})
    return out


def _files() -> list:
    from utils.csv_upload import get_registry
    registry = get_registry()
    out = []
    for f in _expected_files():
        info = registry.get(f['file_type'])
        out.append({'file_type': f['file_type'], 'label': f.get('label') or f['file_type'], 'expected': bool(f.get('expected')),
                    'what': f.get('what'), 'required_columns': list(info.required_columns) if info else [],
                    'optional_columns': list(info.optional_columns) if info else []})
    return out


def options(op) -> dict:
    from utils.data_origin import origins
    cfg = _cfg()
    return {'verticals': _verticals(),
            'data_origins': [{'id': k, 'label': v.get('label') or k, 'synthetic': bool(v.get('synthetic')),
                              'disclosure': v.get('disclosure')} for k, v in origins().items()],
            'default_data_origin': cfg.get('default_data_origin'),
            'files': _files(), 'max_csv_bytes': cfg.get('max_csv_bytes'), 'modes': list(cfg.get('modes') or []),
            'can_write': op.platform_role in settings.get('platform', 'write_roles')}


# ── step 1: the tenant ──

def create_tenant(op, data: dict) -> dict:
    from mcp_server.cs_pulse_onboarding import _create_customer_impl
    from utils.vertical_registry import SUPPORTED_VERTICALS, normalize_vertical

    def field(name: str) -> str:
        return str((data or {}).get(name) or '').strip()

    name, domain, admin_name = field('name'), field('domain').lower(), field('admin_name')
    admin_email, vertical, tier = field('admin_email').lower(), field('vertical'), field('tier') or None
    data_origin = field('data_origin') or _cfg().get('default_data_origin')
    missing = [k for k, v in (('name', name), ('domain', domain), ('admin_name', admin_name),
                              ('admin_email', admin_email), ('vertical', vertical)) if not v]
    if missing:
        raise ValueError(f"required: {', '.join(missing)}")
    if not _DOMAIN.match(domain):
        raise ValueError('domain must look like example.com')
    if not _EMAIL.match(admin_email):
        raise ValueError('admin_email is not an email address')
    vertical = normalize_vertical(vertical)
    if vertical not in SUPPORTED_VERTICALS:
        raise ValueError(f"vertical must be one of: {', '.join(sorted(SUPPORTED_VERTICALS))}")
    tiers = _tiers(vertical)
    if tier and tier not in [t['id'] for t in tiers['choices']]:
        raise ValueError('tier is not offered for this vertical')

    res = _tool_errors(lambda: _create_customer_impl(name, domain, vertical, admin_email, admin_name, data_origin, tier,
                                                     audit_surface=None))
    _audit(op, 'onboard_create', res['customer_id'],
           f"domain={domain} vertical={vertical} data_origin={res['data_origin']} tier={(res.get('tier') or {}).get('tier') or tier or '-'}")
    return {'customer_id': res['customer_id'], 'name': res['customer_name'], 'domain': res['domain'],
            'vertical': res['vertical'], 'data_origin': res['data_origin'], 'disclosure': res.get('disclosure'),
            'tier': res.get('tier'), 'admin_user_id': res['admin_user_id'], 'admin_email': res['admin_email'],
            'admin_setup_token': res['admin_setup_token'],
            'setup_token_expiry_minutes': settings.get('password_setup', 'expiry_minutes'),
            'api_key': res.get('api_key')}


# ── step 2: the files ──

def stage_file(op, customer_id: int, file_type, csv_content, dry_run: bool) -> dict:
    from utils.csv_upload import _upload_csv_impl
    c = _tenant(customer_id)
    canonical = _file_type(file_type)
    if not isinstance(csv_content, str) or not csv_content.strip():
        raise ValueError('csv_content is empty')
    limit = _cfg().get('max_csv_bytes')
    size = len(csv_content.encode('utf-8'))
    if limit and size > limit:
        raise ValueError(f'the file is {size} bytes; the console accepts up to {limit}. Load larger files with scripts/onboard_tenant.py')
    result = _upload_csv_impl(customer_id=c.customer_id, file_type=canonical, csv_content=csv_content,
                              dry_run=bool(dry_run), key_kind='user', key_id=None)
    out = result.to_dict()
    out['file_type'], out['dry_run'] = canonical, bool(dry_run)
    out['staged'] = result.status == 'success' and not dry_run
    _audit(op, 'onboard_validate' if dry_run else 'onboard_upload', c.customer_id,
           f'file={canonical} status={result.status} rows={result.row_count} bytes={size}')
    return out


# ── where the tenant is ──

def _staged(customer_id: int) -> dict:
    from models import CsvUploadStaging
    return {s.file_type: s for s in CsvUploadStaging.query.filter_by(customer_id=customer_id).all()}


def status(op, customer_id: int) -> dict:
    from datetime import datetime
    from models import Account, ProcessRun, User
    c = _tenant(customer_id)
    cid = c.customer_id
    staged = _staged(cid)
    files = []
    for f in _expected_files():
        s = staged.get(f['file_type'])
        files.append({'file_type': f['file_type'], 'label': f.get('label') or f['file_type'], 'expected': bool(f.get('expected')),
                      'staged': s is not None, 'row_count': s.row_count if s else None,
                      'staged_at': _iso(s.updated_at or s.uploaded_at) if s else None})
    run = ProcessRun.query.filter_by(customer_id=cid).order_by(ProcessRun.id.desc()).first()
    accounts = Account.query.filter_by(customer_id=cid).count()
    admins = User.query.filter_by(customer_id=cid, role='admin').order_by(User.user_id).all()
    missing = [f['file_type'] for f in files if f['expected'] and not f['staged']]
    loaded = accounts > 0 and run is not None and run.status in ('success', 'partial')
    # A staged file newer than the last run has not been loaded yet.
    pending = [f['file_type'] for f in files if f['staged'] and (
        run is None or run.status == 'failed' or (staged[f['file_type']].updated_at or staged[f['file_type']].uploaded_at) is None
        or (run.finished_at or run.started_at) < (staged[f['file_type']].updated_at or staged[f['file_type']].uploaded_at))]
    if run is not None and run.status == 'running':
        step = 'processing'
    elif loaded and not pending:
        step = 'done'
    elif missing and not loaded:
        step = 'upload'
    else:
        step = 'check'
    _audit(op, 'onboard_status', cid, f'step={step}')
    return {'tenant': {'customer_id': cid, 'name': c.customer_name, 'domain': c.domain, 'vertical': c.vertical,
                       'data_origin': c.data_origin, 'status': _tenant_status(c, datetime.utcnow()),
                       'created_at': _iso(c.created_at)},
            'admins': [{'user_id': u.user_id, 'email': u.email, 'name': u.user_name, 'password_set': bool(u.password_hash)}
                       for u in admins],
            'files': files, 'missing_expected': missing, 'pending_files': pending, 'accounts': accounts,
            'last_run': _run_view(run) if run else None, 'retention': _retention_result(cid), 'step': step}


# ── step 3: the check ──

def preflight_report(op, customer_id: int) -> dict:
    from utils.preflight import preflight
    c = _tenant(customer_id)
    report = preflight(c.customer_id)
    limit = _cfg().get('max_problems')
    problems = report.get('problems') or []
    report['problem_count'] = len(problems)
    report['error_count'] = sum(1 for p in problems if p.get('severity') == 'error')
    if limit and len(problems) > limit:
        report['problems'] = problems[:limit]
    _audit(op, 'onboard_preflight', c.customer_id, f"ok={report.get('ok')} problems={len(problems)}")
    return report


# ── step 4: the run ──

def process(op, customer_id: int, mode: Optional[str] = None) -> dict:
    from mcp_server.cs_pulse_onboarding import _process_data_impl
    from models import ProcessRun
    c = _tenant(customer_id)
    modes = list(_cfg().get('modes') or [])
    mode = mode or (modes[0] if modes else 'auto')
    if modes and mode not in modes:
        raise ValueError(f"mode must be one of: {', '.join(modes)}")
    if c.suspended_at is not None:
        raise ValueError('this tenant is suspended; reactivate it before loading data')
    if ProcessRun.query.filter_by(customer_id=c.customer_id, status='running').first() is not None:
        raise ValueError('a run is already in progress for this tenant')
    _audit(op, 'onboard_process', c.customer_id, f'mode={mode} started')
    res = _tool_errors(lambda: _process_data_impl(c.customer_id, mode))
    _audit(op, 'onboard_process', c.customer_id,
           f"mode={mode} status={res.get('status')} accounts={res.get('accounts')} errors={len(res.get('errors') or [])}")
    return {k: res.get(k) for k in ('status', 'mode', 'vertical', 'accounts', 'kpi_measurements', 'csv_files_processed',
                                    'steps_completed', 'errors', 'data_warnings', 'duration_s', 'message')}


# ── analysis settings: the hindsight retention figure ──

def _retention_result(customer_id: int) -> Optional[dict]:
    """The retention block of the tenant's latest hindsight run, or None when there is none."""
    from models import WizardRun
    run = (WizardRun.query.filter_by(customer_id=customer_id, wizard='b', status='completed')
           .order_by(WizardRun.created_at.desc()).first())
    block = ((run.results or {}).get('realized_nrr') if run is not None else None) or None
    if not block:
        return None
    return {'run_id': run.run_id, 'generated_at': (run.results or {}).get('generated_at'), 'basis': block.get('basis'),
            'window': block.get('window'), 'settings': block.get('settings'), 'portfolio': block.get('portfolio')}


def retention_settings(op, customer_id: int) -> dict:
    from wizards import wizard_b_settings
    c = _tenant(customer_id)
    out = wizard_b_settings.retention_view(c.customer_id)
    out['result'] = _retention_result(c.customer_id)
    _audit(op, 'onboard_settings_read', c.customer_id, f"overrides={len(out['overrides'])}")
    return out


def save_retention_settings(op, customer_id: int, data: dict) -> dict:
    """Store the tenant's overrides and, when asked, recompute hindsight so the figure reflects them."""
    from wizards import wizard_b_settings
    c = _tenant(customer_id)
    values = (data or {}).get('values')
    if values is not None and not isinstance(values, dict):
        raise ValueError('values must be an object of setting: value')
    out = wizard_b_settings.save_retention(c.customer_id, values or {})
    recomputed = None
    if (data or {}).get('recompute'):
        from wizards.wizard_b_hindsight import run_wizard_b
        res = run_wizard_b(c.customer_id)
        recomputed = res.get('status')
        out['recompute'] = {'status': res.get('status'), 'reason': res.get('reason')}
    out['result'] = _retention_result(c.customer_id)
    _audit(op, 'onboard_settings_save', c.customer_id,
           'overrides=' + (','.join(f'{k}={v}' for k, v in sorted(out['overrides'].items())) or 'none')
           + (f' recompute={recomputed}' if recomputed else ''))
    return out
