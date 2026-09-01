#!/usr/bin/env python3
"""
Vertical Registry — single dispatch point for vertical-specific KPI definitions.

Resolution priority (highest first):
1. CustomerConfig.dc2s_kpi_definitions JSON column (DB — hot-reloadable)
2. JSON catalog file: config/{vertical}_kpi_catalog.json
3. Python module: verticals/{vertical}/kpi_definitions.py (legacy, for DC2_S/SaaS)

Design principle: DC2_S is not special. Any vertical can be defined via JSON
catalog without Python code. Python modules are legacy fallback only.

Usage:
    from utils.vertical_registry import get_pillars, get_kpis, get_vertical_for_customer

    pillars = get_pillars('saas_premium')   # → SAAS_PILLARS
    kpis = get_kpis('dc2_s')               # → DC2S_KPIS
    vertical = get_vertical_for_customer(444)  # → 'dc2_s' (or raises ValueError)
"""

import json
import logging
import os
from typing import Dict, Any, Optional, Tuple

log = logging.getLogger(__name__)

# Lazy-loaded caches
_pillars_cache: Dict[str, Dict] = {}
_kpis_cache: Dict[str, Dict] = {}
_pillar_roles_cache: Dict[str, Dict[str, str]] = {}

# Directory where JSON catalogs live
_CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config')

# Vertical aliases (normalize before lookup)
VERTICAL_ALIASES = {
    'dc2_s': 'dc2_s',
    'dc2s': 'dc2_s',
    'dc': 'dc2_s',
    'datacenter': 'dc2_s',
    'saas': 'saas_premium',
    'saas_premium': 'saas_premium',
}

# Auto-discover supported verticals from JSON catalogs on disk + hardcoded known ones
def _discover_verticals() -> set:
    """Discover verticals from JSON catalog files + known legacy verticals."""
    found = {'dc2_s', 'saas_premium'}  # Always include legacy verticals
    try:
        import glob
        for f in glob.glob(os.path.join(_CATALOG_DIR, '*_kpi_catalog.json')):
            basename = os.path.basename(f)
            # e.g., dc2s_kpi_catalog.json → dc2s, saas_premium_kpi_catalog.json → saas_premium
            slug = basename.replace('_kpi_catalog.json', '')
            # Map back through aliases
            canonical = VERTICAL_ALIASES.get(slug, slug)
            found.add(canonical)
    except Exception:
        pass
    return found

SUPPORTED_VERTICALS = _discover_verticals()


def normalize_vertical(vertical: str) -> str:
    """Normalize vertical name to canonical form."""
    return VERTICAL_ALIASES.get(vertical, vertical)


def get_pillars(vertical: str) -> Dict[str, Dict[str, Any]]:
    """Get pillar definitions for a vertical."""
    vertical = normalize_vertical(vertical)

    if vertical not in _pillars_cache:
        _pillars_cache[vertical] = _load_pillars(vertical)

    return _pillars_cache[vertical]


def get_kpis(vertical: str) -> Dict[str, Dict[str, Any]]:
    """Get KPI definitions for a vertical."""
    vertical = normalize_vertical(vertical)

    if vertical not in _kpis_cache:
        _kpis_cache[vertical] = _load_kpis(vertical)

    return _kpis_cache[vertical]


def get_pillar_roles(vertical: str) -> Dict[str, str]:
    """Get the role_name -> pillar_code map for a vertical.

    Sourced from the `pillar_roles` block of the vertical's JSON catalog
    (config/{vertical}_kpi_catalog.json). Returns {} if the vertical has no
    JSON catalog, or the catalog has no `pillar_roles` block — never raises,
    since an incomplete role map is a legitimate state (not every vertical
    fills every role; see `role()`).
    """
    vertical = normalize_vertical(vertical)

    if vertical not in _pillar_roles_cache:
        _pillar_roles_cache[vertical] = _load_pillar_roles(vertical)

    return _pillar_roles_cache[vertical]


