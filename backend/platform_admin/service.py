"""
What the platform console shows and does. Every function runs inside a Flask app
context and returns plain dicts — never an ORM row, never a secret.

    overview()                      totals, system status, the attention list
    attention()                     what an operator should look at, worst first
    list_tenants()                  one row per tenant
    tenant_detail(customer_id)      accounts, users, keys, runs, uploads, audit
    audit_log(...)                  the tool audit log across tenants, filterable

    suspend_tenant(op, customer_id, reason) / reactivate_tenant(op, customer_id)
    revoke_key(op, key_id)
    set_user_active(op, user_id, active)
    reset_user_password(op, user_id) -> raw setup token (shown once)

WHAT NEVER LEAVES THIS MODULE: key hashes, password hashes, setup-token hashes,
the server key, webhook secrets, signal text. Keys are shown by id and prefix
only (the same view scripts/manage_api_keys.py prints).

Every write, and every tenant-detail read, is written to the tool audit log as
platform.<action> with the operator's user id — an operator reading across
tenants is itself something a reviewer will want to see.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from app_api import settings

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}


def _cfg() -> dict:
    return settings.get('platform', 'attention')


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _audit(op, action: str, customer_id, detail: str, outcome: str = 'allowed') -> None:
    from mcp_server import audit
    audit.record('ui', f'platform.{action}', customer_id, key_kind='user', key_record=None, outcome=outcome,
                 detail=f'operator:{op.user_id} {detail}'.strip())


# ── system status ──

def worker_status() -> dict:
    """The signal worker's heartbeat, with the same staleness rule /health uses."""
    from models import WorkerHeartbeat
    from signal_engine.worker import SignalEnrichmentWorker
    hb = WorkerHeartbeat.query.get(SignalEnrichmentWorker.NAME)
    if hb is None:
        return {'known': False}
    interval = hb.poll_interval_seconds or 60
    age_s = (datetime.utcnow() - hb.last_pass_at).total_seconds()
    return {'known': True, 'last_pass_at': _iso(hb.last_pass_at), 'age_seconds': round(age_s, 1),
            'last_processed_count': hb.last_processed_count, 'last_error': (hb.last_error or None) and hb.last_error[:300],
            'consecutive_errors': hb.consecutive_errors, 'poll_interval_seconds': interval,
            'stale': age_s > max(interval * 3, 180)}


def system_status() -> dict:
    from journeys.wizard_a import GENERATOR_VERSION, stale_journey_query
    from playbooks.governance import health_counts
    return {'git_sha': os.environ.get('GIT_SHA'), 'build_time': os.environ.get('BUILD_TIME'),
            'journey_generator_version': GENERATOR_VERSION, 'stale_journeys': stale_journey_query().count(),
            'worker': worker_status(), 'interventions': health_counts(),
            'time': datetime.utcnow().isoformat() + 'Z'}


# ── per-tenant aggregates (one grouped query each, never one query per tenant) ──

def _grouped(model_col, count_col, *filters) -> dict:
    from extensions import db
    from sqlalchemy import func
    q = db.session.query(model_col, func.count(count_col))
    for f in filters:
        q = q.filter(f)
    return {k: int(n) for k, n in q.group_by(model_col).all()}


def _latest_health_by_tenant() -> dict:
    """{customer_id: {status: n}} over each account's most recent scored month."""
    from extensions import db
    from sqlalchemy import func
    from models import Account, HealthScore
    latest = (db.session.query(HealthScore.account_id, func.max(HealthScore.measurement_month).label('m'))
              .group_by(HealthScore.account_id).subquery())
    rows = (db.session.query(Account.customer_id, HealthScore.health_status, func.count(HealthScore.health_score_id))
            .join(latest, (latest.c.account_id == HealthScore.account_id) & (latest.c.m == HealthScore.measurement_month))
            .join(Account, Account.account_id == HealthScore.account_id)
            .group_by(Account.customer_id, HealthScore.health_status).all())
    out: dict = {}
    for cid, status, n in rows:
        out.setdefault(cid, {})[status or 'unscored'] = int(n)
    return out


def _last_run_by_tenant() -> dict:
    from extensions import db
    from sqlalchemy import func
    from models import ProcessRun
    latest = db.session.query(func.max(ProcessRun.id)).group_by(ProcessRun.customer_id).subquery()
    return {r.customer_id: r for r in ProcessRun.query.filter(ProcessRun.id.in_(db.session.query(latest))).all()}


