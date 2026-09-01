#!/usr/bin/env python3
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
    tier: str = None,
) -> dict:
    """Create a new customer with admin user and auto-generated API key.

    This is the first write step in onboarding. Creates:
    1. Customer record (with UUID)
    2. Admin user (with generated password)
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
        tier: Optional KPI tier for SaaS verticals. Options:
            'saas_starter_9' — 9 KPIs, 4 pillars, 1-hour onboarding (default for SaaS)
            'saas_predictive_11' — 11 KPIs, behavioral signals, requires product analytics
            'saas_full_43' — all KPIs, enterprise deployment
            If omitted, SaaS defaults to 'saas_starter_9'. Other verticals use the full catalog.
    """
    _require_auth_if_key_present('create_customer', None)
    _check_mcp_enabled()
    app = _get_flask_app()

    with app.app_context():
        from models import Customer, User, CustomerConfig
        from extensions import db
        from werkzeug.security import generate_password_hash
        import secrets as _secrets

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
        )
        if customer_uuid:
            customer.uuid = customer_uuid
        db.session.add(customer)
        db.session.flush()

        customer_id = customer.customer_id

        generated_password = _secrets.token_urlsafe(16)
        user = User(
            customer_id=customer_id,
            user_name=admin_name,
            email=admin_email,
            password_hash=generate_password_hash(generated_password),
            role='admin',
            vertical=vertical,
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
        }

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
    _require_auth_if_key_present('upload_csv', customer_id)
    _check_mcp_enabled()

    app = _get_flask_app()
    with app.app_context():
        from utils.csv_upload import _upload_csv_impl
        result = _upload_csv_impl(
            customer_id=customer_id,
            file_type=file_type,
            csv_content=csv_content,
            dry_run=dry_run,
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
            link_stakeholders_to_decisions,
        )

        customer = db.session.get(Customer, int(customer_id))
        if not customer:
            raise ToolError(f"Customer {customer_id} not found.")
        try:
            vertical = get_vertical_for_customer(customer_id)
        except ValueError as e:
            raise ToolError(str(e))

        steps, errors, timings = [], [], {}

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
        if has_staged:
            ingest = ingest_staged_csvs(customer_id, vertical)
            steps.extend(ingest.steps)
            errors.extend(ingest.errors)
            timings.update(ingest.timings)
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
            customer_id, accounts, mode=mode,
        )
        if health_step:
            steps.append(health_step)
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
        # Stage 3: Wizard A (arc classification)        — Tier 2A-5

        # Item 38: stakeholder→decision INVOLVES linking, after Wizard A.
        _t = time.time()
        step = link_stakeholders_to_decisions(customer_id)
        if step:
            steps.append(step)
        timings['stakeholder_linking'] = round(time.time() - _t, 2)

        # Stages 3a–8 (LLM tier-1, Wizard B, signal analyst, urgent scanner,
        # ROI, approval seed, Qdrant, onboarding agent) — later phases.

        status = 'success' if steps and not errors else 'partial' if steps else 'failed'
        duration = round(time.time() - _t0, 1)
        timings['total'] = duration

        import logging
        logging.getLogger(__name__).info(
            "process_data complete: customer=%s mode=%s duration=%ss timings=%s",
            customer_id, mode, duration, timings,
        )

        return {
            'scope': 'customer',
            'customer_id': customer_id,
            'status': status,
            'mode': mode,
            'vertical': vertical,
            'accounts': len(accounts),
            'kpi_measurements': kpi_count,
            'csv_files_processed': files_processed,
            'steps_completed': steps,
            'context_graph_audit': None,  # populated once Wizard A is ported
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


def _check_kpi_dependencies(enabled_kpis=None, enabled_pillars=None):
    """Check if disabled KPIs/pillars affect downstream engines (ROI, arc classifier).

    Returns list of warning strings. Empty list = no issues.
    Only warns when the customer has EXPLICITLY selected a subset of KPIs/pillars
    (not when using defaults = all enabled).
    """
    if not enabled_kpis and not enabled_pillars:
        return []  # Using all defaults — no warnings needed

    import json
    import os
    deps_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'kpi_dependencies.json')
    try:
        with open(deps_path) as f:
            deps = json.load(f)
    except Exception:
        return []  # Can't load deps file — skip silently

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