def role(vertical: str, role_name: str) -> Optional[str]:
    """Resolve which pillar code plays a semantic role for a vertical.

    Roles are vertical-agnostic labels (e.g. 'partner', 'expansion',
    'revenue', 'reliability', 'capacity', 'adoption', 'engagement',
    'compliance') that let callers ask "which pillar is the partner
    pillar for this vertical?" instead of hardcoding a pillar CODE (like
    'P4') or a literal pillar NAME string and assuming it means the same
    thing in every vertical — that assumption is exactly what let
    partner_portal leak a non-partner vertical's pillar data under partner
    labels (see mcp_server/cs_pulse_admin.py::partner_portal).

    Returns the pillar code (e.g. 'P4') if this vertical has a pillar
    playing that role, or None if it doesn't — a vertical is not required
    to fill every role (e.g. healthcare_provider has no 'partner' or
    'expansion' pillar at all). Callers must treat None as "this vertical
    genuinely has no such pillar," not retry with a different vertical's
    code or a hardcoded default.

    Example:
        role('dc2_s', 'partner')       # → 'P4' (Channel & Partner Health)
        role('datacenter_v1', 'partner')  # → None (P4 there is Power & Facility)
    """
    vertical = normalize_vertical(vertical)
    return get_pillar_roles(vertical).get(role_name)


def get_default_pillar_weights(vertical: str) -> Dict[str, float]:
    """Get default L2 pillar weights for a vertical."""
    pillars = get_pillars(vertical)
    return {pid: info.get('weight_l2', 0.20) for pid, info in pillars.items()}


def get_vertical_for_customer(customer_id: int) -> str:
    """Look up vertical from CustomerConfig DB.

    Fails closed: raises ValueError if there's no CustomerConfig row for this
    customer or its `vertical` column is unset. No fallback to dc2_s — a
    silent default here previously masked missing-config bugs by serving the
    wrong vertical's KPI/pillar catalog under the customer's own name.
    Callers that need a "no customer context" default (e.g. customer_id is
    None) must decide and implement that explicitly at the call site.
    """
    from models import CustomerConfig
    config = CustomerConfig.query.filter_by(customer_id=customer_id).first()
    if config and config.vertical:
        return normalize_vertical(config.vertical)

    raise ValueError(
        f"Cannot resolve vertical for customer_id={customer_id}: no "
        f"CustomerConfig row found, or its vertical column is unset. "
        f"No fallback to dc2_s."
    )


def get_catalog_for_customer(customer_id: int) -> Tuple[Dict, Dict]:
    """Return (pillars, kpis) for a customer's vertical. Convenience wrapper."""
    vertical = get_vertical_for_customer(customer_id)
    return get_pillars(vertical), get_kpis(vertical)


def get_customer_kpi_overrides(customer_id: int) -> Optional[Dict]:
    """
    Load custom KPI definitions from CustomerConfig.dc2s_kpi_definitions.
    Returns the JSON dict if set, None otherwise.
    This is the hot-reload path: admin stores custom KPIs in DB, no restart needed.
    """
    try:
        from models import CustomerConfig
        config = CustomerConfig.query.filter_by(customer_id=customer_id).first()
        if config and hasattr(config, 'dc2s_kpi_definitions') and config.dc2s_kpi_definitions:
            return config.dc2s_kpi_definitions
    except Exception as e:
        log.debug("vertical_registry: could not load kpi_definitions for %s: %s", customer_id, e)
    return None


# ----------------------------------------------------------------
# Internal loaders — 3-tier resolution
# Priority: 1) JSON catalog file  2) Python module (legacy)
# DB overrides (CustomerConfig) are handled at the scoring layer,
# not here — this loads the BASE catalog for a vertical.
# ----------------------------------------------------------------