def _last_activity_by_tenant() -> dict:
    """{customer_id: datetime} — the most recent upload, signal or process run."""
    from extensions import db
    from sqlalchemy import func
    from models import CsvUpload, ProcessRun, QualitativeSignal
    out: dict = {}

    def fold(rows):
        for cid, at in rows:
            if at is None:
                continue
            if not isinstance(at, datetime):                    # signal_date is a date
                at = datetime(at.year, at.month, at.day)
            if cid not in out or at > out[cid]:
                out[cid] = at

    fold(db.session.query(CsvUpload.customer_id, func.max(CsvUpload.uploaded_at)).group_by(CsvUpload.customer_id).all())
    fold(db.session.query(ProcessRun.customer_id, func.max(ProcessRun.started_at)).group_by(ProcessRun.customer_id).all())
    fold(db.session.query(QualitativeSignal.customer_id, func.max(QualitativeSignal.signal_date))
         .filter(QualitativeSignal.signal_date <= datetime.utcnow().date())     # a future-dated row is not activity
         .group_by(QualitativeSignal.customer_id).all())
    return out


def _run_view(r) -> dict:
    errors = r.errors or []
    first = None
    if errors:
        e = errors[0]
        first = (e.get('error') or e.get('message') or str(e)) if isinstance(e, dict) else str(e)
    return {'run_id': r.run_id, 'status': r.status, 'mode': r.mode, 'started_at': _iso(r.started_at),
            'finished_at': _iso(r.finished_at), 'error_count': len(errors), 'first_error': (first or None) and first[:300],
            'counts': r.counts or {}, 'generator_version': r.generator_version, 'key_kind': r.key_kind}


def _tenant_status(c, now) -> str:
    if c.suspended_at:
        return 'suspended'
    if c.expires_at and c.expires_at < now:
        return 'expired'
    return 'active'


def list_tenants() -> list:
    from models import Account, Customer, CustomerApiKey, User
    now = datetime.utcnow()
    accounts = _grouped(Account.customer_id, Account.account_id)
    users = _grouped(User.customer_id, User.user_id, User.active.is_(True))
    keys = _grouped(CustomerApiKey.customer_id, CustomerApiKey.id, CustomerApiKey.is_active.is_(True))
    health, runs, activity = _latest_health_by_tenant(), _last_run_by_tenant(), _last_activity_by_tenant()
    out = []
    for c in Customer.query.order_by(Customer.customer_id).all():
        run = runs.get(c.customer_id)
        out.append({
            'customer_id': c.customer_id, 'name': c.customer_name, 'vertical': c.vertical, 'data_origin': c.data_origin,
            'status': _tenant_status(c, now), 'suspended_at': _iso(c.suspended_at), 'suspended_reason': c.suspended_reason,
            'expires_at': _iso(c.expires_at), 'created_at': _iso(c.created_at),
            'accounts': accounts.get(c.customer_id, 0), 'active_users': users.get(c.customer_id, 0),
            'active_keys': keys.get(c.customer_id, 0), 'health': health.get(c.customer_id, {}),
            'last_activity_at': _iso(activity.get(c.customer_id)),
            'last_run': {'status': run.status, 'started_at': _iso(run.started_at)} if run else None,
        })
    return out


# ── the attention list ──

