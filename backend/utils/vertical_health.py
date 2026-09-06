"""
Vertical-Aware Health Calculator Resolver
==========================================
Single source of truth for getting the correct calculate_kpi_health()
function based on a customer's vertical.

Always uses the generic JSON-based scorer (utils/generic_scorer.py) — this
build never carries over per-vertical Python modules
(verticals/{v}/api_routes.py). The old repo tried that path first and fell
back to the generic scorer for every vertical anyway (dc2_s and
saas_premium were both explicitly hardcoded to skip it — parity verified,
190 L1 checks + 8 L2/L3 scenarios, zero delta — and any *other* vertical
would hit an ImportError since no verticals/ directory exists here), so the
branch was already permanently dead in practice. Not carried forward.

Design principle: no vertical is special. Every vertical, including dc2_s,
is defined by its JSON catalog alone — see utils/vertical_registry.py.

Usage:
    from utils.vertical_health import get_health_calculator

    calculate_kpi_health = get_health_calculator(customer_id)
    overall_health, pillar_averages = calculate_kpi_health(kpi_values, customer_id)
"""

import logging

logger = logging.getLogger(__name__)

# Cache: customer_id -> vertical string
_vertical_cache = {}


def resolve_vertical(customer_id: int) -> str:
    """
    Resolve the vertical for a customer. Delegates to
    utils.vertical_registry.get_vertical_for_customer() — the canonical,
    fail-closed lookup (raises ValueError rather than silently defaulting
    to any vertical, dc2_s included, when a customer has no CustomerConfig
    row or an unset vertical column).

    The old repo had a second, less safe copy of this exact resolution
    logic here — defaulting to 'dc2_s' on any lookup failure, plus a
    directory-existence check tied to a per-customer directory layout this
    build doesn't have. Both removed: one call, one source of truth,
    consistent with vertical_registry's own "no fallback to dc2_s"
    contract instead of quietly working around it a second time.

    customer_id=None is not given a default here either — per
    get_vertical_for_customer's own docstring, a caller that genuinely
    needs a "no customer context" behavior must decide and implement that
    explicitly, not inherit a silent vertical guess from this function.
    """
    from utils.vertical_registry import get_vertical_for_customer

    cid = int(customer_id)
    if cid in _vertical_cache:
        return _vertical_cache[cid]

    vertical = get_vertical_for_customer(cid)
    _vertical_cache[cid] = vertical
    return vertical


def clear_vertical_cache(customer_id: int = None):
    """Clear the vertical cache. Call when customer config changes."""
    if customer_id:
        _vertical_cache.pop(int(customer_id), None)
    else:
        _vertical_cache.clear()


def flatten_kpi_weights(kpi_weights) -> dict:
    """CustomerConfig.kpi_weights is documented as {pillar: {code: w}}; a flat {code: w} is accepted too."""
    flat = {}
    for k, v in (kpi_weights or {}).items():
        if isinstance(v, dict):
            flat.update({code: float(w) for code, w in v.items()})
        else:
            flat[k] = float(v)
    return flat


def get_health_calculator(customer_id: int):
    """
    Return the correct calculate_kpi_health function for a customer's
    vertical, via the generic JSON-catalog scorer — see module docstring
    for why there's no per-vertical-Python-module branch here.

    Returns:
        callable: calculate_kpi_health(kpi_values, customer_id=None) -> (float, dict)
    """
    vertical = resolve_vertical(customer_id)
    return _make_generic_calculator(vertical, customer_id)