def _find_json_catalog_path(vertical: str) -> Optional[str]:
    """
    Locate the JSON catalog file for a vertical, if one exists.
    Tries multiple naming patterns (dc2_s → dc2_s_kpi_catalog.json, dc2s_kpi_catalog.json).
    Returns the path, or None if no candidate exists on disk.
    """
    slug = vertical.replace('_', '')  # dc2_s → dc2s
    candidates = [
        os.path.join(_CATALOG_DIR, f'{vertical}_kpi_catalog.json'),
        os.path.join(_CATALOG_DIR, f'{slug}_kpi_catalog.json'),
        os.path.join(_CATALOG_DIR, f'{vertical}.json'),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _load_from_json_catalog(vertical: str) -> Optional[Tuple[Dict, Dict]]:
    """
    Try to load KPI + pillar catalogs from a JSON file.
    Looks for: config/{vertical}_kpi_catalog.json
    Returns (kpis, pillars) or None if file doesn't exist.
    """
    path = _find_json_catalog_path(vertical)
    if path:
        from utils.generic_scorer import load_catalog_from_json
        kpis, pillars = load_catalog_from_json(path, expected_vertical=vertical)
        log.info(f"vertical_registry: loaded {vertical} from JSON catalog: {path} ({len(kpis)} KPIs)")
        return kpis, pillars
    return None


def _load_pillar_roles(vertical: str) -> Dict[str, str]:
    """
    Load the `pillar_roles` block (role_name -> pillar_code) from a
    vertical's JSON catalog file. Returns {} if there's no JSON catalog
    for this vertical, or the catalog has no `pillar_roles` block —
    Python-module-only legacy verticals (Tier 2 in _load_catalog) have no
    role map today; that's a gap for a future vertical, not an error here.
    """
    path = _find_json_catalog_path(vertical)
    if not path:
        return {}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        roles = data.get('pillar_roles') or {}
        if not isinstance(roles, dict):
            log.warning(f"vertical_registry: {path} has a non-dict pillar_roles block, ignoring")
            return {}
        return roles
    except Exception as e:
        log.warning(f"vertical_registry: failed to load pillar_roles from {path}: {e}")
        return {}


def _load_from_python_module(vertical: str) -> Optional[Tuple[Dict, Dict]]:
    """
    Legacy: load from Python module verticals/{vertical}/kpi_definitions.py.
    Returns (kpis, pillars) or None if module doesn't exist.
    """
    try:
        if vertical == 'dc2_s':
            from verticals.dc2_s.kpi_definitions import DC2S_KPIS, DC2S_PILLARS
            return DC2S_KPIS, DC2S_PILLARS
        elif vertical == 'saas_premium':
            from verticals.saas_premium.kpi_definitions import SAAS_KPIS, SAAS_PILLARS
            return SAAS_KPIS, SAAS_PILLARS
        else:
            # Try dynamic import for any vertical
            import importlib
            mod = importlib.import_module(f'verticals.{vertical}.kpi_definitions')
            kpis = getattr(mod, f'{vertical.upper()}_KPIS', None)
            pillars = getattr(mod, f'{vertical.upper()}_PILLARS', None)
            if kpis and pillars:
                return kpis, pillars
    except (ImportError, ModuleNotFoundError):
        pass
    except Exception as e:
        log.warning(f"vertical_registry: Python module error for {vertical}: {e}")
    return None


def _load_catalog(vertical: str) -> Tuple[Dict, Dict]:
    """
    Load KPI + pillar catalogs using 3-tier resolution.
    1. JSON catalog file (config/{vertical}_kpi_catalog.json)
    2. Python module (verticals/{vertical}/kpi_definitions.py) — legacy
    Raises ValueError if neither found.
    """
    # Tier 1: JSON catalog file
    result = _load_from_json_catalog(vertical)
    if result:
        return result

    # Tier 2: Python module (legacy)
    result = _load_from_python_module(vertical)
    if result:
        return result

    raise ValueError(
        f"Unknown vertical: '{vertical}'. No JSON catalog at config/{vertical}_kpi_catalog.json "
        f"and no Python module at verticals/{vertical}/kpi_definitions.py"
    )


def _load_pillars(vertical: str) -> Dict:
    kpis, pillars = _load_catalog(vertical)
    return pillars


def _load_kpis(vertical: str) -> Dict:
    kpis, pillars = _load_catalog(vertical)
    return kpis
