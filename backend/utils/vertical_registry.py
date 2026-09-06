#!/usr/bin/env python3
"""
Vertical Registry — single dispatch point for vertical-specific KPI definitions.

Base catalog resolution: JSON catalog file only —
config/{vertical}_kpi_catalog.json. (A per-customer override layer exists
separately: get_customer_kpi_overrides() reads CustomerConfig.kpi_definitions
for hot-reloadable, customer-specific KPI overrides on top of the base
catalog — that's a distinct concern from resolving the base catalog itself.)

Design principle: no vertical is special, dc2_s included. Every vertical is
defined by its JSON catalog alone; there is no Python-module fallback tier
in this build (the old repo had one, verticals/{vertical}/kpi_definitions.py
— removed here since every vertical already has a JSON catalog, making that
tier permanently unreachable dead code, not a real fallback).

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
_pillar_roles_notes_cache: Dict[str, Dict[str, str]] = {}

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


def get_vertical_description(vertical: str) -> Optional[str]:
    """The catalog's own 'description' (None when the catalog has none)."""
    import json
    path = _find_json_catalog_path(normalize_vertical(vertical))
    if not path:
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f).get('description') or None


def get_catalog_version(vertical: str) -> Optional[str]:
    """The catalog's own 'version' (None when it has none) — stamped on every health score."""
    import json
    path = _find_json_catalog_path(normalize_vertical(vertical))
    if not path:
        return None
    with open(path, encoding='utf-8') as f:
        v = json.load(f).get('version')
    return str(v) if v is not None else None


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


def get_pillar_roles_notes(vertical: str) -> Dict[str, str]:
    """Get the `pillar_roles_notes` block (pillar_code(s) -> human-readable reason) for a vertical.

    Several catalogs (healthcare_provider, manufacturing_iot, datacenter_v1, dc2_s,
    saas_premium) document, per unmapped or low-confidence pillar, *why* it was left
    that way in `pillar_roles` — e.g. "no clean match in the shared vocabulary,
    left unmapped rather than force-fit". Until now nothing read this block back;
    it was write-only documentation. Callers that report an unmapped pillar/role to
    a human (e.g. roi/measured.py's by_pillar) should surface it here instead of a
    bare 'unmapped' status, so a deliberate design decision doesn't read as a bug.

    Returns {} if the vertical has no JSON catalog, or the catalog has no
    `pillar_roles_notes` block — never raises.
    """
    vertical = normalize_vertical(vertical)

    if vertical not in _pillar_roles_notes_cache:
        _pillar_roles_notes_cache[vertical] = _load_pillar_roles_notes(vertical)

    return _pillar_roles_notes_cache[vertical]


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
    Load custom KPI definitions from CustomerConfig.kpi_definitions.
    Returns the JSON dict if set, None otherwise.
    This is the hot-reload path: admin stores custom KPIs in DB, no restart needed.
    """
    try:
        from models import CustomerConfig
        config = CustomerConfig.query.filter_by(customer_id=customer_id).first()
        if config and hasattr(config, 'kpi_definitions') and config.kpi_definitions:
            return config.kpi_definitions
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
    for this vertical, or the catalog has no `pillar_roles` block — an
    incomplete role map is a legitimate state (not every vertical fills
    every role), not an error here.
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


def _load_pillar_roles_notes(vertical: str) -> Dict[str, str]:
    """
    Load the `pillar_roles_notes` block (pillar_code(s) -> reason string) from a
    vertical's JSON catalog file. Returns {} if there's no JSON catalog for this
    vertical, or the catalog has no `pillar_roles_notes` block.
    """
    path = _find_json_catalog_path(vertical)
    if not path:
        return {}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        notes = data.get('pillar_roles_notes') or {}
        if not isinstance(notes, dict):
            log.warning(f"vertical_registry: {path} has a non-dict pillar_roles_notes block, ignoring")
            return {}
        return notes
    except Exception as e:
        log.warning(f"vertical_registry: failed to load pillar_roles_notes from {path}: {e}")
        return {}


def _load_catalog(vertical: str) -> Tuple[Dict, Dict]:
    """
    Load KPI + pillar catalogs from a vertical's JSON catalog file
    (config/{vertical}_kpi_catalog.json). Raises ValueError if not found.

    The old repo had a second, legacy tier here (a per-vertical Python
    module, verticals/{v}/kpi_definitions.py) as a fallback. Removed, not
    carried forward: this build has no verticals/ directory at all, so
    that branch could only ever raise ImportError and fall through — dead
    code kept "just in case," the same shape as the equivalent fallback
    already removed from utils/vertical_health.py. Consistent with the
    design principle both files now state plainly: no vertical is special,
    every one is defined by its JSON catalog alone. If a future vertical
    genuinely needs logic a catalog can't express, that's a real decision
    to make when it comes up, not a legacy escape hatch kept unused.
    """
    result = _load_from_json_catalog(vertical)
    if result:
        return result

    raise ValueError(
        f"Unknown vertical: '{vertical}'. No JSON catalog at config/{vertical}_kpi_catalog.json"
    )


def _load_pillars(vertical: str) -> Dict:
    kpis, pillars = _load_catalog(vertical)
    return pillars


def _load_kpis(vertical: str) -> Dict:
    kpis, pillars = _load_catalog(vertical)
    return kpis