def _make_generic_calculator(vertical: str, customer_id: int = None):
    """
    Create a calculate_kpi_health function using the generic scorer + JSON catalog.
    This works for ANY vertical that has a catalog — no Python module needed.
    """
    from utils.vertical_registry import get_kpis, get_pillars
    from utils.generic_scorer import score_account_health

    # Pre-load catalogs (cached by registry)
    try:
        kpi_catalog = get_kpis(vertical)
        pillar_catalog = get_pillars(vertical)
    except ValueError:
        logger.error(f"No catalog found for vertical '{vertical}' — cannot create scorer")
        raise

    logger.info(f"Using generic scorer for vertical '{vertical}' ({len(kpi_catalog)} KPIs)")
    vertical_name = vertical

    def calculate_kpi_health(kpi_values, customer_id=None, vertical=None,
                             pillar_weight_overrides=None, explain=False, kpi_weight_overrides=None):
        """Generic health calculator using JSON catalog.

        `pillar_weight_overrides` (explicit, e.g. a lifecycle-stage profile
        from utils/lifecycle_stages.py) takes precedence over the
        customer's CustomerConfig.pillar_weights. The old repo's pipeline
        passed this kwarg to a calculator that didn't accept it, caught the
        TypeError and silently fell back to config weights — so lifecycle
        stage weights were never applied for any customer. Accepted here.
        `kpi_weight_overrides` (flat {code: weight}) likewise; without it
        the customer's CustomerConfig.kpi_weights apply (nested per pillar
        or flat), else the catalog's weight_l1.

        weight_source names who set the pillar weights that were applied:
        'lifecycle' (explicit stage profile) | CustomerConfig.weights_origin
        ('vertical_default' | 'customer_config' | 'wizard_c'; a row with
        weights but no origin was set by hand → 'customer_config') |
        'catalog' (no override row: the catalog's weight_l2 directly).
        """
        enabled_pillars = None
        weight_source = 'catalog'
        if pillar_weight_overrides:
            enabled_pillars = set(pillar_weight_overrides.keys())
            weight_source = 'lifecycle'
        cc = None
        if customer_id is not None and (not pillar_weight_overrides or kpi_weight_overrides is None):
            from models import CustomerConfig as CC
            cc = CC.query.filter_by(customer_id=int(customer_id)).first()
        if not pillar_weight_overrides and cc is not None and cc.pillar_weights:
            pillar_weight_overrides = cc.pillar_weights
            enabled_pillars = set(cc.pillar_weights.keys())
            weight_source = cc.weights_origin or 'customer_config'
        if kpi_weight_overrides is None and cc is not None and cc.kpi_weights:
            kpi_weight_overrides = flatten_kpi_weights(cc.kpi_weights)

        from utils.generic_scorer import score_account_health_explained
        r = score_account_health_explained(
            kpi_values=kpi_values,
            kpi_catalog=kpi_catalog,
            pillar_catalog=pillar_catalog,
            pillar_weight_overrides=pillar_weight_overrides,
            enabled_pillars=enabled_pillars,
            kpi_weight_overrides=kpi_weight_overrides,
        )
        if explain:
            from utils.vertical_registry import get_catalog_version
            r['weight_source'] = weight_source
            r['catalog_version'] = get_catalog_version(vertical_name)
            r['vertical'] = vertical_name
            return r
        return r['health'], r['pillars']

    return calculate_kpi_health


def get_trailing_kpi_values_func(customer_id: int):
    """Return the correct _get_trailing_kpi_values function for a customer's vertical."""
    vertical = resolve_vertical(customer_id)

    # Every vertical uses the same generic trailing-values query — no
    # per-vertical Python implementation exists in this build.
    def _generic_trailing_kpi_values(account_id, days=30):
        """Generic trailing KPI values — queries KPIMeasurement for any vertical."""
        from models import KPIMeasurement, db
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = KPIMeasurement.query.filter(
            KPIMeasurement.account_id == account_id,
            KPIMeasurement.measured_at >= cutoff,
        ).all()
        # Average by KPI code
        kpi_sums = {}
        kpi_counts = {}
        for r in rows:
            code = r.kpi_code
            kpi_sums[code] = kpi_sums.get(code, 0) + float(r.value)
            kpi_counts[code] = kpi_counts.get(code, 0) + 1
        return {code: kpi_sums[code] / kpi_counts[code] for code in kpi_sums}

    return _generic_trailing_kpi_values


def calculate_health_for_customer(kpi_values: dict, customer_id: int):
    """One-call convenience: resolve vertical + calculate health."""
    calc = get_health_calculator(customer_id)
    return calc(kpi_values, customer_id=customer_id)


# ============================================================
# Promoted utilities (moved from verticals/dc2_s/api_routes.py)
# These are vertical-agnostic and used by MCP server, playbook API, etc.
# ============================================================

def get_precalculated_scores(account_id):
    """
    Fetch the latest pre-calculated health score and pillar scores.

    Returns (health_score, health_status, pillar_dict) or (None, None, None)
    if no pre-calculated scores exist.

    Wave 1 Workstream A (Aug 4 2026): delegates to the canonical service in
    utils/account_health.py — this was one of four independently-maintained
    copies of the same read. Signature preserved for existing callers; new
    code should call utils.account_health.get_account_health() directly.
    """
    try:
        from utils.account_health import get_precalculated_scores_tuple
        return get_precalculated_scores_tuple(account_id)
    except Exception as e:
        logger.debug(f"Could not fetch pre-calculated scores for account {account_id}: {e}")
        return None, None, None


def normalize_kpi_code(kpi_code: str, customer_id: int = None) -> str:
    """
    Validate that a kpi_code exists in the customer's vertical catalog.

    Returns the kpi_code if valid, None otherwise.
    Vertical-aware: checks the correct catalog (DC2_S, SaaS Premium, or custom).
    """
    from utils.vertical_registry import get_kpis
    vertical = resolve_vertical(customer_id)
    try:
        catalog = get_kpis(vertical)
        return kpi_code if kpi_code in catalog else None
    except Exception:
        return None