def attention() -> list:
    """What needs an operator, worst first. Each item:
    {severity, kind, title, detail, customer_id, customer_name, since}.
    Every rule is a fact read from the database, with its threshold in
    config/app_api.json platform.attention — nothing here is a guess."""
    from extensions import db
    from sqlalchemy import func
    from models import Account, Customer, CustomerApiKey, JourneyData, ProcessRun, ToolAuditLog, User
    from journeys.wizard_a import stale_journey_query
    cfg, now = _cfg(), datetime.utcnow()
    names = {c.customer_id: c for c in Customer.query.all()}
    items: list = []

    def add(severity, kind, title, detail=None, cid=None, since=None):
        c = names.get(cid)
        items.append({'severity': severity, 'kind': kind, 'title': title, 'detail': detail, 'customer_id': cid,
                      'customer_name': c.customer_name if c else None, 'since': _iso(since)})

    # the worker
    w = worker_status()
    if not w['known']:
        add('warning', 'worker_unknown', 'The signal worker has never reported in',
            'No heartbeat row exists. Signals will not be enriched until the worker container runs.')
    elif w['stale']:
        add('critical', 'worker_stale', 'The signal worker has stopped reporting',
            f"Last pass {int(w['age_seconds'] // 60)} minutes ago (expected every {w['poll_interval_seconds']}s).")
    elif w['consecutive_errors']:
        add('warning', 'worker_errors', f"The signal worker has failed {w['consecutive_errors']} passes in a row", w['last_error'])

    # stale journeys (behind the generator version; a deploy normally rebuilds them)
    stale = stale_journey_query().count()
    if stale:
        add('warning', 'stale_journeys', f'{stale} journeys are behind the current generator version',
            'They are rebuilt on deploy; if this persists the rebuild step failed.')

    # journeys whose last rebuild failed (stamped by journeys.wizard_a)
    failed_builds = (db.session.query(JourneyData.customer_id, func.count(JourneyData.id))
                     .filter(JourneyData.journey_json['build_error'].as_string().isnot(None))
                     .group_by(JourneyData.customer_id).all())
    for cid, n in failed_builds:
        add('critical', 'journey_build_failed', f'{n} account journey{"s" if n != 1 else ""} failed to rebuild',
            'The previous build is still being served and may be out of date.', cid)

    # each tenant's most recent processing run
    stuck_before = now - timedelta(minutes=int(cfg['process_run_stuck_minutes']))
    for cid, r in _last_run_by_tenant().items():
        if r.status == 'failed':
            add('critical', 'process_run_failed', 'The last data processing run failed', _run_view(r)['first_error'], cid, r.started_at)
        elif r.status == 'partial':
            add('warning', 'process_run_partial', 'The last data processing run finished with errors',
                _run_view(r)['first_error'], cid, r.started_at)
        elif r.status == 'running' and r.started_at and r.started_at < stuck_before:
            add('warning', 'process_run_stuck', 'A data processing run has not finished',
                f"Started {r.started_at.isoformat()} and still marked running.", cid, r.started_at)

    # data freshness
    activity = _last_activity_by_tenant()
    account_counts = _grouped(Account.customer_id, Account.account_id)
    stale_before = now - timedelta(days=int(cfg['stale_data_days']))
    for cid, c in names.items():
        if c.suspended_at:
            add('info', 'tenant_suspended', 'Tenant is suspended', c.suspended_reason, cid, c.suspended_at)
            continue
        last = activity.get(cid)
        if last is None:
            if account_counts.get(cid):
                add('info', 'no_data', 'Has accounts but no data has ever been loaded', None, cid)
        elif last < stale_before:
            # a demo tenant on synthetic data is not expected to receive anything new — worth listing, not a warning
            add('info' if (c.data_origin or '').startswith('synthetic') else 'warning', 'stale_data',
                f"No new data for {(now - last).days} days",
                f"Last upload, signal or run: {last.date().isoformat()}.", cid, last)

    # tenants reaching their end date
    warn_until = now + timedelta(days=int(cfg['tenant_expiry_warning_days']))
    for cid, c in names.items():
        if c.expires_at and c.expires_at < now:
            add('warning', 'tenant_expired', 'Tenant is past its end date',
                'Nothing removes an expired tenant automatically.', cid, c.expires_at)
        elif c.expires_at and c.expires_at < warn_until:
            add('info', 'tenant_expiring', f"Tenant reaches its end date on {c.expires_at.date().isoformat()}", None, cid, c.expires_at)

    # keys about to lapse
    key_warn = now + timedelta(days=int(cfg['key_expiry_warning_days']))
    for k in CustomerApiKey.query.filter(CustomerApiKey.is_active.is_(True), CustomerApiKey.expires_at.isnot(None),
                                         CustomerApiKey.expires_at > now, CustomerApiKey.expires_at < key_warn).all():
        add('warning', 'key_expiring', f"API key {k.name!r} expires on {k.expires_at.date().isoformat()}",
            f'Key id {k.id}. Integrations using it will start failing.', k.customer_id, k.expires_at)

    # refused calls
    since = now - timedelta(hours=int(cfg['denied_calls_window_hours']))
    denied = (db.session.query(ToolAuditLog.customer_id, func.count(ToolAuditLog.id))
              .filter(ToolAuditLog.outcome == 'denied', ToolAuditLog.at >= since).group_by(ToolAuditLog.customer_id).all())
    for cid, n in denied:
        if n >= int(cfg['denied_calls_threshold']):
            add('warning', 'denied_calls', f"{n} refused calls in the last {cfg['denied_calls_window_hours']} hours",
                'Someone or something is calling with a key that is wrong, revoked or out of scope. See the audit log.'
                if cid else 'These calls named no tenant (a missing or unknown key). See the audit log.', cid)

    # users nobody scoped (they can log in but reach nothing)
    # (counted here, not in SQL: an unset JSON column is stored as SQL NULL by some writers and JSON null by others)
    unscoped: dict = {}
    for cid, allowed in (db.session.query(User.customer_id, User.allowed_customer_ids)
                         .filter(User.active.is_(True), User.platform_role.is_(None)).all()):
        if not allowed:
            unscoped[cid] = unscoped.get(cid, 0) + 1
    for cid, n in unscoped.items():
        add('warning', 'unscoped_users', f'{n} active user{"s" if n != 1 else ""} with no tenant scope',
            'They can log in but will be refused everywhere. Usually a provisioning mistake.', cid)

    items.sort(key=lambda i: (SEVERITY_ORDER[i['severity']], i['customer_id'] or 0))
    return items


