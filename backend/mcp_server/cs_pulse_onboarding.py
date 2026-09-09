#!/usr/bin/env python3
import os
import re
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional
"""
CS Pulse MCP — Onboarding Tools (frictionless auth).

Tier 2A port (2026-09-01), sub-checkpoints so far: create_customer,
upload_csv, process_data (CSV ingest half; the post-ingest stages land
one per sub-checkpoint in mcp_server/process_data_pipeline.py). Remaining
onboarding tools (trigger_wizard, etc.) are later sub-checkpoints — see
project memory for the full phase breakdown.

Two changes made relative to the old repo's create_customer, not cosmetic:

1. The verticals.provision_dc_customer.provision_customer() call is
   dropped entirely, not carried forward wrapped in its old try/except.
   It provisioned a per-customer filesystem directory
   (verticals/customerNNN-{vertical}/...) — a pattern Tier 1 already
   established doesn't exist in this build (no verticals/ directory at
   all; every vertical is DB rows + a JSON catalog). The old repo's own
   try/except already proves this call could only ever fail there too —
   porting a call that can never succeed, just to silently swallow its
   failure, is exactly the kind of dead code this rebuild is meant to
   drop rather than reproduce.

2. _check_kpi_dependencies() dropped its cust_vertical parameter — grep
   confirmed it was never referenced inside the function body in the old
   repo either, a genuinely unused parameter, not just an unused default.
   UPDATE (2026-09-07): a vertical parameter is back, for a different and
   now-real reason — config/kpi_dependencies.json was a single dc2_s-only
   file silently read for every vertical (P1-KPI1 there is "Time-to-First-
   Workload"; the same code is "Daily Active Users" on saas_premium). It's
   now config/kpi_dependencies/<vertical>.json, one real file per vertical,
   and the parameter is actually read this time — see the function itself.

All tools register on the shared `mcp` instance from cs_pulse_mcp_server.
"""

from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


# ===================================================================
# KPI Tier resolution (SaaS verticals)
# ===================================================================

def _load_tier_config():
    """Load the SaaS KPI tier definitions from config."""
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), '..', 'config', 'saas_kpi_tiers.json')
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _resolve_kpi_tier(tier: str, vertical: str) -> dict:
    """Resolve tier name to tier definition. Returns None for non-SaaS or unrecognized tier."""
    if vertical not in ('saas_premium', 'saas'):
        return None  # Non-SaaS verticals use the full catalog — no tiers yet

    config = _load_tier_config()
    if not config:
        return None

    tiers = config.get('tiers', {})

    if tier and tier in tiers:
        return tiers[tier]

    default = config.get('default_tier', 'saas_starter_9')
    return tiers.get(default)


def _apply_kpi_tier(customer_config, tier_def: dict) -> dict:
    """Apply tier KPI selection AND pillar weights to a CustomerConfig.

    Shift-left: sets both enabled_kpis and pillar_weights at creation time
    so health scores are computed correctly from the first process_data call.
    Without pillar_weights, the scorer falls back to full-catalog defaults
    which spread weight across all pillars — including pillars with zero
    KPIs in the tier, diluting the score.
    """
    kpi_codes = tier_def.get('kpi_codes')
    if kpi_codes == 'all':
        customer_config.enabled_kpis = None
        customer_config.pillar_weights = None  # use catalog defaults
    elif kpi_codes:
        customer_config.enabled_kpis = kpi_codes

        active_pillars = tier_def.get('pillars')
        if active_pillars and len(active_pillars) < 5:
            equal_weight = round(1.0 / len(active_pillars), 4)
            pw = {p: equal_weight for p in active_pillars}
            diff = round(1.0 - sum(pw.values()), 4)
            if diff != 0:
                pw[active_pillars[-1]] = round(pw[active_pillars[-1]] + diff, 4)
            customer_config.pillar_weights = pw
            customer_config.weights_origin = 'vertical_default'   # the tier's default, not a person's choice — HealthScore.weight_source says so

    return {
        'name': tier_def.get('display_name'),
        'model_grade': tier_def.get('model_grade'),
        'kpi_count': tier_def.get('kpi_count'),
        'pillars': tier_def.get('pillars'),
        'upgrade_path': tier_def.get('upgrade_path'),
    }


# ===================================================================
# Tool: create_customer
# ===================================================================

@mcp.tool
def create_customer(
    name: str,
    domain: str,
    vertical: str,
    admin_email: str,
    admin_name: str,
    data_origin: str,
    tier: str = None,
) -> dict:
    """Create a new customer with admin user and auto-generated API key.

    This is the first write step in onboarding. Creates:
    1. Customer record (with UUID)
    2. Admin user (with a one-time password-setup token — see admin_setup_token in the result)
    3. CustomerConfig (vertical defaults)
    4. API key (returned once — save it! Not issued yet if api_key_service
       isn't available in this build — known gap, see project memory)

    No authentication required — this is the entry point for new prospects.

    After creation, onboard with the 4-CSV canonical set:
        1. accounts.csv — enriched with products, champion, contract, firmographic data
        2. kpi_measurements.csv — KPI time-series from customer systems
        3. enhanced_qualitative_signals.csv — signal feed (NPS, escalations, champion changes)
        4. outcomes.csv — CRM renewal/churn/expansion history
        Then call process_data() — Wizard A auto-generates context graph.

    Args:
        name: Company name
        domain: Email domain (e.g. 'acme.com')
        vertical: Vertical slug (e.g. 'datacenter_v1')
        admin_email: Admin user email
        admin_name: Admin user display name
        data_origin: REQUIRED. Where this tenant's data comes from — 'real' (a customer's
            own data) or 'synthetic_demo' / 'synthetic_replay' / 'synthetic_test'. Disclosed
            on every surface; only 'real' earns a 'measured' label. Changing it later is an
            audited action (declare_data_origin).
        tier: Optional KPI tier for SaaS verticals. Options:
            'saas_starter_9' — 9 KPIs, 4 pillars, 1-hour onboarding (default for SaaS)
            'saas_predictive_11' — 11 KPIs, behavioral signals, requires product analytics
            'saas_full_43' — all KPIs, enterprise deployment
            If omitted, SaaS defaults to 'saas_starter_9'. Other verticals use the full catalog.
    """
    _require_auth_if_key_present('create_customer', None)
    _check_mcp_enabled()
    from utils.data_origin import validate as _validate_origin, disclosure as _disclosure
    try:
        data_origin = _validate_origin(data_origin)
    except ValueError as e:
        raise ToolError(str(e))
    app = _get_flask_app()

    with app.app_context():
        from models import Customer, User, CustomerConfig
        from extensions import db

        existing = Customer.query.filter_by(domain=domain).first()
        if existing:
            raise ToolError(
                f"A customer with domain '{domain}' already exists "
                f"(customer_id={existing.customer_id}). "
                f"Use complete_onboarding(check_only=True) to check its state."
            )

        existing_user = User.query.filter_by(email=admin_email).first()
        if existing_user:
            raise ToolError(f"Email '{admin_email}' is already registered.")

        try:
            from id_generator import generate_id
            uuid_vertical = 'dc' if vertical.startswith('dc') else vertical
            customer_uuid = generate_id(uuid_vertical, 'customer')
        except Exception:
            customer_uuid = None

        customer = Customer(
            customer_name=name,
            email=admin_email,
            domain=domain,
            vertical=vertical,
            data_origin=data_origin,
        )
        if customer_uuid:
            customer.uuid = customer_uuid
        db.session.add(customer)
        db.session.flush()

        customer_id = customer.customer_id

        user = User(
            customer_id=customer_id,
            user_name=admin_name,
            email=admin_email,
            role='admin',
            vertical=vertical,
            # This tenant's administrator — NOT a platform operator. The scope column has
            # to say so explicitly: app_api.auth.allows_customer is fail-closed and reads
            # only this list (see that module's docstring for the leak this closes). The
            # platform-wide identity is the Bearer MCP_SERVER_API_KEY, never a session role.
            allowed_customer_ids=[customer_id],
        )
        if customer_uuid:
            user.customer_uuid = customer_uuid
        try:
            from id_generator import generate_id as _gen_id
            user.uuid = _gen_id('dc' if vertical.startswith('dc') else vertical, 'user')
        except Exception:
            pass
        db.session.add(user)
        db.session.flush()

        from app_api.auth import issue_setup_token
        setup_token = issue_setup_token(user)

        config = CustomerConfig(
            customer_id=customer_id,
            vertical=vertical,
        )

        # ── Apply KPI tier (SaaS verticals) ──
        resolved_tier = _resolve_kpi_tier(tier, vertical)
        tier_info = None
        if resolved_tier:
            tier_info = _apply_kpi_tier(config, resolved_tier)

        db.session.add(config)

        # Known gap: api_key_service.py + CustomerApiKey aren't ported yet
        # (see mcp_server/auth.py's module docstring) — degrades to no key
        # issued, same as the old repo's own try/except around this call.
        try:
            from api_key_service import generate_api_key as _gen_api_key
            full_key, _key_record = _gen_api_key(
                customer_id=customer_id,
                created_by=user.user_id,
                name='MCP Onboarding Key',
                scopes=['read', 'write'],
            )
        except Exception:
            full_key = None

        # ── Auto-enable ALL features for Beta ──
        ALL_FEATURES = [
            'context_graph', 'story_arcs', 'signal_edges',
            'stakeholder_tracking', 'decision_lifecycle',
            'outcome_economics', 'industry_benchmarks',
        ]
        from models import FeatureToggle as _FT
        for feat in ALL_FEATURES:
            existing_toggle = _FT.query.filter_by(customer_id=customer_id, feature_name=feat).first()
            if not existing_toggle:
                db.session.add(_FT(
                    customer_id=customer_id,
                    feature_name=feat,
                    enabled=True,
                    config={sub: True for sub in ALL_FEATURES if sub != 'context_graph'} if feat == 'context_graph' else {},
                    description='Auto-enabled at customer creation (Beta)',
                ))

        db.session.commit()

        result = {
            'scope': 'customer',
            'customer_id': customer_id,
            'customer_name': name,
            'customer_uuid': customer_uuid,
            'domain': domain,
            'vertical': vertical,
            'created_at': customer.created_at.isoformat() if customer.created_at else None,
            'admin_user_id': user.user_id,
            'admin_email': admin_email,
            'admin_setup_token': setup_token,
            'admin_setup_token_note': 'Shown only once — use it at POST /app/api/auth/set-password to set the admin login password.',
            'data_origin': data_origin,
            'disclosure': _disclosure(data_origin),
        }
        from mcp_server import audit as _audit
        _audit.record('mcp', 'create_customer', customer_id, key_kind='n/a', outcome='allowed', detail=f'data_origin={data_origin}')

        if full_key:
            result['api_key'] = full_key
            result['api_key_note'] = (
                'Save this API key — it is shown only once. '
                'Use it for the intelligence tools (list_accounts, get_account_health, etc.).'
            )
            import logging as _key_log
            _masked = full_key[:12] + '...' + full_key[-4:] if len(full_key) > 16 else '***'
            _key_log.getLogger(__name__).info(
                f"API key generated for customer {customer_id}: {_masked}"
            )

        if tier_info:
            result['tier'] = tier_info

        return result


# ===================================================================
# KPI Dependency Guard
# ===================================================================

@mcp.tool
def upload_csv(customer_id: int, file_type: str, csv_content: str, dry_run: bool = False) -> dict:
    """Upload CSV data for a customer.

    Stages the CSV content in the database (CsvUploadStaging) — see that
    model's docstring for why this build stages to a DB table rather than
    the old repo's per-customer disk directory. The file can then be
    processed via process_data().

    When dry_run=True, validates the CSV against the platform schema
    (required/optional columns, row count) but does NOT persist data.

    Canonical 4-CSV onboarding set:
      'accounts.csv' — enriched with products, champion, contract, firmographic
      'kpi_measurements.csv' — KPI time-series
      'enhanced_qualitative_signals.csv' — signal feed
      'outcomes.csv' — CRM renewal/churn/expansion history

    Args:
        customer_id: The customer ID
        file_type: The CSV file type (e.g. 'accounts.csv', 'kpi_measurements.csv')
        csv_content: The raw CSV content as a string
        dry_run: If True, validate only — do not persist. Returns validation result.
    """
    key_record = _require_auth_if_key_present('upload_csv', customer_id)
    _check_mcp_enabled()

    app = _get_flask_app()
    with app.app_context():
        from utils.csv_upload import _upload_csv_impl
        from mcp_server.auth import extract_api_key
        key_kind = 'customer' if key_record else ('server' if extract_api_key() else ('local' if os.environ.get('MCP_TRANSPORT', 'stdio') != 'http' else 'none'))
        result = _upload_csv_impl(
            customer_id=customer_id,
            file_type=file_type,
            csv_content=csv_content,
            dry_run=dry_run,
            key_kind=key_kind, key_id=getattr(key_record, 'id', None),
        )

        if result.status == 'error' or (result.status == 'validation_error' and not dry_run):
            raise ToolError(
                f"CSV upload failed for {file_type}: {'; '.join(result.errors)}. "
                f"Use dry_run=True to inspect details."
            )

        d = result.to_dict()
        d['scope'] = 'validation' if dry_run else 'customer'
        return d


# ===================================================================
# Tool: process_data
# ===================================================================

