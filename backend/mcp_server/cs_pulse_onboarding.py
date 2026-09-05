#!/usr/bin/env python3
import os
from datetime import datetime
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
    data_origin: str,
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
            data_origin=data_origin,
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

    Wizards 'c' (weight calibration) and 'd' (NRR predictor) are not in
    this build yet.

    Args:
        customer_id: The customer ID
        wizard: 'a' or 'b'
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
            status = 'completed' if result.get('status') in ('completed', 'skipped') else 'failed'
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
                            WizardRun, CustomerApiKey, FeatureToggle)
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
        _del(FeatureToggle, FeatureToggle.query.filter_by(customer_id=c.customer_id))
        _del(CustomerApiKey, CustomerApiKey.query.filter_by(customer_id=c.customer_id))
        _del(Account, Account.query.filter_by(customer_id=c.customer_id))
        _del(User, User.query.filter_by(customer_id=c.customer_id))
        _del(CustomerConfig, CustomerConfig.query.filter_by(customer_id=c.customer_id))
        name, domain = c.customer_name, c.domain
        db.session.delete(c)
        db.session.commit()
        _audit.record('mcp', 'delete_customer', customer_id, key_kind='server' if raw else 'local', outcome='allowed',
                      detail=f'{name} ({domain}) deleted: {reason.strip()[:200]} — rows {counts}')
        return {'customer_id': customer_id, 'customer_name': name, 'domain': domain, 'deleted_rows': counts, 'reason': reason.strip()}