def overview() -> dict:
    from extensions import db
    from sqlalchemy import func
    from models import Account, Customer, CustomerApiKey, LLMUsageLog, QualitativeSignal, ToolAuditLog, User
    now = datetime.utcnow()
    tenants = Customer.query.all()
    status_counts: dict = {}
    for c in tenants:
        s = _tenant_status(c, now)
        status_counts[s] = status_counts.get(s, 0) + 1
    by_vertical = dict(db.session.query(func.coalesce(Customer.vertical, 'unset'), func.count(Customer.customer_id))
                       .group_by(func.coalesce(Customer.vertical, 'unset')).all())
    by_origin = dict(db.session.query(func.coalesce(Customer.data_origin, 'customer data'), func.count(Customer.customer_id))
                     .group_by(func.coalesce(Customer.data_origin, 'customer data')).all())
    health_total: dict = {}
    for per in _latest_health_by_tenant().values():
        for status, n in per.items():
            health_total[status] = health_total.get(status, 0) + n
    day = now - timedelta(hours=24)
    month = now - timedelta(days=30)
    calls = dict(db.session.query(ToolAuditLog.outcome, func.count(ToolAuditLog.id)).filter(ToolAuditLog.at >= day)
                 .group_by(ToolAuditLog.outcome).all())
    llm_cost = db.session.query(func.coalesce(func.sum(LLMUsageLog.cost_estimate_usd), 0)).filter(LLMUsageLog.created_at >= month).scalar()
    items = attention()
    return {
        'totals': {
            'tenants': len(tenants), 'tenants_by_status': status_counts, 'tenants_by_vertical': {k: int(v) for k, v in by_vertical.items()},
            'tenants_by_data_origin': {k: int(v) for k, v in by_origin.items()},
            'accounts': Account.query.count(), 'active_users': User.query.filter(User.active.is_(True)).count(),
            'active_keys': CustomerApiKey.query.filter(CustomerApiKey.is_active.is_(True)).count(),
            'signals': QualitativeSignal.query.count(), 'account_health': health_total,
            'calls_24h': {'allowed': int(calls.get('allowed', 0)), 'denied': int(calls.get('denied', 0))},
            'llm_cost_30d_usd': round(float(llm_cost or 0), 2),
        },
        'system': system_status(),
        'attention': items,
        'attention_counts': {s: sum(1 for i in items if i['severity'] == s) for s in SEVERITY_ORDER},
    }


# ── one tenant ──

def _user_view(u) -> dict:
    return {'user_id': u.user_id, 'name': u.user_name, 'email': u.email, 'role': u.role, 'active': bool(u.active),
            'has_password': bool(u.password_hash), 'last_login': _iso(u.last_login), 'scoped': bool(u.allowed_customer_ids),
            'platform_role': u.platform_role}


def _key_view(k) -> dict:
    return {'id': k.id, 'name': k.name, 'key_prefix': k.key_prefix, 'scopes': k.scopes or [], 'is_active': bool(k.is_active),
            'created_at': _iso(k.created_at), 'last_used_at': _iso(k.last_used_at), 'expires_at': _iso(k.expires_at),
            'account_restricted': k.allowed_account_ids is not None}