def _process_data_impl(customer_id: int, mode: str = 'auto') -> dict:
    """Run the data pipeline for a customer.

    Path 2 (staged CSVs exist): ingest them — utils/csv_ingest.py — then
    run the post-ingest stages. Path 1 (nothing staged, data already in
    DB): post-ingest stages only. Neither: error.

    Ported 2026-09-01 (Tier 2A-3) from the old repo's 1338-line inline
    version. The ingest half is utils/csv_ingest.py (its module docstring
    lists every behavioral change and bug fixed). The post-ingest stages
    land one per sub-checkpoint in mcp_server/process_data_pipeline.py;
    the slots below are in the old repo's stage order, already reflecting
    the items 28/32/38 ordering fixes.

    `mode`: 'auto' (default) — health scores are immutable, only new
    months get scored; 'full_recalc' — rewrite every month with current
    weights. Only meaningful once health scoring is ported; accepted and
    passed through now so callers don't change later.
    """
    import time
    _t0 = time.time()

    _check_mcp_enabled()
    app = _get_flask_app()

    with app.app_context():
        from models import Customer, Account, KPIMeasurement
        from extensions import db
        from utils.vertical_registry import get_vertical_for_customer
        from utils.csv_ingest import ingest_staged_csvs, staged_files
        from mcp_server.process_data_pipeline import (
            calculate_health_scores,
            backfill_product_adoption,
            run_wizard_a_step,
            run_wizard_b_step,
            run_wizard_d_step,
            link_stakeholders_to_decisions,
        )

        customer = db.session.get(Customer, int(customer_id))
        if not customer:
            raise ToolError(f"Customer {customer_id} not found.")
        try:
            vertical = get_vertical_for_customer(customer_id)
        except ValueError as e:
            raise ToolError(str(e))
        # utils.vertical_health memoises customer_id → vertical for the scorer.
        # A run must start from the DB's answer: the vertical can be changed
        # between runs, and any process that reuses ids (test DBs recreated
        # per module) would otherwise score with a stale catalog — caught on
        # the customer-415 parity run scoring dc2_s data as datacenter_v1.
        from utils.vertical_health import clear_vertical_cache
        clear_vertical_cache(customer_id)

        steps, errors, timings = [], [], {}

        # The run record (G2): every health row written below names it.
        from models import ProcessRun
        from journeys.wizard_a import GENERATOR_VERSION
        from mcp_server.auth import extract_api_key, validate_customer_key
        import uuid as _uuid
        _key = extract_api_key()
        _rec = validate_customer_key(_key) if _key else None
        run = ProcessRun(run_id=f'pd_{_uuid.uuid4().hex[:16]}', customer_id=customer_id, vertical=vertical, mode=mode,
                         status='running', generator_version=GENERATOR_VERSION,
                         key_kind=('customer' if _rec else 'server' if _key else ('local' if os.environ.get('MCP_TRANSPORT', 'stdio') != 'http' else 'none')),
                         key_id=getattr(_rec, 'id', None))
        db.session.add(run)
        db.session.commit()
        run_db_id = run.id
        counts: dict = {}

        accounts = Account.query.filter_by(customer_id=customer_id).all()
        acct_ids = [a.account_id for a in accounts]
        kpi_count = (
            KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(acct_ids)).count()
            if acct_ids else 0
        )
        data_in_db = bool(accounts) and kpi_count > 0
        has_staged = bool(staged_files(customer_id))

        if not data_in_db and not has_staged:
            raise ToolError(
                f"No data found for customer {customer_id}. "
                f"Upload CSV files via upload_csv() first."
            )

        files_processed = None
        upload_ids: list = []
        if has_staged:
            ingest = ingest_staged_csvs(customer_id, vertical, process_run_id=run_db_id)
            steps.extend(ingest.steps)
            errors.extend(ingest.errors)
            timings.update(ingest.timings)
            counts.update(ingest.counts)
            upload_ids = ingest.upload_ids
            files_processed = ingest.files
            accounts = Account.query.filter_by(customer_id=customer_id).all()
            acct_ids = [a.account_id for a in accounts]
            kpi_count = (
                KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(acct_ids)).count()
                if acct_ids else 0
            )
        else:
            timings['csv_load'] = timings['cg_load'] = 0
            steps.append(f'data_already_in_db_{len(accounts)}_accounts_{kpi_count}_kpis')

        # Stage 2: health scores (immutable — only new months in 'auto')
        health_step, changed_account_ids, health_timings = calculate_health_scores(
            customer_id, accounts, mode=mode, process_run_id=run_db_id,
        )
        if health_step:
            steps.append(health_step)
        else:
            errors.append('health_scores: stage failed (see log) — no rows written')
        timings.update(health_timings)
        # Stage 2 event publish (HEALTH_SCORES_UPDATED) — deferred with its
        # subscribers; see process_data_pipeline's module docstring.

        # Stage 2b: adoption-pillar score → profile_metadata products
        _t = time.time()
        step = backfill_product_adoption(customer_id, accounts, vertical)
        if step:
            steps.append(step)
        timings['product_adoption'] = round(time.time() - _t, 2)

        # Stage 2c: proactive signal scan                — later phase

        # Stage 3: Wizard A v2 — journeys, evidence-cited arcs, leading layer
        wa_step, wa_duration, wa_summary = run_wizard_a_step(customer_id, changed_account_ids, mode)
        if wa_step:
            steps.append(wa_step)
        timings['wizard_a'] = wa_duration

        # Item 38: stakeholder→decision INVOLVES linking, after Wizard A.
        _t = time.time()
        step = link_stakeholders_to_decisions(customer_id)
        if step:
            steps.append(step)
        timings['stakeholder_linking'] = round(time.time() - _t, 2)

        # Stage 3b: Wizard B — Hindsight over the journeys (≥5), persisted as a WizardRun
        wb_step, wb_duration = run_wizard_b_step(customer_id)
        if wb_step:
            steps.append(wb_step)
        timings['wizard_b'] = wb_duration

        # Stage 3c: Wizard D — Foresight over the journeys, embedded as journey_json['forecast']
        wd_step, wd_duration = run_wizard_d_step(customer_id)
        if wd_step:
            steps.append(wd_step)
        timings['wizard_d'] = wd_duration

        # Stages 3a, 4–8 (LLM tier-1, signal analyst, urgent scanner, ROI,
        # approval seed, Qdrant, onboarding agent) — later phases.

        status = 'success' if steps and not errors else 'partial' if steps else 'failed'
        duration = round(time.time() - _t0, 1)
        timings['total'] = duration
        counts.update({'accounts': len(accounts), 'kpi_measurements': kpi_count, 'changed_accounts': len(changed_account_ids)})
        run = db.session.get(ProcessRun, run_db_id)
        run.status, run.steps, run.errors, run.timings, run.counts, run.upload_ids = status, steps, errors, timings, counts, upload_ids
        run.finished_at = datetime.utcnow()
        db.session.commit()

        import logging
        logging.getLogger(__name__).info(
            "process_data complete: customer=%s mode=%s duration=%ss timings=%s",
            customer_id, mode, duration, timings,
        )

        return {
            'scope': 'customer',
            'customer_id': customer_id,
            'run_id': run.run_id,
            'status': status,
            'mode': mode,
            'vertical': vertical,
            'accounts': len(accounts),
            'kpi_measurements': kpi_count,
            'csv_files_processed': files_processed,
            'steps_completed': steps,
            'context_graph_audit': None,  # invariant audit — later phase
            'wizard_a': (
                {'coverage': wa_summary['coverage'], 'arcs': wa_summary['arcs']}
                if wa_summary else None
            ),
            'errors': errors,
            'duration_s': duration,
            'timings': timings,
            'message': (
                f"Data processing {'completed' if status == 'success' else 'completed with issues'} "
                f"(mode={mode}, {duration}s). "
                f"Steps: {', '.join(steps) if steps else 'none'}."
            ),
        }


@mcp.tool
def process_data(customer_id: int, mode: str = 'auto') -> dict:
    """Trigger the data processing pipeline for a customer.

    Ingests every CSV staged via upload_csv() into the database — accounts,
    KPI measurements, qualitative signals, and the context-graph files
    (outcomes, stakeholders, decisions, engagement events, profiles,
    benchmarks, signal edges) — then runs the post-ingest stages. Staged
    files are consumed on a fully successful run; if any step errors they
    are kept so the run can be retried (every loader is idempotent).

    Health scores are immutable: once written for (account, month) they are
    never retroactively recalculated. Weight changes apply forward only.

    Args:
        customer_id: The customer ID
        mode: 'auto' (default, immutable scores) or 'full_recalc' (admin rewrite)
    """
    _require_auth_if_key_present('process_data', customer_id)
    if mode not in ('auto', 'full_recalc'):
        mode = 'auto'
    return _process_data_impl(customer_id, mode=mode)


# ===================================================================
# Tool: trigger_wizard
# ===================================================================

_WIZARDS = {
    'a': 'Journeys (Wizard A v2 — evidence-cited arcs, leading layer)',
    'b': 'Hindsight (Wizard B — patterns, transitions, realized NRR, backtest)',
    'd': 'Foresight (Wizard D — per-account retention / expansion / expected ARR with basis + interval, portfolio roll-up)',
    'c': 'Calibration (Wizard C — weight proposal from logged outcomes, human-approved)',
}


@mcp.tool
def trigger_wizard(customer_id: int, wizard: str) -> dict:
    """Run a wizard for a customer on demand (process_data runs both automatically).

    - 'a': rebuild every account's journey — arc hypothesis with cited
      evidence, phases, leading-vs-trailing series, expected-path overlay.
    - 'b': Hindsight over the journeys — arc pattern profiles, phase
      transition matrix with triggers, realized NRR per arc, intervention
      before/after, the lead-time backtest, and data-derived early-warning
      rules. Needs ≥5 journeys. Results are stored as a WizardRun.

    - 'd': Foresight over the journeys — per account, retention and
      expansion probability with an interval and a basis label ('prior'
      template until the tenant has enough labelled outcomes, then
      'calibrated' on them), expected ARR at the horizon end, and a
      revenue-weighted portfolio roll-up with the range propagated. Stored
      as a ForecastRun and embedded in each journey as `forecast`; read
      with get_forecast.
    - 'c': Calibration — labels every logged OUTCOME by its revenue bucket,
      scores the account's KPIs in the window before it, and proposes
      pillar/KPI weights with per-weight evidence and a before/after on
      every account. Writes a proposal only; nothing changes until
      approve_calibration. Below the outcome gate the result is
      insufficient_outcomes with the counts. Never runs from process_data.

    Args:
        customer_id: The customer ID
        wizard: 'a', 'b', 'c' or 'd'
    """
    _require_auth_if_key_present('trigger_wizard', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()

    with app.app_context():
        from models import Customer, WizardRun
        from extensions import db
        import uuid as _uuid
        from datetime import datetime as _dt

        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f"Customer {customer_id} not found.")
        wizard = (wizard or '').lower().strip()
        if wizard not in _WIZARDS:
            raise ToolError(f"Invalid wizard '{wizard}'. Available in this build: {sorted(_WIZARDS)}.")

        run_id = f"wizard_{wizard}_{_dt.utcnow().strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:8]}"
        try:
            if wizard == 'a':
                from journeys.wizard_a import run_wizard_a
                result = run_wizard_a(customer_id)
                summary = {k: v for k, v in result.items() if k != 'arcs'} | {'accounts': len(result.get('arcs', {}))}
            elif wizard == 'd':
                from wizards.wizard_d_foresight import run_wizard_d
                result = run_wizard_d(customer_id, created_by='trigger_wizard')
                summary = result if result.get('status') != 'completed' else {
                    'status': 'completed', 'run_id': result['run_id'], 'accounts': result['accounts'],
                    'basis_counts': result['basis_counts'],
                    'labels': {k: result['labels'][k] for k in ('n', 'positive', 'negative', 'needed', 'per_class_needed', 'reason')},
                    'portfolio': {k: result['portfolio'][k] for k in ('arr', 'expected_arr_end', 'low', 'high', 'nrr', 'nrr_low', 'nrr_high',
                                                                      'headline_assumption', 'basis')},
                }
            elif wizard == 'c':
                from wizards.wizard_c_calibration import propose
                result = propose(customer_id)
                summary = {k: result.get(k) for k in ('status', 'proposal_id', 'outcome_counts', 'adjusted', 'short_by', 'note') if k in result}
                if result.get('status') == 'proposed':
                    summary['impact'] = result['impact']['summary']
                    summary['proposed_pillar_weights'] = result['proposed']['pillar_weights']
            else:
                from wizards.wizard_b_hindsight import run_wizard_b
                result = run_wizard_b(customer_id, persist=False)
                summary = result if result.get('status') != 'completed' else {
                    'status': 'completed', 'journeys': result['journeys'], 'coverage': result['coverage'],
                    'evidence_label': result['evidence_label'],
                    'patterns': list(result['pattern_profiles']),
                    'portfolio_nrr': result['realized_nrr']['portfolio']['nrr'],
                    'h1': {k: v for k, v in result['backtest']['results']['H1_retention'].items() if k != 'per_event'},
                    'rules': len(result['early_warning_rules']),
                }
            status = 'completed' if result.get('status') in ('completed', 'skipped', 'proposed', 'insufficient_outcomes', 'no_confident_effect') else 'failed'
            run = WizardRun(run_id=run_id, customer_id=customer_id, wizard=wizard, status=status,
                            config={'triggered_via': 'mcp_trigger_wizard'}, results=result,
                            completed_at=_dt.utcnow(), created_by='trigger_wizard')
            db.session.add(run)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            db.session.add(WizardRun(run_id=run_id, customer_id=customer_id, wizard=wizard, status='failed',
                                     config={'triggered_via': 'mcp_trigger_wizard'}, error_message=str(e),
                                     completed_at=_dt.utcnow(), created_by='trigger_wizard'))
            db.session.commit()
            raise ToolError(f"Wizard {wizard.upper()} failed: {e}")

        return {
            'scope': 'customer', 'customer_id': customer_id, 'wizard': wizard,
            'wizard_name': _WIZARDS[wizard], 'run_id': run_id, 'status': status,
            'result_summary': summary,
        }


# ===================================================================
# Tools: signals (signal engine v2)
# ===================================================================