def tenant_detail(op, customer_id: int) -> Optional[dict]:
    from extensions import db
    from sqlalchemy import func
    from models import (Account, CsvUpload, Customer, CustomerApiKey, HealthScore, Intervention, JourneyData, LLMUsageLog,
                        ProcessRun, QualitativeSignal, User)
    from mcp_server import audit
    cid = int(customer_id)
    c = db.session.get(Customer, cid)
    if not c:
        return None
    now = datetime.utcnow()
    latest = (db.session.query(HealthScore.account_id, func.max(HealthScore.measurement_month).label('m'))
              .group_by(HealthScore.account_id).subquery())
    health = {h.account_id: h for h in HealthScore.query.join(
        latest, (latest.c.account_id == HealthScore.account_id) & (latest.c.m == HealthScore.measurement_month)).all()}
    journeys = {j.account_id: j for j in JourneyData.query.filter_by(customer_id=cid).all()}
    accounts = []
    for a in Account.query.filter_by(customer_id=cid).order_by(Account.account_name).all():
        h, j = health.get(a.account_id), journeys.get(a.account_id)
        accounts.append({
            'account_id': a.account_id, 'name': a.account_name, 'status': a.account_status,
            'revenue': float(a.revenue) if a.revenue is not None else None,
            'health_score': float(h.health_score) if h and h.health_score is not None else None,
            'health_status': h.health_status if h else None, 'health_month': _iso(h.measurement_month) if h else None,
            'journey': 'failed' if (j and (j.journey_json or {}).get('build_error')) else 'built' if j else 'none',
            'journey_version': j.generator_version if j else None,
        })
    month = now - timedelta(days=30)
    llm = db.session.query(func.coalesce(func.sum(LLMUsageLog.cost_estimate_usd), 0), func.count(LLMUsageLog.id)) \
        .filter(LLMUsageLog.customer_id == cid, LLMUsageLog.created_at >= month).one()
    interventions = dict(db.session.query(Intervention.state, func.count(Intervention.id))
                         .filter(Intervention.customer_id == cid).group_by(Intervention.state).all())
    out = {
        'tenant': {'customer_id': c.customer_id, 'name': c.customer_name, 'vertical': c.vertical, 'data_origin': c.data_origin,
                   'domain': c.domain, 'status': _tenant_status(c, now), 'suspended_at': _iso(c.suspended_at),
                   'suspended_reason': c.suspended_reason, 'expires_at': _iso(c.expires_at), 'created_at': _iso(c.created_at)},
        'accounts': accounts,
        'users': [_user_view(u) for u in User.query.filter_by(customer_id=cid).order_by(User.user_id).all()],
        'keys': [_key_view(k) for k in CustomerApiKey.query.filter_by(customer_id=cid).order_by(CustomerApiKey.id.desc()).all()],
        'runs': [_run_view(r) for r in ProcessRun.query.filter_by(customer_id=cid).order_by(ProcessRun.id.desc()).limit(10).all()],
        'uploads': [{'id': u.id, 'file_type': u.file_type, 'row_count': u.row_count, 'uploaded_at': _iso(u.uploaded_at),
                     'consumed': u.consumed_at is not None,
                     'warnings': len((u.validation or {}).get('warnings', []) if isinstance(u.validation, dict) else (u.validation or []))}
                    for u in CsvUpload.query.filter_by(customer_id=cid).order_by(CsvUpload.id.desc()).limit(10).all()],
        'signals': QualitativeSignal.query.filter_by(customer_id=cid).count(),
        'interventions': {k: int(v) for k, v in interventions.items()},
        'llm_30d': {'cost_usd': round(float(llm[0] or 0), 2), 'calls': int(llm[1])},
        'attention': [i for i in attention() if i['customer_id'] == cid],
        'audit': audit.query(customer_id=cid, limit=25),
    }
    _audit(op, 'tenant_view', cid, 'viewed tenant detail')
    return out


# ── audit ──

def audit_log(customer_id=None, outcome: Optional[str] = None, tool: Optional[str] = None, before_id=None, limit=None) -> dict:
    from models import Customer, ToolAuditLog
    page = int(settings.get('platform', 'audit_page_size'))
    cap = int(settings.get('platform', 'audit_max_page_size'))
    try:
        limit = max(1, min(int(limit), cap)) if limit else page
    except (TypeError, ValueError):
        limit = page
    q = ToolAuditLog.query
    if customer_id not in (None, ''):
        q = q.filter(ToolAuditLog.customer_id == int(customer_id))
    if outcome in ('allowed', 'denied'):
        q = q.filter(ToolAuditLog.outcome == outcome)
    if tool:
        q = q.filter(ToolAuditLog.tool.ilike(f'%{tool.strip()[:80]}%'))
    if before_id not in (None, ''):
        q = q.filter(ToolAuditLog.id < int(before_id))
    rows = q.order_by(ToolAuditLog.id.desc()).limit(limit + 1).all()
    more, rows = len(rows) > limit, rows[:limit]
    names = {c.customer_id: c.customer_name for c in Customer.query.all()}
    return {'entries': [{'id': r.id, 'at': _iso(r.at), 'surface': r.surface, 'tool': r.tool, 'customer_id': r.customer_id,
                         'customer_name': names.get(r.customer_id), 'key_kind': r.key_kind, 'key_id': r.key_id,
                         'key_prefix': r.key_prefix, 'caller_ip': r.caller_ip, 'outcome': r.outcome, 'detail': r.detail}
                        for r in rows],
            'next_before_id': rows[-1].id if (more and rows) else None}


# ── actions (operator only; every one audited) ──

def suspend_tenant(op, customer_id: int, reason: Optional[str]) -> dict:
    from extensions import db
    from models import Customer
    c = db.session.get(Customer, int(customer_id))
    if not c:
        raise LookupError(f'tenant {customer_id} not found')
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('a reason is required to suspend a tenant')
    if op.customer_id is not None and int(op.customer_id) == c.customer_id:
        raise ValueError('you cannot suspend the tenant your own login belongs to')
    if not c.suspended_at:
        c.suspended_at = datetime.utcnow()
        c.suspended_reason = reason[:255]
        db.session.commit()
        _audit(op, 'tenant_suspend', c.customer_id, f'suspended: {reason[:150]}')
    return {'customer_id': c.customer_id, 'status': 'suspended', 'suspended_at': _iso(c.suspended_at), 'suspended_reason': c.suspended_reason}


def reactivate_tenant(op, customer_id: int) -> dict:
    from extensions import db
    from models import Customer
    c = db.session.get(Customer, int(customer_id))
    if not c:
        raise LookupError(f'tenant {customer_id} not found')
    if c.suspended_at:
        c.suspended_at = None
        c.suspended_reason = None
        db.session.commit()
        _audit(op, 'tenant_reactivate', c.customer_id, 'reactivated')
    return {'customer_id': c.customer_id, 'status': _tenant_status(c, datetime.utcnow())}


def revoke_key(op, key_id: int) -> dict:
    """Revocation only. A replacement key is issued on the box
    (scripts/manage_api_keys.py rotate) so a raw key never crosses the browser."""
    from extensions import db
    from models import CustomerApiKey
    import api_key_service as svc
    k = db.session.get(CustomerApiKey, int(key_id))
    if not k:
        raise LookupError(f'key {key_id} not found')
    was_active = bool(k.is_active)
    svc.revoke_api_key(k.id)
    if was_active:
        _audit(op, 'key_revoke', k.customer_id, f'revoked key id={k.id} prefix={k.key_prefix}')
    return _key_view(db.session.get(CustomerApiKey, int(key_id)))


def _target_user(op, user_id: int):
    from extensions import db
    from models import User
    u = db.session.get(User, int(user_id))
    if not u:
        raise LookupError(f'user {user_id} not found')
    if u.platform_role and u.user_id != op.user_id:
        # operators are managed on the box, by someone with shell access — not by each other from a browser
        raise PermissionError('platform operators are managed with scripts/manage_platform_operators.py, not from the console')
    return u


def set_user_active(op, user_id: int, active: bool) -> dict:
    from extensions import db
    u = _target_user(op, user_id)
    if u.user_id == op.user_id and not active:
        raise ValueError('you cannot deactivate your own login')
    if bool(u.active) != bool(active):
        u.active = bool(active)
        db.session.commit()
        _audit(op, 'user_activate' if active else 'user_deactivate', u.customer_id, f'user {u.user_id}')
    return _user_view(u)


def reset_user_password(op, user_id: int) -> str:
    from extensions import db
    from app_api.auth import issue_setup_token
    u = _target_user(op, user_id)
    raw = issue_setup_token(u)
    db.session.commit()
    _audit(op, 'user_reset_password', u.customer_id, f'user {u.user_id}: one-time setup token issued')
    return raw