@mcp.tool
def submit_signal(customer_id: int, account_id: int, raw_text: str, source_type: str = 'manual',
                  occurred_at: str = None, signal_type: str = None, participants: list = None,
                  source_ref: str = None, consent_verified: bool = None, process_now: bool = True) -> dict:
    """Record one piece of evidence for an account and, by default, turn it
    into a journey episode immediately.

    - raw_text: what happened (a note, an email, a ticket summary, a meeting
      takeaway). Free text is classified by the LLM into a taxonomy role.
    - signal_type: optional taxonomy subtype (e.g. 'champion_departure',
      'usage_decline', 'expansion_interest'). When given, no LLM call is made —
      the structured path. Unknown subtypes fall back to LLM classification.
    - occurred_at: ISO timestamp of the EVENT (not of this call). Always pass
      it for anything that didn't just happen — the journey is dated by it.
    - participants: [{"name": "Lisa Park", "role": "Director of Infrastructure"}]
      — resolved against the account's roster; unresolved people are kept and
      flagged, never dropped.
    - source_type: manual | email | slack | transcript | ticket | crm_activity | meeting | external
    - Exact duplicates (same account, same text, within 7 days) are reported,
      not stored twice.

    Args:
        customer_id: The customer ID
        account_id: The account ID (must belong to the customer)
        raw_text: The evidence text
        source_type: Where it came from (default 'manual')
        occurred_at: ISO timestamp of the event (default: now)
        signal_type: Taxonomy subtype for the structured path (optional)
        participants: People involved, [{name, role}] (optional)
        source_ref: Source-system reference (ticket id, message id) (optional)
        consent_verified: Required true for transcripts
        process_now: Classify + write the evidence node + rebuild the journey now (default true)
    """
    _require_auth_if_key_present('submit_signal', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from signal_engine.pipeline import ingest, process_pending
        try:
            res = ingest(customer_id, account_id, source_type, raw_text, occurred_at=occurred_at,
                         participants=participants, signal_type=signal_type, source_ref=source_ref,
                         consent_verified=consent_verified)
        except ValueError as e:
            raise ToolError(str(e))
        if res['status'] == 'queued' and process_now:
            out = process_pending(customer_id=customer_id, limit=50)
            mine = next((x for x in out['signals'] if x['signal_id'] == res['signal_id']), None)
            res.update({'processed': True, 'evidence': mine, 'journeys_rebuilt': out['journeys_rebuilt']})
        return res


@mcp.tool
def research_account_external_signals(customer_id: int, account_id: int, process_now: bool = True) -> dict:
    """Research an account's real-world company for recent public signals — funding,
    leadership changes, layoffs, hiring shifts, competitor adoption — via live web
    search, then record findings the same way any other evidence enters the graph
    (source_type='external').

    On-demand only, never automatic: each call is a real, metered LLM request with
    live web search, gated by the customer's own LLM budget (get_llm_cost_summary
    shows current spend; the call raises if the budget circuit breaker is tripped).
    External findings are structurally weaker evidence than a customer's own
    communication — the research is prompted to hedge anything not confirmed by an
    official source, and unreviewed/low-confidence findings surface in the review
    queue the same way any other uncertain signal does, not silently trusted.

    Args:
        customer_id: The customer ID
        account_id: The account ID (must belong to the customer)
        process_now: Classify + write the evidence node + rebuild the journey now (default true)
    """
    _require_auth_if_key_present('research_account_external_signals', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from signal_engine.external_research import research_and_ingest
        try:
            return research_and_ingest(customer_id, account_id, process_now=process_now)
        except (ValueError, RuntimeError) as e:
            raise ToolError(str(e))
        except PermissionError as e:
            raise ToolError(f'budget check failed: {e}')


@mcp.tool
def process_signals(customer_id: int, limit: int = 50) -> dict:
    """Turn every pending signal for a customer into evidence (classify,
    reconcile polarity, resolve people, write the node) and rebuild the
    journeys of the accounts touched. The background worker does this
    automatically; call it to force a pass (e.g. after a webhook burst or a
    bulk ingest).

    Args:
        customer_id: The customer ID
        limit: Max signals per pass (default 50)
    """
    _require_auth_if_key_present('process_signals', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from signal_engine.pipeline import process_pending
        return process_pending(customer_id=customer_id, limit=limit)


@mcp.tool
def configure_signal_engine(customer_id: int, enabled: bool = True, slack_team_id: str = None,
                            slack_channel_map: dict = None) -> dict:
    """Enable the webhook sources (Slack, inbound email) for a customer and
    map their Slack workspace / channels to accounts. MCP submit_signal and
    the JSON ingest routes don't need this — they are key-authenticated.

    Args:
        customer_id: The customer ID
        enabled: Turn the per-customer signal_engine toggle on/off
        slack_team_id: Slack workspace id (T0…) that maps to this customer
        slack_channel_map: {"C04…": account_id, …}
    """
    _require_auth_if_key_present('configure_signal_engine', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import Customer, FeatureToggle
        from extensions import db
        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f"Customer {customer_id} not found.")
        t = FeatureToggle.query.filter_by(customer_id=customer_id, feature_name='signal_engine').first()
        if not t:
            t = FeatureToggle(customer_id=customer_id, feature_name='signal_engine', enabled=enabled, config={})
            db.session.add(t)
        t.enabled = bool(enabled)
        cfg = dict(t.config or {})
        if slack_team_id is not None:
            cfg['slack_team_id'] = slack_team_id
        if slack_channel_map:
            cfg['slack_channel_map'] = {str(k): int(v) for k, v in slack_channel_map.items()}
        t.config = cfg
        db.session.commit()
        return {'customer_id': customer_id, 'signal_engine_enabled': t.enabled, 'config': cfg}


def _kpi_dependencies_path(vertical: str) -> str:
    """config/kpi_dependencies/<vertical>.json — same one-file-per-vertical convention as
    config/economics/<vertical>.json and config/investment/<vertical>.json. A separate
    function (not inlined) so tests can monkeypatch it to point at a fixture file."""
    import os
    return os.path.join(os.path.dirname(__file__), '..', 'config', 'kpi_dependencies', f'{vertical}.json')


def _check_kpi_dependencies(vertical: str, enabled_kpis=None, enabled_pillars=None):
    """Check if disabled KPIs/pillars affect downstream engines (ROI, arc classifier),
    using `vertical`'s OWN dependency map — config/kpi_dependencies/<vertical>.json.

    Returns list of warning strings. Empty list = no issues.
    Only warns when the customer has EXPLICITLY selected a subset of KPIs/pillars
    (not when using defaults = all enabled).

    Fails closed on anything short of a validated, matching file: no file yet for this
    vertical, an unparseable file, or a file whose own declared 'vertical' key doesn't
    match the one asked for (a copy-paste-wrong-vertical guard — exactly the bug class
    this function used to have when one dc2_s-flavoured file was read for every
    vertical). Every case logs clearly and returns [] — never another vertical's
    warnings, and never an exception, since this is advisory UX, not a hard gate on
    configure_customer_kpis.
    """
    if not enabled_kpis and not enabled_pillars:
        return []  # Using all defaults — no warnings needed

    import json
    import logging
    import os
    log = logging.getLogger(__name__)
    deps_path = _kpi_dependencies_path(vertical)
    if not os.path.exists(deps_path):
        log.warning("_check_kpi_dependencies: no dependency map yet for vertical %r (expected %s) — "
                    "skipping dependency warnings for this call, NOT falling back to another "
                    "vertical's data", vertical, deps_path)
        return []
    try:
        with open(deps_path) as f:
            deps = json.load(f)
    except Exception as e:
        log.warning("_check_kpi_dependencies: %s failed to load (%s) — skipping dependency warnings", deps_path, e)
        return []
    if deps.get('vertical') != vertical:
        log.warning("_check_kpi_dependencies: %s declares vertical=%r, expected %r — refusing to use it "
                    "(skipping dependency warnings rather than risk showing another vertical's content)",
                    deps_path, deps.get('vertical'), vertical)
        return []

    warnings = []

    if enabled_pillars:
        all_pillars = set(deps.get('pillar_dependencies', {}).keys())
        disabled_pillars = all_pillars - set(enabled_pillars)
        for p in sorted(disabled_pillars):
            dep = deps['pillar_dependencies'].get(p)
            if dep:
                warnings.append(dep['warning'])

    if enabled_kpis:
        all_kpi_deps = deps.get('dependencies', {})
        for kpi_code, dep in all_kpi_deps.items():
            if kpi_code not in enabled_kpis:
                warnings.append(dep['warning'])

    return warnings


def _configure_customer_kpis_impl(customer_id: int, pillar_weights: dict = None, kpi_weights: dict = None,
                                  enabled_kpis: list = None, kpi_overrides: dict = None,
                                  customized_by: str = None) -> dict:
    """Business logic for configure_customer_kpis — a human directly setting a tenant's
    KPI/pillar weights, the missing writer for weights_origin='customer_config' (the
    third label, alongside 'vertical_default' from create_customer's tier default and
    'wizard_c' from an approved calibration — see models.CustomerConfig.weights_origin).

    Overlay semantics: only the fields actually passed change (None = leave alone), same
    contract as playbooks.definitions.configure_tenant. Every pillar/KPI code is checked
    against the tenant's actual vertical catalog (utils.vertical_registry.get_kpis/
    get_pillars — the same source wizard_c_calibration.current_weights() reads) rather
    than trusting the caller; unknown codes raise ValueError. Weights are sanity-checked:
    not a number, negative, or a dict summing to <= 0 all raise. A KPI weight of exactly
    0 also raises — utils.generic_scorer treats a falsy weight_l1 override as *unset* and
    falls back to 1.0 (`if not l1_weight or l1_weight <= 0: l1_weight = 1.0`), so writing
    0 would silently do the opposite of what the caller asked; use enabled_kpis to drop a
    KPI entirely instead. Pillar weights may be 0 (the scorer honours that literally,
    zeroing the pillar's contribution) as long as the group doesn't sum to <= 0.

    kpi_overrides is merged into the existing dict, not replaced: the same JSON column
    also holds utils.llm_budget_controller's per-customer LLM budget under the
    'llm_budget' key (kept there "to avoid adding new columns" — see that module), and a
    full-column replace here would silently delete a budget nothing else in this build
    writes back. Every other field is replaced whole, matching configure_tenant's
    per-field overlay contract.

    On success: config_version is bumped, weights_origin is set to 'customer_config',
    customized_by is set (given, else the calling actor's label), and health is
    recomputed through the normal pipeline in 'full_recalc' mode — the same mode
    wizard_c_calibration.approve() uses — so every account's stored score reflects the
    new weights immediately rather than waiting for the next CSV upload. A recompute
    failure is recorded on the result, not hidden — the config write already committed.

    Assumes an app context is already open (the @mcp.tool wrapper below provides one),
    matching wizards.wizard_c_calibration.approve()'s contract.
    """
    from extensions import db
    from models import Account, CustomerConfig, HealthScore
    from utils.vertical_registry import get_vertical_for_customer, get_kpis, get_pillars
    from wizards.wizard_c_calibration import current_actor, _bump_version

    if pillar_weights is None and kpi_weights is None and enabled_kpis is None and kpi_overrides is None:
        raise ValueError('configure_customer_kpis: pass at least one of pillar_weights, kpi_weights, '
                         'enabled_kpis, kpi_overrides — nothing to change')

    cc = CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()
    if not cc:
        raise ValueError(f'customer {customer_id} has no CustomerConfig row')
    vertical = get_vertical_for_customer(int(customer_id))     # fails closed on an unset vertical
    kpis, pillars = get_kpis(vertical), get_pillars(vertical)

    def _weight(value, where: str, allow_zero: bool = True) -> float:
        try:
            w = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{where}: {value!r} is not a number')
        if w < 0:
            raise ValueError(f'{where}: weight {w} is negative')
        if w == 0 and not allow_zero:
            raise ValueError(f"{where}: a weight of 0 is treated as *unset* by the scorer (falls back to 1.0), "
                             f"not as 'zero out this KPI' — drop the code from kpi_weights or from enabled_kpis "
                             f"to exclude it entirely")
        return w

    new_pillar_weights = None
    if pillar_weights is not None:
        if not isinstance(pillar_weights, dict) or not pillar_weights:
            raise ValueError('pillar_weights must be a non-empty {pillar_code: weight} dict')
        unknown = sorted(p for p in pillar_weights if p not in pillars)
        if unknown:
            raise ValueError(f'unknown pillar codes for vertical {vertical!r}: {unknown} (known: {sorted(pillars)})')
        new_pillar_weights = {p: _weight(w, f'pillar_weights[{p!r}]') for p, w in pillar_weights.items()}
        if sum(new_pillar_weights.values()) <= 0:
            raise ValueError('pillar_weights must have at least one positive weight (the group sums to <= 0)')

    new_kpi_weights = None
    if kpi_weights is not None:
        if not isinstance(kpi_weights, dict) or not kpi_weights:
            raise ValueError('kpi_weights must be a non-empty {pillar_code: {kpi_code: weight}} dict')
        new_kpi_weights = {}
        for p, ws in kpi_weights.items():
            if p not in pillars:
                raise ValueError(f'unknown pillar code {p!r} in kpi_weights for vertical {vertical!r} (known: {sorted(pillars)})')
            if not isinstance(ws, dict) or not ws:
                raise ValueError(f'kpi_weights[{p!r}] must be a non-empty {{kpi_code: weight}} dict')
            group = {}
            for code, w in ws.items():
                if code not in kpis:
                    raise ValueError(f'unknown KPI code {code!r} for vertical {vertical!r} (known: {sorted(kpis)})')
                if kpis[code].get('pillar') != p:
                    raise ValueError(f"KPI {code!r} belongs to pillar {kpis[code].get('pillar')!r}, not {p!r} "
                                     f"— nest it under its own pillar in kpi_weights")
                group[code] = _weight(w, f'kpi_weights[{p!r}][{code!r}]', allow_zero=False)
            if sum(group.values()) <= 0:
                raise ValueError(f'kpi_weights[{p!r}] must have at least one positive weight (the group sums to <= 0)')
            new_kpi_weights[p] = group

    new_enabled_kpis = None
    if enabled_kpis is not None:
        if not isinstance(enabled_kpis, list) or not enabled_kpis:
            raise ValueError('enabled_kpis must be a non-empty list of KPI codes')
        unknown = sorted({c for c in enabled_kpis if c not in kpis})
        if unknown:
            raise ValueError(f'unknown KPI codes for vertical {vertical!r}: {unknown} (known: {sorted(kpis)})')
        new_enabled_kpis = sorted(set(enabled_kpis))

    new_kpi_overrides = None
    if kpi_overrides is not None:
        if not isinstance(kpi_overrides, dict) or not kpi_overrides:
            raise ValueError('kpi_overrides must be a non-empty {kpi_code: {...}} dict')
        unknown = sorted(c for c in kpi_overrides if c not in kpis)
        if unknown:
            raise ValueError(f'unknown KPI codes for vertical {vertical!r}: {unknown} (known: {sorted(kpis)})')
        new_kpi_overrides = dict(kpi_overrides)

    # Dependency warnings (non-fatal): config/kpi_dependencies/<vertical>.json is scoped
    # to this customer's own vertical (P1-KPI1 is "Time-to-First-Workload" on dc2_s but
    # "Daily Active Users" on saas_premium — same code, different KPI) — no longer a
    # single dc2_s-only file read for every vertical. _check_kpi_dependencies fails
    # closed (returns [], logs) for a vertical with no dependency file yet rather than
    # falling back to dc2_s's or any other vertical's content.
    warnings = _check_kpi_dependencies(
        vertical,
        enabled_kpis=new_enabled_kpis,
        enabled_pillars=sorted(new_pillar_weights) if new_pillar_weights is not None else None,
    )

    actor = current_actor()
    label = (customized_by or '').strip() or actor['label']

    if new_pillar_weights is not None:
        cc.pillar_weights = new_pillar_weights
    if new_kpi_weights is not None:
        cc.kpi_weights = new_kpi_weights
    if new_enabled_kpis is not None:
        cc.enabled_kpis = new_enabled_kpis
    if new_kpi_overrides is not None:
        cc.kpi_overrides = {**(cc.kpi_overrides or {}), **new_kpi_overrides}
    fields_changed = [n for n, v in (('pillar_weights', new_pillar_weights), ('kpi_weights', new_kpi_weights),
                                     ('enabled_kpis', new_enabled_kpis), ('kpi_overrides', new_kpi_overrides))
                      if v is not None]
    cc.customized_by = label
    cc.config_version = _bump_version(cc.config_version)
    cc.weights_origin = 'customer_config'
    db.session.commit()

    from mcp_server import audit
    audit.record('customer_config', 'configure_customer_kpis', int(customer_id), key_kind=actor['key_kind'],
                key_record=actor.get('key_record'), outcome='allowed',
                detail=f'fields={fields_changed} by {label} -> config_version {cc.config_version}, '
                       f'weights_origin=customer_config' + (f' warnings={len(warnings)}' if warnings else ''))

    recompute = {'mode': 'full_recalc', 'status': None, 'run_id': None, 'steps': None, 'error': None,
                'health_rows_customer_config': 0}
    try:
        res = _process_data_impl(int(customer_id), mode='full_recalc')
        recompute.update({'status': res.get('status'), 'run_id': res.get('run_id'), 'steps': res.get('steps_completed')})
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('configure_customer_kpis: health recompute for customer %s failed: %s', customer_id, e)
        db.session.rollback()
        recompute.update({'status': 'failed', 'error': str(e)[:300]})
    acct_ids = [a.account_id for a in Account.query.filter_by(customer_id=int(customer_id)).all()]
    if acct_ids:
        recompute['health_rows_customer_config'] = HealthScore.query.filter(
            HealthScore.account_id.in_(acct_ids), HealthScore.weight_source == 'customer_config').count()

    return {
        'customer_id': int(customer_id), 'vertical': vertical, 'config_version': cc.config_version,
        'weights_origin': cc.weights_origin, 'customized_by': cc.customized_by,
        'pillar_weights': cc.pillar_weights, 'kpi_weights': cc.kpi_weights,
        'enabled_kpis': cc.enabled_kpis, 'kpi_overrides': cc.kpi_overrides,
        'fields_changed': fields_changed, 'warnings': warnings, 'recompute': recompute,
    }


@mcp.tool
def configure_customer_kpis(customer_id: int, pillar_weights: dict = None, kpi_weights: dict = None,
                            enabled_kpis: list = None, kpi_overrides: dict = None,
                            customized_by: str = None) -> dict:
    """Set a tenant's KPI/pillar weights by hand — the direct-human-input counterpart to
    an approved Wizard C calibration. Only the fields you pass change (overlay, not
    replace-everything); pass a field to fully replace it (kpi_overrides is the one
    exception — see below). Every pillar/KPI code is validated against the vertical's
    own catalog and rejected if unknown; weights are checked for being non-negative and
    for each group (all pillar weights, or the KPI weights within one pillar) summing to
    more than zero. Writes CustomerConfig with config_version bumped and
    weights_origin='customer_config', then recomputes health (full_recalc) so the change
    is visible immediately, not on the next CSV upload.

    Args:
        customer_id: The customer ID
        pillar_weights: Replace the tenant's pillar weights: {pillar_code: weight}, e.g.
            {"P1": 0.3, "P2": 0.3, "P3": 0.4}. Every key must be a real pillar for this
            vertical (get_vertical_config lists them); weights need not sum to 1 — the
            scorer normalises by the group total — but must sum to something positive.
        kpi_weights: Replace the tenant's KPI weights within each pillar:
            {pillar_code: {kpi_code: weight}}, e.g. {"P1": {"P1-KPI1": 2, "P1-KPI2": 1}}.
            Every KPI must belong to the pillar it's nested under. A weight of exactly 0
            is rejected (the scorer treats it as unset, not as zero-out) — use
            enabled_kpis to drop a KPI instead.
        enabled_kpis: Replace the tenant's enabled-KPI list — every other KPI in the
            catalog is excluded from scoring. Must be non-empty; every code must be real.
        kpi_overrides: Per-KPI overrides, e.g. {"P3-KPI1": {"target": 90}}. Merged into
            the existing dict (not replaced) so an unrelated key already stored there
            (this column also holds the LLM budget config) is never silently dropped.
        customized_by: Who made this change (e.g. an email or username), kept on the row.
            Defaults to the calling API key's label when omitted.
    """
    _require_auth_if_key_present('configure_customer_kpis', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        try:
            return _configure_customer_kpis_impl(customer_id, pillar_weights=pillar_weights, kpi_weights=kpi_weights,
                                                 enabled_kpis=enabled_kpis, kpi_overrides=kpi_overrides,
                                                 customized_by=customized_by)
        except ValueError as e:
            raise ToolError(str(e))


# ===================================================================
# Read surface (journeys + evidence) and human review
# ===================================================================

@mcp.tool
def list_journeys(customer_id: int) -> dict:
    """Portfolio view: one row per account — arc and state, current phase,
    latest leading (qual) vs trailing (kpi_only) month with its early-warning
    label and role counts, live months since the last KPI upload, last
    evidence date, lead days, open review count. Every number here is
    computed from cited evidence; use get_journey for the citations.

    Args:
        customer_id: The customer ID
    """
    _require_auth_if_key_present('list_journeys', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from journeys.read import list_journeys as _lj, origin_block
        rows = _lj(customer_id)
        return {'customer_id': customer_id, **origin_block(customer_id), 'accounts': len(rows), 'journeys': rows}


@mcp.tool
def get_journey(customer_id: int, account_id: int, compact: bool = False) -> dict:
    """One account's journey v3 with its evidence index: arc hypothesis with
    supporting episode ids, phases with trigger episodes, leading-vs-trailing
    series (incl. live months after the last KPI upload), episodes, and
    `evidence` — every cited node keyed by id with its verbatim quote, role,
    person, provenance (source, model, basis), confidence and review state.
    Cite evidence node ids / quotes when you summarise; never assert a claim
    the evidence map cannot back.

    Args:
        customer_id: The customer ID
        account_id: The account ID
        compact: Drop episodes/phases/hooks and keep the last 3 series months (default false)
    """
    _require_auth_if_key_present('get_journey', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from journeys.read import get_journey as _gj
        j = _gj(customer_id, account_id, compact=compact)
        if j is None:
            raise ToolError(f'no journey for account {account_id} — run process_data or trigger_wizard(customer_id, "a")')
        return j


@mcp.tool
def get_evidence(customer_id: int, account_id: int = None, node_ids: list = None, role: str = None,
                 since: str = None, until: str = None, include_rejected: bool = False, limit: int = 200) -> dict:
    """Evidence nodes (observed signals, decisions, outcomes) with quote, role,
    person, provenance, confidence and review state. Filter by account, node
    ids, taxonomy role (e.g. 'commercial_pressure'), or date range. Rejected
    evidence is hidden unless include_rejected.

    Args:
        customer_id: The customer ID
        account_id: Restrict to one account (optional)
        node_ids: Specific node ids, e.g. from a journey's evidence_node_ids (optional)
        role: Taxonomy signal role (optional)
        since: ISO date/time lower bound (optional)
        until: ISO date/time upper bound (optional)
        include_rejected: Include evidence a reviewer rejected (default false)
        limit: Max rows (default 200)
    """
    _require_auth_if_key_present('get_evidence', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from journeys.read import get_evidence as _ge
        rows = _ge(customer_id, account_id, node_ids, role, since, until, include_rejected=include_rejected, limit=limit)
        return {'customer_id': customer_id, 'count': len(rows), 'evidence': rows}


@mcp.tool
def get_review_queue(customer_id: int, account_id: int = None, urgency: str = None, page: int = 1, per_page: int = 25) -> dict:
    """Evidence awaiting human verification: signals the extractor flagged
    requires_review (low confidence, possible duplicate, unknown subtype).
    Until reviewed they count at reduced weight on the journey. Decide with
    review_signal.

    Args:
        customer_id: The customer ID
        account_id: Restrict to one account (optional)
        urgency: critical | high | medium | low (optional)
        page: Page number (default 1)
        per_page: Rows per page (default 25)
    """
    _require_auth_if_key_present('get_review_queue', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from signal_engine.ingest_api import review_queue
        code, body = review_queue(customer_id, account_id, urgency, page, per_page)
        if code != 200:
            raise ToolError(body.get('error', 'review queue failed'))
        return body


@mcp.tool
def review_signal(customer_id: int, signal_id: str, decision: str, subtype: str = None, node_id: int = None,
                  note: str = None, reviewer: str = None) -> dict:
    """Record a human decision on a piece of evidence (audited, journey rebuilt):
    - accept: the evidence stands at full weight.
    - reject: not evidence / wrong — the node is kept for audit but excluded
      from the journey, series and arcs.
    - reclassify: the model picked the wrong subtype — re-type to `subtype`
      (a taxonomy subtype); role, polarity and urgency are re-derived, the
      original is kept. Pass node_id when the signal has several nodes.

    Args:
        customer_id: The customer ID
        signal_id: The signal id (from get_review_queue / get_evidence provenance.source_event_id)
        decision: accept | reject | reclassify
        subtype: New taxonomy subtype (reclassify only)
        node_id: One evidence node of the signal (optional)
        note: Why (optional, stored)
        reviewer: Who (email or name; optional, stored)
    """
    _require_auth_if_key_present('review_signal', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from signal_engine.review import review_signal as _rs
        try:
            return _rs(customer_id, signal_id, decision, subtype=subtype, node_id=node_id, note=note, reviewer=reviewer)
        except ValueError as e:
            raise ToolError(str(e))


@mcp.tool
def log_outcome(customer_id: int, account_id: int, outcome_type: str, occurred_at: str, revenue: float = None,
                note: str = None, linked_signal_ids: list = None, decided_by: str = None, source_type: str = 'manual',
                source_ref: str = None, title: str = None, use_case: str = None) -> dict:
    """Record a decision on an account: renewal secured, churn lost,
    contraction, expansion closed, refresh won… This is the outcome record
    that Hindsight measures against (realized NRR, lead-time backtest) and
    that the journey narrative cites; without it a passed renewal reads
    "no outcome recorded".

    - outcome_type: a revenue-vocabulary subtype from the tenant taxonomy
      (e.g. renewal_secured, churn_lost, contraction, expansion_closed,
      revenue_protected). Unknown types are rejected with the allowed list.
    - occurred_at: ISO date of the DECISION (signature, cancellation notice,
      PO), not of this call.
    - revenue: the movement in currency; losses may be given positive, they
      are stored negative.
    - linked_signal_ids: the signals that preceded it (signal ids, source refs
      or node ids) — become LED_TO edges the backtest and canvas read.
    - Logging the same decision twice returns 'exists'.

    Args:
        customer_id: The customer ID
        account_id: The account ID
        outcome_type: Taxonomy outcome subtype
        occurred_at: ISO decision date
        revenue: Revenue movement (optional)
        note: Why / evidence (optional, stored)
        linked_signal_ids: Preceding signals to link (optional)
        decided_by: Who recorded the decision (optional)
        source_type: manual | crm_activity | external (default manual)
        source_ref: Reference in the source system (contract id, opportunity id) (optional)
    """
    _require_auth_if_key_present('log_outcome', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from journeys.outcomes import log_outcome as _lo
        try:
            return _lo(customer_id, account_id, outcome_type, occurred_at, revenue=revenue, note=note,
                       linked_signal_ids=linked_signal_ids, decided_by=decided_by, source_type=source_type, source_ref=source_ref, title=title, use_case=use_case)
        except ValueError as e:
            raise ToolError(str(e))


# ===================================================================
# Ask AI over the journey contract (P10)
# ===================================================================

@mcp.tool
def ask(customer_id: int, question: str, account_id: int = None, as_of: str = None) -> dict:
    """Ask a question over the journey contract — one account's cited
    narrative, journey, episodes and evidence, or the portfolio rows — and
    get an answer where every sentence cites the episode / evidence node /
    portfolio row it was built from. Sentences the evidence cannot back are
    dropped and listed under `unsupported`; what the evidence could not say
    is under `evidence_gaps`; numbers are read from the blocks, never
    computed. Without ANTHROPIC_API_KEY a deterministic stub answers from
    the narrative block.

    Args:
        customer_id: The customer ID
        question: The question, e.g. "why did health fall in March?" or "which accounts are most at risk?"
        account_id: One account (optional; otherwise an account named in the question, else the portfolio)
        as_of: ISO date/time — answer as of that instant only (scrubber semantics; optional)
    """
    _require_auth_if_key_present('ask', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from ask_ai.answer import ask as _ask
        try:
            return _ask(customer_id, question, account_id=account_id, as_of=as_of)
        except (LookupError, ValueError) as e:
            raise ToolError(str(e))


# ===================================================================
# Data origin (disclosure) + bulk communications import
# ===================================================================

@mcp.tool
def declare_data_origin(customer_id: int, data_origin: str, reason: str) -> dict:
    """Change a tenant's declared data origin ('real' | 'synthetic_demo' |
    'synthetic_replay' | 'synthetic_test'). Audited with the reason; the
    disclosure on every surface changes immediately. Use it when a tenant
    that started as a demo begins receiving its own data — never to hide
    where data came from.

    Args:
        customer_id: The customer ID
        data_origin: The new origin
        reason: Why (stored in the audit log)
    """
    _require_auth_if_key_present('declare_data_origin', customer_id)
    _check_mcp_enabled()
    from utils.data_origin import validate as _validate_origin, block as _block
    try:
        new = _validate_origin(data_origin)
    except ValueError as e:
        raise ToolError(str(e))
    if not (reason or '').strip():
        raise ToolError('reason is required')
    app = _get_flask_app()
    with app.app_context():
        from models import Customer
        from extensions import db
        from mcp_server import audit as _audit
        c = db.session.get(Customer, int(customer_id))
        if not c:
            raise ToolError(f'Customer {customer_id} not found.')
        old = c.data_origin
        c.data_origin = new
        db.session.commit()
        _audit.record('mcp', 'declare_data_origin', customer_id, key_kind='n/a', outcome='allowed',
                      detail=f'{old} -> {new}: {reason.strip()[:200]}')
        return {'customer_id': customer_id, 'previous': old, **_block(c), 'reason': reason.strip()}


@mcp.tool
def import_communications(customer_id: int, communications: list, process_now: bool = True) -> dict:
    """Bulk-import raw communications (the file a customer or the generator
    exports: one object per communication) through the signal engine — the
    same path as submit_signal, in batches.

    Each item: {"source_account_id" | "account_id" | "account_name", "source_type",
    "text", "occurred_at", "participants": [{"name","role"}]?, "source_ref"?,
    "signal_type"? (a taxonomy subtype for the structured path), "consent_verified"?}.
    Accounts are resolved by external id, then id, then name. Exact duplicates
    are reported, unknown accounts are listed, nothing is dropped silently.

    Args:
        customer_id: The customer ID
        communications: List of communication objects (≤ 500 per call)
        process_now: Classify + write evidence + rebuild journeys now (default true)
    """
    _require_auth_if_key_present('import_communications', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from signal_engine.pipeline import import_communications as _imp
        try:
            return _imp(int(customer_id), communications, process_now=process_now)
        except ValueError as e:
            raise ToolError(str(e))


# ===================================================================
# Customer extensions: the per-tenant column map
# ===================================================================

@mcp.tool
def configure_column_map(customer_id: int, file_type: str, mapping: dict, replace: bool = False) -> dict:
    """Map a customer's own column names to ours for one file, applied at every upload
    (their view can keep its names). Keys are their columns (or 'attributes.<col>' to
    promote an extension into a field we read); values are our column names for that
    file. Unknown target names are refused. Audited.

    Args:
        customer_id: The customer ID
        file_type: account_details.csv | kpi_measurements.csv | enhanced_qualitative_signals.csv | outcomes.csv (aliases accepted)
        mapping: {"their_column": "our_column", ...}
        replace: Replace the file's whole map instead of merging (default merge)
    """
    _require_auth_if_key_present('configure_column_map', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import CustomerConfig
        from extensions import db
        from utils.csv_upload import resolve_file_type, known_columns
        from mcp_server import audit as _audit
        try:
            info = resolve_file_type(file_type)
        except ValueError as e:
            raise ToolError(str(e))
        ours = known_columns(info.canonical_filename)
        bad = [v for v in (mapping or {}).values() if v not in ours]
        if bad:
            raise ToolError(f'not columns of {info.canonical_filename}: {bad}; allowed: {sorted(ours)}')
        cc = CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()
        if not cc:
            raise ToolError(f'Customer {customer_id} not found.')
        cmap = dict(cc.column_map or {})
        current = {} if replace else dict(cmap.get(info.canonical_filename) or {})
        current.update({str(k): str(v) for k, v in (mapping or {}).items()})
        cmap[info.canonical_filename] = current
        cc.column_map = cmap
        db.session.commit()
        _audit.record('mcp', 'configure_column_map', customer_id, key_kind='n/a', outcome='allowed',
                      detail=f'{info.canonical_filename}: {current}'[:400])
        return {'customer_id': customer_id, 'file_type': info.canonical_filename, 'column_map': current}


@mcp.tool
def get_column_map(customer_id: int) -> dict:
    """The tenant's column maps per file, and the column names each file accepts.

    Args:
        customer_id: The customer ID
    """
    _require_auth_if_key_present('get_column_map', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import CustomerConfig
        from utils.csv_upload import known_columns
        cc = CustomerConfig.query.filter_by(customer_id=int(customer_id)).first()
        files = ['account_details.csv', 'kpi_measurements.csv', 'enhanced_qualitative_signals.csv', 'outcomes.csv']
        return {'customer_id': customer_id, 'column_map': (cc.column_map if cc else None) or {},
                'accepted_columns': {f: sorted(known_columns(f)) for f in files}}


# ===================================================================
# Tenant removal (admin) — server key only, explicit confirmation, audited
# ===================================================================

@mcp.tool
def delete_customer(customer_id: int, confirm_domain: str, reason: str) -> dict:
    """Remove a tenant and everything it owns (accounts, KPI rows, health scores,
    signals, evidence nodes and edges, journeys, uploads, runs, reviews, keys,
    users, config, toggles). Irreversible. Requires the SERVER key and the
    tenant's domain typed back as confirmation; the deletion and its reason
    are the last audit rows for that customer.

    Args:
        customer_id: The customer ID
        confirm_domain: The tenant's domain, exactly (refuses otherwise)
        reason: Why (stored in the audit log)
    """
    _require_auth_if_key_present('delete_customer', customer_id)
    _check_mcp_enabled()
    from mcp_server.auth import extract_api_key, validate_server_key, MCP_AUTH_REQUIRED
    raw = extract_api_key()
    if os.environ.get('MCP_TRANSPORT', 'stdio') == 'http' and MCP_AUTH_REQUIRED and not (raw and validate_server_key(raw)):
        raise ToolError('delete_customer requires the server key')
    if not (reason or '').strip():
        raise ToolError('reason is required')
    app = _get_flask_app()
    with app.app_context():
        from extensions import db
        from models import (Customer, CustomerConfig, User, Account, KPIMeasurement, HealthScore, QualitativeSignal,
                            ContextNode, ContextEdge, JourneyData, CsvUpload, CsvUploadStaging, ProcessRun, SignalReview,
                            WizardRun, CustomerApiKey, FeatureToggle, Intervention, ForecastRun, AccountForecast, WeightCalibration)
        from mcp_server import audit as _audit
        c = db.session.get(Customer, int(customer_id))
        if not c:
            raise ToolError(f'Customer {customer_id} not found.')
        if (confirm_domain or '').strip().lower() != (c.domain or '').lower():
            raise ToolError(f"confirm_domain must equal the tenant's domain ({c.domain!r})")
        acct_ids = [a.account_id for a in Account.query.filter_by(customer_id=c.customer_id).all()]
        counts = {}
        def _del(model, q):
            n = q.delete(synchronize_session=False)
            counts[model.__tablename__] = counts.get(model.__tablename__, 0) + int(n or 0)
        _del(Intervention, Intervention.query.filter_by(customer_id=c.customer_id))
        _del(AccountForecast, AccountForecast.query.filter_by(customer_id=c.customer_id))
        _del(ForecastRun, ForecastRun.query.filter_by(customer_id=c.customer_id))
        _del(WeightCalibration, WeightCalibration.query.filter_by(customer_id=c.customer_id))
        _del(ContextEdge, ContextEdge.query.filter_by(customer_id=c.customer_id))
        _del(ContextNode, ContextNode.query.filter_by(customer_id=c.customer_id))
        _del(JourneyData, JourneyData.query.filter_by(customer_id=c.customer_id))
        _del(SignalReview, SignalReview.query.filter_by(customer_id=c.customer_id))
        _del(QualitativeSignal, QualitativeSignal.query.filter_by(customer_id=c.customer_id))
        if acct_ids:
            _del(HealthScore, HealthScore.query.filter(HealthScore.account_id.in_(acct_ids)))
            _del(KPIMeasurement, KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(acct_ids)))
        _del(WizardRun, WizardRun.query.filter_by(customer_id=c.customer_id))
        _del(ProcessRun, ProcessRun.query.filter_by(customer_id=c.customer_id))
        _del(CsvUploadStaging, CsvUploadStaging.query.filter_by(customer_id=c.customer_id))
        _del(CsvUpload, CsvUpload.query.filter_by(customer_id=c.customer_id))
        # the LLM spend ledger references the tenant: its totals go into the audit detail, then the rows go
        from sqlalchemy import text as _text, func as _func
        usage_totals = dict(db.session.execute(_text(
            'select count(*), coalesce(sum(tokens_in),0), coalesce(sum(tokens_out),0) from llm_usage_log where customer_id=:cid'),
            {'cid': c.customer_id}).first()._mapping) if db.session.execute(_text(
            "select 1 from information_schema.tables where table_name='llm_usage_log'")).first() else {}
        if usage_totals:
            n = db.session.execute(_text('delete from llm_usage_log where customer_id=:cid'), {'cid': c.customer_id}).rowcount
            counts['llm_usage_log'] = int(n or 0)
        _del(FeatureToggle, FeatureToggle.query.filter_by(customer_id=c.customer_id))
        _del(CustomerApiKey, CustomerApiKey.query.filter_by(customer_id=c.customer_id))
        _del(Account, Account.query.filter_by(customer_id=c.customer_id))
        _del(User, User.query.filter_by(customer_id=c.customer_id))
        _del(CustomerConfig, CustomerConfig.query.filter_by(customer_id=c.customer_id))
        name, domain = c.customer_name, c.domain
        db.session.delete(c)
        db.session.commit()
        _audit.record('mcp', 'delete_customer', customer_id, key_kind='server' if raw else 'local', outcome='allowed',
                      detail=f'{name} ({domain}) deleted: {reason.strip()[:200]} — rows {counts} — llm usage {usage_totals}')
        return {'customer_id': customer_id, 'customer_name': name, 'domain': domain, 'deleted_rows': counts, 'reason': reason.strip()}


# ===================================================================
# Tool: clone_customer — deep-copy a tenant (core primitive only)
# ===================================================================
#
# Future use case (not wired up here): a new visitor on a marketing site
# gets a temporary sandbox copy of a demo tenant to explore. This function
# is only the copy primitive — nothing here adds it to any HTTP/auth/signup
# flow, and nothing sweeps Customer.expires_at once it passes (no
# scheduler/cron/Celery exists anywhere in this codebase, confirmed absent
# from requirements.txt); both are separate, later work. clone_customer
# accepts ttl_minutes and stamps the deadline column — that is the whole of
# its involvement in "temporary."
#
# Table list: the forward/copy version of delete_customer's own list — "what
# a customer owns" — Intervention, AccountForecast, ForecastRun,
# WeightCalibration, ContextEdge, ContextNode, JourneyData, SignalReview,
# QualitativeSignal, HealthScore, KPIMeasurement, WizardRun, ProcessRun,
# CsvUploadStaging, CsvUpload, FeatureToggle, Account, CustomerConfig,
# Customer — with exclusions matching what create_customer already does for
# a brand-new tenant, not what delete_customer deletes:
#   - CustomerApiKey / llm_usage_log: never copied. The clone gets ONE fresh
#     key (api_key_service, the same call create_customer makes) and an
#     empty spend ledger, never the source's.
#   - User: never copied verbatim (User.email has a global UniqueConstraint).
#     Exactly one fresh admin User is minted, same shape as create_customer's.
#   - CustomerConfig.openai_api_key_encrypted / _updated_at: the one
#     CustomerConfig field that is itself a live credential (a customer's
#     own OpenAI key) — never carried into a clone even though the rest of
#     CustomerConfig is copied, for the same reason as CustomerApiKey.
#
# Two more tenant-scoped secrets/integration targets live inside
# FeatureToggle rows (found while reading playbooks/definitions.py and
# configure_signal_engine, not mentioned in the original scoping notes): the
# 'playbooks' toggle's config can hold a real webhook_url + webhook_secret +
# slack_webhook_url, and the 'signal_engine' toggle's config can hold a real
# Slack workspace/channel map. Both are stripped on clone — otherwise a demo
# clone could silently fire signed webhooks at the source tenant's real
# n8n/Salesforce endpoint, or receive the source's live Slack signal traffic.
#
# ID remapping: two-pass, same shape as the old repo's clone_customer
# (accounts first, building an old-id->new-id map, before anything that
# references those ids) but generalized to every id space this schema
# actually has, verified against real rows in a populated dev database
# (customer 5 in customerintel_aurelia_dev, a live 6-account demo tenant —
# not assumed) rather than guessed from the models alone:
#   - Account.account_id, ContextNode.node_id, ContextEdge.edge_id,
#     HealthScore.health_score_id, CsvUpload.id, ProcessRun.id and
#     Intervention.id each get their own old->new map.
#   - ForecastRun.run_id / ProcessRun.run_id / WizardRun.run_id are unique
#     STRINGS, not surrogate ints; each cloned row gets a freshly minted
#     sibling id (_mint_sibling_run_id), and old->new STRING maps are built
#     for all three since they turned out to be embedded elsewhere (a
#     forecast run_id inside journey_json['forecast'] and
#     AccountForecast.forecast_json; a process run_id inside
#     WeightCalibration.recompute).
#   - JSON blobs DO embed old ids internally, confirmed by inspecting real
#     rows rather than assumed: ContextNode.properties for an INTERVENTION
#     node carries an intervention_id and trigger_episode_ids;
#     JourneyData.journey_json's episodes/phases/arc/narrative/forecast all
#     cite "{prefix}:{id}" episode-id strings — 'sig'/'dec'/'int'/'out' key
#     off ContextNode.node_id, 'hs' keys off HealthScore.health_score_id,
#     per journeys/journey_builder.py's Episode construction —
#     AccountForecast.forecast_json['cites'] and WeightCalibration.impact's
#     per-account rows carry ids the same way. _remap_deep walks every JSON
#     blob column (not just the two the scoping investigation named) and
#     rewrites every id it recognizes, by dict key and by the "{prefix}:{id}"
#     string pattern; anything not in its allowlist passes through
#     unchanged. It is applied uniformly to every JSON column on every
#     cloned table, not selectively, so nothing gets missed by omission.
#
# Frictionless, but fail-closed on the source: clone_customer is one of the
# ONBOARDING_TOOLS (no key required over HTTP — see onboarding_tool_registry
# .py, where the name was already reserved), matching the stated future use
# case of an anonymous visitor cloning a DEMO tenant. That would be a
# serious hole if it could clone ANY tenant — an anonymous, unauthenticated
# caller could otherwise exfiltrate a full copy of a real paying customer's
# accounts/signals/health/interventions into a tenant they control. So the
# source's data_origin must already be synthetic (utils.data_origin
# .is_synthetic) or clone_customer refuses, unconditionally — including with
# the server key. This gate isn't spelled out verbatim in the brief; it's
# the same judgment call create_customer/delete_customer already make about
# who can touch what, applied to a new frictionless tool that deep-reads a
# whole tenant. When a customer API key IS presented, require_auth_if_key
# _present is called with source_customer_id (not None, unlike
# create_customer, which has no existing tenant to scope against) so the
# key must be scoped to the tenant being cloned; clone_customer is also in
# auth.WRITE_TOOLS so that key must carry write scope, same as
# upload_csv/process_data/trigger_wizard (also frictionless AND mutating).

_EPISODE_ID_RE = re.compile(r'^(sig|dec|int|out|hs):(\d+)$')
_INTERVENTION_REF_RE = re.compile(r'^intervention:(\d+)$')

# Dict keys recognized as a single embedded id, and which bucket of `maps`
# (passed to _remap_deep) resolves them.
_SCALAR_ID_KEYS = {
    'node_id': 'node', 'outcome_node_id': 'node', 'cg_node_id': 'node',
    'intervention_id': 'intervention',
    'account_id': 'account',
    'health_score_id': 'health_score',
    'upload_id': 'csv_upload', 'input_upload_id': 'csv_upload',
    'process_run_id': 'process_run_pk',
}
# Dict keys recognized as a LIST of embedded ids of one kind.
_LIST_ID_KEYS = {
    'evidence_node_ids': 'node', 'trigger_node_ids': 'node', 'node_ids': 'node',
    'outcome_node_ids': 'node', 'upload_ids': 'csv_upload',
}


def _remap_scalar_str(s: str, maps: dict) -> str:
    """Rewrite one string if it is an embedded id in a recognized micro-format:
    journeys/journey_builder.py's episode ids ('sig:419', 'hs:88', ...) and
    playbooks/governance.py's ContextNode.source_event_id ('intervention:12').
    Anything else (free text, a signal_id UUID, an external CRM ref) is
    returned unchanged — both regexes anchor start-to-end, so a quote or
    title that merely contains a colon never matches."""
    m = _EPISODE_ID_RE.match(s)
    if m:
        prefix, old = m.group(1), int(m.group(2))
        bucket = maps['health_score'] if prefix == 'hs' else maps['node']
        new = bucket.get(old)
        return f'{prefix}:{new}' if new is not None else s
    m = _INTERVENTION_REF_RE.match(s)
    if m:
        new = maps['intervention'].get(int(m.group(1)))
        return f'intervention:{new}' if new is not None else s
    return s


def _remap_deep(obj, maps: dict, run_id_maps: list):
    """Recursively rewrite embedded old ids to new ones inside a JSON blob.

    `maps` is {'node', 'account', 'health_score', 'intervention', 'csv_upload',
    'process_run_pk'} -> {old_id: new_id}. A bucket not yet populated (its
    table hasn't been cloned yet at the point this runs) just means its keys
    pass through unchanged — safe to call before every map is complete, as
    long as the CALLER only relies on buckets it knows are already done.

    `run_id_maps` is a list of {old_run_id_str: new_run_id_str} dicts
    (forecast / process / wizard run ids); a 'run_id' string is looked up
    across all of them since more than one run-id space can appear in the
    same blob shape (e.g. journey_json['forecast']['run_id'] is a
    ForecastRun id, WeightCalibration.recompute['run_id'] is a ProcessRun id).

    Every other key/value is copied through unchanged — this is deliberately
    a broad allowlist walk, not a narrow one, so it is safe to run over every
    JSON column on every cloned table rather than only the ones known in
    advance to need it.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SCALAR_ID_KEYS and isinstance(v, int) and not isinstance(v, bool):
                out[k] = maps[_SCALAR_ID_KEYS[k]].get(v, v)
            elif k in _LIST_ID_KEYS and isinstance(v, list):
                bucket = maps[_LIST_ID_KEYS[k]]
                out[k] = [bucket.get(x, x) if (isinstance(x, int) and not isinstance(x, bool))
                          else _remap_deep(x, maps, run_id_maps) for x in v]
            elif k == 'run_id' and isinstance(v, str):
                out[k] = next((m[v] for m in run_id_maps if v in m), v)
            else:
                out[k] = _remap_deep(v, maps, run_id_maps)
        return out
    if isinstance(obj, list):
        return [_remap_deep(x, maps, run_id_maps) for x in obj]
    if isinstance(obj, str):
        return _remap_scalar_str(obj, maps)
    return obj


def _remap_int_list(values, bucket_map: dict) -> list:
    """For a JSON column whose OWN value is a bare list of ids (e.g.
    Intervention.trigger_node_ids) — _remap_deep only rewrites a list found
    nested under a recognized dict key, not a column's top-level value."""
    return [bucket_map.get(v, v) if (isinstance(v, int) and not isinstance(v, bool)) else v for v in (values or [])]


def _remap_episode_str_list(values, maps: dict) -> list:
    """Same as _remap_int_list, for a bare list of episode-id strings
    (Intervention.trigger_episode_ids)."""
    return [_remap_scalar_str(v, maps) if isinstance(v, str) else v for v in (values or [])]


def _mint_sibling_run_id(old_run_id: Optional[str], max_len: int) -> str:
    """A fresh, all-but-certainly-unique id in the same family as old_run_id.
    ForecastRun/ProcessRun/WizardRun.run_id are each globally unique strings,
    so a literal copy would violate the unique constraint."""
    suffix = f'_cl{secrets.token_hex(4)}'
    base = (old_run_id or 'run')[:max(0, max_len - len(suffix))]
    return f'{base}{suffix}'[:max_len]


@mcp.tool
def clone_customer(source_customer_id: int, name: str, domain: str, admin_email: str = None,
                   admin_name: str = None, ttl_minutes: int = None) -> dict:
    """Deep-copy a tenant into a brand-new one: every account, KPI row, health
    score, signal, evidence node/edge, journey, intervention, forecast,
    calibration, and upload/process/feature-toggle record the source owns,
    all re-keyed to new ids under a new Customer with its own fresh admin
    user and API key. This is the copy-forward mirror of delete_customer's
    table list (see that tool's docstring) minus what a clone must never
    inherit: the source's CustomerApiKey rows, its LLM usage ledger, its
    users, and any live credential/external-integration target found along
    the way (CustomerConfig's OpenAI key; a playbooks webhook secret/URL; a
    signal_engine Slack workspace/channel map). Core primitive only — not
    wired into any onboarding/HTTP/signup flow, and nothing sweeps
    expires_at once it passes (no scheduler exists in this codebase).

    The source must already be a synthetic/demo tenant
    (utils.data_origin.is_synthetic) — this refuses to clone a tenant whose
    data_origin is 'real', unconditionally (even with the server key). This
    tool is frictionless (ONBOARDING_TOOLS, no key required over HTTP) for
    the same reason create_customer is: it is meant to be reachable by an
    anonymous prospect. Without the is_synthetic gate that would let anyone
    exfiltrate a full copy of a real customer's data; with it, cloning is
    limited to exactly the case this exists for — handing out sandbox
    copies of demo tenants. If a customer API key IS presented, it must be
    scoped to source_customer_id and carry write scope (auth.WRITE_TOOLS).

    Args:
        source_customer_id: The demo/synthetic tenant to copy.
        name: Company name for the new (cloned) customer.
        domain: Email domain for the new customer (e.g. 'acme-clone-4f2a.demo'). Must be unused.
        admin_email: Admin user email for the clone. Defaults to f'admin@{domain}'.
        admin_name: Admin user display name for the clone. Defaults to f'{name} Admin'.
        ttl_minutes: Optional. When given, sets Customer.expires_at = now + ttl_minutes
            (nothing acts on this yet — see this tool's module-level comment). Omit for
            a normal, non-expiring customer (expires_at stays NULL, same as every
            existing tenant).
    """
    _require_auth_if_key_present('clone_customer', int(source_customer_id))
    _check_mcp_enabled()
    app = _get_flask_app()

    with app.app_context():
        from extensions import db
        from models import (Customer, CustomerConfig, User, Account, KPIMeasurement, HealthScore, QualitativeSignal,
                            ContextNode, ContextEdge, JourneyData, CsvUpload, CsvUploadStaging, ProcessRun, SignalReview,
                            WizardRun, FeatureToggle, Intervention, ForecastRun, AccountForecast, WeightCalibration)
        from utils.data_origin import is_synthetic, disclosure as _disclosure

        src = db.session.get(Customer, int(source_customer_id))
        if not src:
            raise ToolError(f'Customer {source_customer_id} not found.')
        if not is_synthetic(src.data_origin):
            raise ToolError(
                f"clone_customer refuses to clone customer {source_customer_id} ({src.domain!r}): "
                f"data_origin={src.data_origin!r} is not synthetic. Only demo/synthetic tenants can be "
                f"cloned by this frictionless tool."
            )
        existing = Customer.query.filter_by(domain=domain).first()
        if existing:
            raise ToolError(f"A customer with domain '{domain}' already exists (customer_id={existing.customer_id}).")

        admin_email = (admin_email or f'admin@{domain}').strip()
        admin_name = (admin_name or f'{name} Admin').strip()
        if User.query.filter_by(email=admin_email).first():
            raise ToolError(f"Email '{admin_email}' is already registered.")

        vertical = src.vertical
        uuid_vertical = 'dc' if (vertical or '').startswith('dc') else vertical

        def _try_gen_id(entity: str):
            try:
                from id_generator import generate_id
                return generate_id(uuid_vertical, entity)
            except Exception:
                return None

        customer_uuid = _try_gen_id('customer')

        expires_at = None
        if ttl_minutes is not None:
            expires_at = datetime.utcnow() + timedelta(minutes=int(ttl_minutes))

        customer = Customer(
            customer_name=name, email=admin_email, domain=domain, vertical=vertical,
            data_origin='synthetic_demo', uuid=customer_uuid, expires_at=expires_at,
        )
        db.session.add(customer)
        db.session.flush()
        new_customer_id = customer.customer_id
        counts = {}

        # ---- CustomerConfig (openai_api_key_* deliberately not copied — a live credential) ----
        src_config = CustomerConfig.query.filter_by(customer_id=source_customer_id).first()
        config = CustomerConfig(customer_id=new_customer_id, vertical=vertical)
        if src_config:
            for field in ('kpi_upload_mode', 'column_map', 'category_weights', 'master_file_name',
                         'pillar_weights', 'enabled_kpis', 'kpi_overrides', 'kpi_weights',
                         'kpi_definitions', 'lifecycle_stage_weights', 'nomenclature_overrides',
                         'config_version', 'customized_by', 'weights_origin'):
                setattr(config, field, getattr(src_config, field))
        db.session.add(config)
        counts['customer_configs'] = 1

        # ---- Admin user: minted, never copied (User.email is globally unique) ----
        user = User(
            customer_id=new_customer_id, user_name=admin_name, email=admin_email, role='admin',
            vertical=vertical, allowed_customer_ids=[new_customer_id],
        )
        if customer_uuid:
            user.customer_uuid = customer_uuid
        user.uuid = _try_gen_id('user')
        db.session.add(user)
        db.session.flush()
        counts['users'] = 1

        from app_api.auth import issue_setup_token
        setup_token = issue_setup_token(user)

        # ---- Fresh API key (CustomerApiKey rows are never copied) ----
        full_key = None
        try:
            from api_key_service import generate_api_key as _gen_api_key
            full_key, _key_record = _gen_api_key(
                customer_id=new_customer_id, created_by=user.user_id,
                name='Clone Onboarding Key', scopes=['read', 'write'],
            )
        except Exception:
            pass

        # ---- FeatureToggle: copy, but strip live external-integration secrets/targets ----
        n = 0
        for t in FeatureToggle.query.filter_by(customer_id=source_customer_id).all():
            cfg = dict(t.config or {})
            if t.feature_name == 'playbooks':
                # webhook_url/webhook_secret/slack_webhook_url are the SOURCE tenant's real,
                # signed webhook target (playbooks/definitions.py tenant_secret/tenant_slack_url)
                # — never carried into a clone.
                for k in ('webhook_url', 'webhook_secret', 'slack_webhook_url'):
                    cfg.pop(k, None)
            elif t.feature_name == 'signal_engine':
                # slack_team_id/slack_channel_map route a REAL Slack workspace's events to this
                # tenant (configure_signal_engine) — copying them would route the source's live
                # Slack traffic into the clone too.
                cfg = {}
            db.session.add(FeatureToggle(
                customer_id=new_customer_id, feature_name=t.feature_name, enabled=t.enabled,
                config=cfg, description=t.description,
            ))
            n += 1
        counts['feature_toggles'] = n

        # id maps, filled in as each phase below completes. _remap_deep / _remap_scalar_str
        # do a plain dict.get(x, x) lookup, so calling them before a bucket is populated is
        # safe (those ids just pass through) — callers below only rely on buckets already done.
        maps = {'node': {}, 'account': {}, 'health_score': {}, 'intervention': {},
               'csv_upload': {}, 'process_run_pk': {}}
        run_id_maps = []

        # ---- Accounts -> account_map ----
        account_map = {}
        for a in Account.query.filter_by(customer_id=source_customer_id).order_by(Account.account_id).all():
            new_a = Account(
                customer_id=new_customer_id, account_name=a.account_name, revenue=a.revenue,
                account_status=a.account_status, industry=a.industry, vertical=a.vertical,
                region=a.region, external_account_id=a.external_account_id,
                profile_metadata=a.profile_metadata, arc_type=a.arc_type, arc_phase=a.arc_phase,
                arc_confidence=a.arc_confidence,
            )
            if a.uuid:
                # Account.uuid is globally unique when set; normal CSV ingest (utils/csv_ingest.py)
                # never sets it in practice, so this only fires on the rare pre-existing row that has one.
                new_a.uuid = _try_gen_id('account')
                new_a.customer_uuid = customer_uuid
            db.session.add(new_a)
            db.session.flush()
            account_map[a.account_id] = new_a.account_id
        maps['account'] = account_map
        counts['accounts'] = len(account_map)
        _acct_ids_or_none = list(account_map.keys()) or [-1]

        # ---- ContextNode -> node_map (properties/source_event_id fixed up later, once
        #      the intervention_map they can reference exists) ----
        node_map, node_pairs = {}, []
        for cn in ContextNode.query.filter_by(customer_id=source_customer_id).order_by(ContextNode.node_id).all():
            new_cn = ContextNode(
                customer_id=new_customer_id, account_id=account_map[cn.account_id],
                node_type=cn.node_type, node_subtype=cn.node_subtype, source=cn.source, tier=cn.tier,
                title=cn.title, properties=cn.properties, revenue_impact=cn.revenue_impact,
                revenue_impact_type=cn.revenue_impact_type, confidence=cn.confidence,
                source_platform=cn.source_platform, source_event_id=cn.source_event_id,
                source_ref=cn.source_ref, occurred_at=cn.occurred_at, expires_at=cn.expires_at,
                weight_decay=cn.weight_decay,
            )
            db.session.add(new_cn)
            db.session.flush()
            node_map[cn.node_id] = new_cn.node_id
            node_pairs.append((cn, new_cn))
        maps['node'] = node_map
        counts['context_nodes'] = len(node_map)

        # ---- ContextEdge -> edge_map (from/to remapped now; properties fixed up later
        #      alongside ContextNode's; superseded_by fixed up right below, once every
        #      edge in this batch has a new id) ----
        edge_map, edge_pairs = {}, []
        for ce in ContextEdge.query.filter_by(customer_id=source_customer_id).order_by(ContextEdge.edge_id).all():
            new_ce = ContextEdge(
                customer_id=new_customer_id, from_node_id=node_map[ce.from_node_id],
                to_node_id=node_map[ce.to_node_id], edge_type=ce.edge_type, lag_days=ce.lag_days,
                weight=ce.weight, confidence=ce.confidence, revenue_impact=ce.revenue_impact,
                revenue_impact_type=ce.revenue_impact_type, properties=ce.properties,
                source_platform=ce.source_platform, created_by=ce.created_by,
                occurred_at=ce.occurred_at, expires_at=ce.expires_at,
            )
            db.session.add(new_ce)
            db.session.flush()
            edge_map[ce.edge_id] = new_ce.edge_id
            edge_pairs.append((ce, new_ce))
        for ce, new_ce in edge_pairs:
            if ce.superseded_by is not None:
                new_ce.superseded_by = edge_map.get(ce.superseded_by)
        counts['context_edges'] = len(edge_map)

        # ---- ProcessRun -> process_run_pk_map / process_run_id_map (upload_ids fixed
        #      up below once csv_upload_map exists) ----
        process_run_pk_map, process_run_id_map, process_run_pairs = {}, {}, []
        for pr in ProcessRun.query.filter_by(customer_id=source_customer_id).order_by(ProcessRun.id).all():
            new_run_id = _mint_sibling_run_id(pr.run_id, 40)
            new_pr = ProcessRun(
                run_id=new_run_id, customer_id=new_customer_id, vertical=pr.vertical, mode=pr.mode,
                status=pr.status, steps=pr.steps, errors=pr.errors, timings=pr.timings, counts=pr.counts,
                upload_ids=pr.upload_ids,   # fixed up below, once csv_upload_map exists
                key_kind=pr.key_kind, key_id=(pr.key_id if pr.key_kind != 'customer' else None),
                generator_version=pr.generator_version, started_at=pr.started_at, finished_at=pr.finished_at,
            )
            db.session.add(new_pr)
            db.session.flush()
            process_run_pk_map[pr.id] = new_pr.id
            process_run_id_map[pr.run_id] = new_run_id
            process_run_pairs.append((pr, new_pr))
        maps['process_run_pk'] = process_run_pk_map
        run_id_maps.append(process_run_id_map)
        counts['process_runs'] = len(process_run_pk_map)

        # ---- CsvUpload -> csv_upload_map (process_run_pk_map already exists) ----
        csv_upload_map = {}
        for cu in CsvUpload.query.filter_by(customer_id=source_customer_id).order_by(CsvUpload.id).all():
            new_cu = CsvUpload(
                customer_id=new_customer_id, file_type=cu.file_type, sha256=cu.sha256,
                row_count=cu.row_count, byte_count=cu.byte_count, validation=cu.validation,
                key_kind=cu.key_kind, key_id=(cu.key_id if cu.key_kind != 'customer' else None),
                uploaded_at=cu.uploaded_at, consumed_at=cu.consumed_at,
                process_run_id=(process_run_pk_map.get(cu.process_run_id) if cu.process_run_id is not None else None),
            )
            db.session.add(new_cu)
            db.session.flush()
            csv_upload_map[cu.id] = new_cu.id
        maps['csv_upload'] = csv_upload_map
        counts['csv_uploads'] = len(csv_upload_map)

        # ---- Fixup: ProcessRun.upload_ids, now that csv_upload_map exists ----
        for pr, new_pr in process_run_pairs:
            new_pr.upload_ids = _remap_int_list(pr.upload_ids, csv_upload_map)

        # ---- HealthScore -> health_score_map (account_map / csv_upload_map / process_run_pk_map all ready) ----
        health_score_map = {}
        for hs in HealthScore.query.filter(HealthScore.account_id.in_(_acct_ids_or_none)).order_by(HealthScore.health_score_id).all():
            new_hs = HealthScore(
                account_id=account_map[hs.account_id], measurement_month=hs.measurement_month,
                health_score=hs.health_score, health_status=hs.health_status, trend=hs.trend,
                change_from_last_month=hs.change_from_last_month, kpi_only_score=hs.kpi_only_score,
                composite_score=hs.composite_score, qual_score=hs.qual_score, divergence=hs.divergence,
                early_warning=hs.early_warning, contributing_pillars=hs.contributing_pillars,
                pillar_weights=hs.pillar_weights, kpi_weights=hs.kpi_weights,
                kpi_codes_used=hs.kpi_codes_used, kpi_codes_dropped=hs.kpi_codes_dropped,
                weight_source=hs.weight_source, catalog_version=hs.catalog_version,
                taxonomy_version=hs.taxonomy_version, scorer_version=hs.scorer_version,
                input_upload_id=(csv_upload_map.get(hs.input_upload_id) if hs.input_upload_id is not None else None),
                process_run_id=(process_run_pk_map.get(hs.process_run_id) if hs.process_run_id is not None else None),
                calculated_at=hs.calculated_at,
            )
            db.session.add(new_hs)
            db.session.flush()
            health_score_map[hs.health_score_id] = new_hs.health_score_id
        maps['health_score'] = health_score_map
        counts['health_scores'] = len(health_score_map)

        # ---- CsvUploadStaging ----
        n = 0
        for st in CsvUploadStaging.query.filter_by(customer_id=source_customer_id).all():
            db.session.add(CsvUploadStaging(
                customer_id=new_customer_id, file_type=st.file_type, csv_content=st.csv_content,
                row_count=st.row_count,
                upload_id=(csv_upload_map.get(st.upload_id) if st.upload_id is not None else None),
                uploaded_at=st.uploaded_at, updated_at=st.updated_at,
            ))
            n += 1
        counts['csv_upload_staging'] = n

        # ---- Intervention -> intervention_map (node_map ready; trigger_key recomputed
        #      from the remapped episode ids so the row stays internally consistent) ----
        intervention_map = {}
        for iv in Intervention.query.filter_by(customer_id=source_customer_id).order_by(Intervention.id).all():
            new_trigger_episode_ids = _remap_episode_str_list(iv.trigger_episode_ids, maps)
            new_iv = Intervention(
                customer_id=new_customer_id, account_id=account_map[iv.account_id],
                playbook_id=iv.playbook_id, playbook_version=iv.playbook_version,
                action_class=iv.action_class, approval_mode=iv.approval_mode, state=iv.state,
                urgency=iv.urgency,
                trigger_key=hashlib.sha256(','.join(sorted(new_trigger_episode_ids)).encode('utf-8')).hexdigest(),
                trigger_episode_ids=new_trigger_episode_ids,
                trigger_node_ids=_remap_int_list(iv.trigger_node_ids, maps['node']),
                trigger_roles=iv.trigger_roles, trigger_quote=iv.trigger_quote,
                evaluated_as_of=iv.evaluated_as_of, expected_outcome_types=iv.expected_outcome_types,
                expected_window_days=iv.expected_window_days, exposure_revenue=iv.exposure_revenue,
                proposed_at=iv.proposed_at, proposed_by=iv.proposed_by,
                approved_at=iv.approved_at, approved_by=iv.approved_by,
                approved_by_key_id=None,   # the OLD tenant's CustomerApiKey.id — never cloned, so this can't resolve
                sent_at=iv.sent_at,
                delivery=(_remap_deep(iv.delivery, maps, run_id_maps) if iv.delivery else iv.delivery),
                started_at=iv.started_at, last_report_at=iv.last_report_at, closed_at=iv.closed_at,
                closed_state=iv.closed_state, closed_by=iv.closed_by,
                outcome_node_id=(maps['node'].get(iv.outcome_node_id) if iv.outcome_node_id is not None else None),
                outcome_in_window=iv.outcome_in_window, outcome_expected=iv.outcome_expected,
                node_id=(maps['node'].get(iv.node_id) if iv.node_id is not None else None),
                notes=iv.notes,
            )
            db.session.add(new_iv)
            db.session.flush()
            intervention_map[iv.id] = new_iv.id
        maps['intervention'] = intervention_map
        counts['interventions'] = len(intervention_map)

        # ---- Fixup: ContextNode.properties / source_event_id and ContextEdge.properties,
        #      now that maps['intervention'] exists too (an INTERVENTION-type node's
        #      properties carry intervention_id + trigger_episode_ids; its
        #      source_event_id is literally f'intervention:{old_id}') ----
        for cn, new_cn in node_pairs:
            if new_cn.properties:
                new_cn.properties = _remap_deep(cn.properties, maps, run_id_maps)
            if new_cn.source_event_id:
                new_cn.source_event_id = _remap_scalar_str(new_cn.source_event_id, maps)
        for ce, new_ce in edge_pairs:
            if new_ce.properties:
                new_ce.properties = _remap_deep(ce.properties, maps, run_id_maps)

        # ---- ForecastRun -> forecast_run_id_map ----
        forecast_run_id_map = {}
        n = 0
        for fr in ForecastRun.query.filter_by(customer_id=source_customer_id).order_by(ForecastRun.id).all():
            new_run_id = _mint_sibling_run_id(fr.run_id, 50)
            db.session.add(ForecastRun(
                run_id=new_run_id, customer_id=new_customer_id, vertical=fr.vertical,
                generator_version=fr.generator_version, horizon_days=fr.horizon_days, as_of=fr.as_of,
                basis_counts=_remap_deep(fr.basis_counts, maps, run_id_maps),
                labels=_remap_deep(fr.labels, maps, run_id_maps),
                portfolio=_remap_deep(fr.portfolio, maps, run_id_maps),
                config_snapshot=_remap_deep(fr.config_snapshot, maps, run_id_maps),
                accounts=fr.accounts, created_at=fr.created_at, created_by=fr.created_by,
            ))
            forecast_run_id_map[fr.run_id] = new_run_id
            n += 1
        run_id_maps.append(forecast_run_id_map)
        counts['forecast_runs'] = n

        # ---- AccountForecast (run_id is a real FK to forecast_runs.run_id) ----
        n = 0
        for af in AccountForecast.query.filter_by(customer_id=source_customer_id).order_by(AccountForecast.id).all():
            db.session.add(AccountForecast(
                run_id=forecast_run_id_map.get(af.run_id, af.run_id), customer_id=new_customer_id,
                account_id=account_map[af.account_id], as_of=af.as_of, basis=af.basis,
                p_retain=af.p_retain, p_retain_low=af.p_retain_low, p_retain_high=af.p_retain_high,
                p_expand=af.p_expand, p_expand_low=af.p_expand_low, p_expand_high=af.p_expand_high,
                arr=af.arr, expected_arr_end=af.expected_arr_end, expected_arr_low=af.expected_arr_low,
                expected_arr_high=af.expected_arr_high, decision_point_at=af.decision_point_at,
                stratum=af.stratum, n_labels=af.n_labels,
                forecast_json=_remap_deep(af.forecast_json, maps, run_id_maps),
                created_at=af.created_at,
            ))
            n += 1
        counts['account_forecasts'] = n

        # ---- WeightCalibration (superseded_by is self-referential: 2-pass within this block) ----
        wc_map, wc_pairs = {}, []
        for wc in WeightCalibration.query.filter_by(customer_id=source_customer_id).order_by(WeightCalibration.id).all():
            new_wc = WeightCalibration(
                customer_id=new_customer_id, vertical=wc.vertical, state=wc.state,
                method_version=wc.method_version, catalog_version=wc.catalog_version,
                config_snapshot=_remap_deep(wc.config_snapshot, maps, run_id_maps),
                outcome_counts=_remap_deep(wc.outcome_counts, maps, run_id_maps),
                outcome_node_ids=_remap_int_list(wc.outcome_node_ids, maps['node']),
                current_pillar_weights=wc.current_pillar_weights, current_kpi_weights=wc.current_kpi_weights,
                proposed_pillar_weights=wc.proposed_pillar_weights, proposed_kpi_weights=wc.proposed_kpi_weights,
                evidence=_remap_deep(wc.evidence, maps, run_id_maps),
                impact=_remap_deep(wc.impact, maps, run_id_maps),
                proposed_at=wc.proposed_at, proposed_by=wc.proposed_by,
                proposed_by_key_id=None,   # the OLD tenant's CustomerApiKey.id — never cloned
                decided_at=wc.decided_at, decided_by=wc.decided_by, decided_by_key_id=None,
                decision_note=wc.decision_note, applied_config_version=wc.applied_config_version,
                recompute=(_remap_deep(wc.recompute, maps, run_id_maps) if wc.recompute else wc.recompute),
                notes=wc.notes,
            )
            db.session.add(new_wc)
            db.session.flush()
            wc_map[wc.id] = new_wc.id
            wc_pairs.append((wc, new_wc))
        for wc, new_wc in wc_pairs:
            if wc.superseded_by is not None:
                new_wc.superseded_by = wc_map.get(wc.superseded_by)
        counts['weight_calibrations'] = len(wc_pairs)

        # ---- KPIMeasurement ----
        n = 0
        for k in KPIMeasurement.query.filter(KPIMeasurement.account_id.in_(_acct_ids_or_none)).order_by(KPIMeasurement.kpi_id).all():
            db.session.add(KPIMeasurement(
                account_id=account_map[k.account_id], kpi_code=k.kpi_code, value=k.value, target=k.target,
                pillar=k.pillar, upload_id=(csv_upload_map.get(k.upload_id) if k.upload_id is not None else None),
                attributes=(_remap_deep(k.attributes, maps, run_id_maps) if k.attributes else k.attributes),
                weight=k.weight, status=k.status, measured_at=k.measured_at, created_at=k.created_at,
            ))
            n += 1
        counts['kpi_measurements'] = n

        # ---- QualitativeSignal (signal_id / composite_signal_id / content_hash / source_ref
        #      copied verbatim: they're customer-scoped business keys or external refs, still
        #      internally valid once customer_id changes — only cg_node_id is a real CG id) ----
        n = 0
        for qs in QualitativeSignal.query.filter_by(customer_id=source_customer_id).order_by(QualitativeSignal.id).all():
            db.session.add(QualitativeSignal(
                signal_id=qs.signal_id, customer_id=new_customer_id, account_id=account_map[qs.account_id],
                signal_date=qs.signal_date, signal_type=qs.signal_type, content=qs.content, sentiment=qs.sentiment,
                stakeholder_level=qs.stakeholder_level, stakeholder_title=qs.stakeholder_title,
                sentiment_score=qs.sentiment_score, keywords=qs.keywords, is_narrative_signal=qs.is_narrative_signal,
                source_type=qs.source_type, raw_text=qs.raw_text, relationship_sentiment=qs.relationship_sentiment,
                product_sentiment=qs.product_sentiment, urgency_score=qs.urgency_score,
                escalation_probability=qs.escalation_probability, intent_signals=qs.intent_signals,
                stakeholder_roles=qs.stakeholder_roles, suggested_action=qs.suggested_action,
                confidence=qs.confidence, requires_review=qs.requires_review, llm_model_version=qs.llm_model_version,
                composite_signal_id=qs.composite_signal_id, dedup_confidence=qs.dedup_confidence,
                cg_node_id=(maps['node'].get(qs.cg_node_id) if qs.cg_node_id is not None else None),
                alert_suppressed=qs.alert_suppressed, structural_urgency=qs.structural_urgency,
                effective_urgency=qs.effective_urgency, consent_verified=qs.consent_verified,
                content_hash=qs.content_hash, source_ref=qs.source_ref, extractions=qs.extractions,
                use_case=qs.use_case, attributes=qs.attributes, occurred_at=qs.occurred_at,
            ))
            n += 1
        counts['qualitative_signals'] = n

        # ---- SignalReview ----
        n = 0
        for sr in SignalReview.query.filter_by(customer_id=source_customer_id).order_by(SignalReview.id).all():
            db.session.add(SignalReview(
                customer_id=new_customer_id, account_id=account_map[sr.account_id], signal_id=sr.signal_id,
                node_id=(maps['node'].get(sr.node_id) if sr.node_id is not None else None),
                decision=sr.decision, from_subtype=sr.from_subtype, to_subtype=sr.to_subtype,
                was_flagged=sr.was_flagged, note=sr.note, reviewer=sr.reviewer, created_at=sr.created_at,
            ))
            n += 1
        counts['signal_reviews'] = n

        # ---- WizardRun ----
        wizard_run_id_map = {}
        n = 0
        for wr in WizardRun.query.filter_by(customer_id=source_customer_id).order_by(WizardRun.id).all():
            new_run_id = _mint_sibling_run_id(wr.run_id, 50)
            db.session.add(WizardRun(
                run_id=new_run_id, customer_id=new_customer_id, wizard=wr.wizard, status=wr.status,
                config=(_remap_deep(wr.config, maps, run_id_maps) if wr.config else wr.config),
                results=(_remap_deep(wr.results, maps, run_id_maps) if wr.results else wr.results),
                error_message=wr.error_message, created_at=wr.created_at, completed_at=wr.completed_at,
                created_by=wr.created_by,
            ))
            wizard_run_id_map[wr.run_id] = new_run_id
            n += 1
        run_id_maps.append(wizard_run_id_map)
        counts['wizard_runs'] = n

        # ---- JourneyData last: journey_json cites ids from every map/run_id_map above,
        #      all of which are complete by this point ----
        n = 0
        for jd in JourneyData.query.filter_by(customer_id=source_customer_id).order_by(JourneyData.id).all():
            db.session.add(JourneyData(
                customer_id=new_customer_id, account_id=account_map[jd.account_id],
                journey_json=_remap_deep(jd.journey_json, maps, run_id_maps),
                total_weeks=jd.total_weeks, journey_pattern=jd.journey_pattern,
                generator_version=jd.generator_version, generated_at=jd.generated_at, updated_at=jd.updated_at,
            ))
            n += 1
        counts['journey_data'] = n

        db.session.commit()

        from mcp_server import audit as _audit
        _audit.record('mcp', 'clone_customer', new_customer_id, key_kind='n/a', outcome='allowed',
                     detail=f'cloned from customer {source_customer_id} ({src.domain}); rows {counts}')

        result = {
            'scope': 'customer',
            'customer_id': new_customer_id,
            'customer_name': name,
            'customer_uuid': customer_uuid,
            'domain': domain,
            'vertical': vertical,
            'created_at': customer.created_at.isoformat() if customer.created_at else None,
            'expires_at': expires_at.isoformat() if expires_at else None,
            'admin_user_id': user.user_id,
            'admin_email': admin_email,
            'admin_setup_token': setup_token,
            'admin_setup_token_note': 'Shown only once — use it at POST /app/api/auth/set-password to set the admin login password.',
            'data_origin': 'synthetic_demo',
            'disclosure': _disclosure('synthetic_demo'),
            'cloned_from_customer_id': int(source_customer_id),
            'cloned_rows': counts,
        }
        if full_key:
            result['api_key'] = full_key
            result['api_key_note'] = (
                'Save this API key — it is shown only once. '
                'Use it for the intelligence tools (list_accounts, get_account_health, etc.).'
            )
        return result


# ===================================================================
# Tools: playbook governance layer (playbooks/) — the record between
# "the evidence says act" and "an outcome happened"
# ===================================================================

@mcp.tool
def get_playbooks(customer_id: int) -> dict:
    """The playbooks this tenant's vertical defines (config/playbooks/<vertical>.json,
    validated against the taxonomy) and the tenant's overlay: webhook target
    (secret masked), switched-off playbooks, automation level, kill switch.

    Args:
        customer_id: The customer ID
    """
    _require_auth_if_key_present('get_playbooks', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from playbooks.definitions import playbooks_for_customer
        return playbooks_for_customer(customer_id)


@mcp.tool
def configure_playbooks(customer_id: int, webhook_url: str = None, webhook_secret: str = None,
                        disabled_playbooks: list = None, automation_level: int = None, kill_switch: bool = None,
                        slack_webhook_url: str = None) -> dict:
    """Set the tenant's playbook overlay. Only the fields given change.

    - webhook_url / webhook_secret: where approved interventions are POSTed
      (one signed JSON payload per approval; X-CI-Signature = sha256 HMAC of
      '<timestamp>.<body>' with the secret). https only. The secret is stored
      per tenant, never returned.
    - slack_webhook_url: optional Slack incoming-webhook URL; an approved
      notify-class intervention also posts a minimal message there (account,
      playbook, quote, intervention id). The URL is a secret: stored per
      tenant, never returned (reads show slack_webhook_url_set).
    - disabled_playbooks: playbook ids to switch off for this tenant.
    - automation_level: 0 = every approval is human; 1 = playbooks declared
      approval=auto (notify only) are approved by policy at evaluation time.
    - kill_switch: true stops evaluation and sending for the tenant.

    Args:
        customer_id: The customer ID
        webhook_url: Absolute https URL of the workflow engine's endpoint ('' clears it)
        webhook_secret: Shared secret for the signature ('' clears it)
        disabled_playbooks: Playbook ids to switch off (replaces the list)
        automation_level: 0 or 1
        kill_switch: true/false
        slack_webhook_url: https://hooks.slack.com/services/... ('' clears it)
    """
    _require_auth_if_key_present('configure_playbooks', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import Customer
        from extensions import db
        from playbooks.definitions import configure_tenant
        from playbooks.governance import current_actor, _audit
        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f'Customer {customer_id} not found.')
        try:
            cfg = configure_tenant(customer_id, webhook_url=webhook_url, webhook_secret=webhook_secret,
                                   disabled_playbooks=disabled_playbooks, automation_level=automation_level, kill_switch=kill_switch,
                                   slack_webhook_url=slack_webhook_url)
        except ValueError as e:
            raise ToolError(str(e))
        changed = [k for k, v in (('webhook_url', webhook_url), ('webhook_secret', webhook_secret), ('disabled_playbooks', disabled_playbooks),
                                  ('automation_level', automation_level), ('kill_switch', kill_switch),
                                  ('slack_webhook_url', slack_webhook_url)) if v is not None]
        _audit(customer_id, 'configure', current_actor(),
               f"changed {changed}; url_host={cfg['webhook_url'] and cfg['webhook_url'].split('/')[2]} level={cfg['automation_level']} "
               f"kill={cfg['kill_switch']} off={cfg['disabled_playbooks']} slack={cfg['slack_webhook_url_set']}")
        return {'customer_id': int(customer_id), 'tenant': cfg}


@mcp.tool
def evaluate_playbooks(customer_id: int, account_id: int = None, dry_run: bool = False) -> dict:
    """Propose interventions from the journeys' latest leading month: for each
    playbook whose trigger roles are in the cited evidence (at or above its
    urgency floor, and inside the renewal window when the trigger names one),
    write a 'proposed' row citing the episode ids. Runs by itself after every
    journey rebuild; call it to force a pass or to see what it would propose.

    Idempotent per (account, playbook, trigger set); one open proposal per
    (account, playbook); nothing within window_days of a closed one. Nothing
    is sent here unless the tenant's automation_level is 1 and the playbook is
    declared approval=auto (notify only).

    Args:
        customer_id: The customer ID
        account_id: One account (default: every account with a journey)
        dry_run: Return what would be proposed without writing anything
    """
    _require_auth_if_key_present('evaluate_playbooks', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import Customer
        from extensions import db
        from playbooks.governance import evaluate
        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f'Customer {customer_id} not found.')
        try:
            return evaluate(customer_id, account_id, dry_run=dry_run)
        except ValueError as e:
            raise ToolError(str(e))


@mcp.tool
def approve_intervention(customer_id: int, intervention_id: int, note: str = None) -> dict:
    """Approve a proposed intervention (write scope or the server key). The
    platform then sends the signed payload to the tenant's webhook, writes the
    INTERVENTION node (cited from the trigger evidence) and moves the row to
    'sent'. A failed or unconfigured delivery still moves to 'sent' with the
    error on the row and on the journey — the approval happened; the
    delivery problem is a finding, not a reason to hide it. To decline a
    proposal, call report_intervention with state='cancelled'.

    Args:
        customer_id: The customer ID
        intervention_id: The proposed intervention
        note: Why (kept on the row and in the audit log)
    """
    _require_auth_if_key_present('approve_intervention', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from playbooks.governance import approve
        try:
            return approve(customer_id, intervention_id, note=note)
        except ValueError as e:
            raise ToolError(str(e))


@mcp.tool
def report_intervention(customer_id: int, intervention_id: int, state: str, note: str = None,
                        outcome_type: str = None, outcome_date: str = None, revenue: Optional[float] = None) -> dict:
    """What the external workflow calls back with. state: 'started'
    (informational), 'done', 'failed', or 'cancelled' (also how a person
    declines a proposal). An outcome, if given, is logged through
    log_outcome (the tenant's outcome vocabulary, decision date, revenue
    magnitude) and linked to the intervention; the row records whether it
    landed inside the playbook's window and is one of the outcomes the
    playbook expects.

    Args:
        customer_id: The customer ID
        intervention_id: The intervention being reported on
        state: started | done | failed | cancelled
        note: What happened
        outcome_type: An outcome type from the tenant's vocabulary (optional)
        outcome_date: ISO date of the decision (default: now)
        revenue: Revenue magnitude for the outcome (optional)
    """
    _require_auth_if_key_present('report_intervention', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from playbooks.governance import report
        try:
            return report(customer_id, intervention_id, state, note=note, outcome_type=outcome_type,
                          outcome_date=outcome_date, revenue=revenue)
        except ValueError as e:
            raise ToolError(str(e))


@mcp.tool
def list_interventions(customer_id: int, account_id: int = None, state: str = None) -> dict:
    """Every intervention for a tenant: state, the cited evidence and quote,
    who approved, delivery result, what the workflow reported, the linked
    outcome. Flags stuck ones (sent, no report within the configured days)
    and delivery problems, and gives the per-playbook numbers: proposed /
    approved / sent / closed done-failed-cancelled, outcomes within window,
    realized $ and exposure $ as two numbers, never summed.

    Args:
        customer_id: The customer ID
        account_id: Filter to one account (optional)
        state: proposed | approved | sent | closed (optional)
    """
    _require_auth_if_key_present('list_interventions', customer_id)
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from playbooks.governance import list_interventions as _list
        return _list(customer_id, account_id, state)
