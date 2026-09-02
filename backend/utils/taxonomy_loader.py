"""
Taxonomy Loader — single source of truth for polarity/revenue-bucket/auto-recovery
subtype vocabularies. Base + per-vertical overlay pattern mirrors vertical_registry.

Loads:
- config/taxonomy_base.json   (vertical-agnostic canonical vocabulary)
- config/taxonomy_{vertical}.json  (per-vertical overlay; additive only)

Overlays CANNOT contradict the base — e.g. an overlay cannot mark a subtype as
polarity-ambiguous if the base already places it in a definitive polarity bucket.
Validation is strict and fail-fast.

Pure-Python validation (no jsonschema dependency).

Used by:
- utils.context_graph_invariants  (backend; polarity + revenue_bucket decisions)
- load-driver/scenarios/scenario_manifest.py  (via copy at load-driver/config/)

Usage:
    from utils.taxonomy_loader import get_taxonomy, validate_all_at_boot

    tax = get_taxonomy()              # base only
    tax = get_taxonomy('saas_premium')  # base + SaaS overlay
    validate_all_at_boot()            # called from app_v3_minimal startup
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

log = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config')

_REQUIRED_TOP_KEYS = {'version'}
_ALLOWED_TOP_KEYS = {
    'version', 'description', 'extends', 'vertical',
    'polarity_ambiguous_outcome_subtypes',
    'polarity_ambiguous_signal_subtypes',
    'revenue_buckets',
    'auto_recovery_outcome_subtypes',
    'signal_roles',
}
_BUCKET_KEYS = {'at_risk', 'lost', 'expansion', 'pipeline', 'protected'}

# Signal-role polarity (Tier 2A-5, journeys): which roles count as negative /
# positive behavioral evidence. Roles absent here are neutral (intervention,
# routine, announcement). 'announcement' is deliberately neutral AND excluded
# from the leading composite — see taxonomy_base.json's _description.
NEGATIVE_SIGNAL_ROLES = frozenset({
    'champion_change', 'engagement_decline', 'usage_decline', 'escalation',
    'infra_incident', 'capacity_pressure', 'delivery_stall', 'commercial_pressure',
})
POSITIVE_SIGNAL_ROLES = frozenset({
    'expansion_intent', 'expansion_realized', 'advocacy', 'recovery',
})
# 'crm_flag' = the CSM's / CRM's own risk assessment — the human comparator
# the lead-time claim is measured against, so it can never feed the layer
# it is compared with.
LEADING_EXCLUDED_ROLES = frozenset({'announcement', 'crm_flag'})

# Module-level cache keyed by (vertical or None)
_taxonomy_cache: Dict[Optional[str], 'Taxonomy'] = {}


@dataclass(frozen=True)
class Taxonomy:
    """Effective taxonomy for a tenant (base + overlay merged)."""
    version: str
    vertical: Optional[str]
    polarity_ambiguous_outcome_subtypes: FrozenSet[str]
    polarity_ambiguous_signal_subtypes: FrozenSet[str]
    revenue_bucket_map: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    auto_recovery_outcome_subtypes: FrozenSet[str] = field(default_factory=frozenset)
    signal_roles: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    signal_role_map: Dict[str, str] = field(default_factory=dict)  # subtype -> role

    def signal_role(self, subtype: Optional[str]) -> Optional[str]:
        """Role for a signal subtype, or None if the vocabulary doesn't know it."""
        if not subtype:
            return None
        return self.signal_role_map.get(subtype.strip().lower())

    def role_polarity(self, role: Optional[str]) -> int:
        """+1 positive, -1 negative, 0 neutral/unknown."""
        if role in NEGATIVE_SIGNAL_ROLES:
            return -1
        if role in POSITIVE_SIGNAL_ROLES:
            return 1
        return 0

    def revenue_bucket(self, subtype: Optional[str]) -> Optional[str]:
        if not subtype:
            return None
        s = subtype.strip().lower()
        for bucket, subs in self.revenue_bucket_map.items():
            if s in subs:
                return bucket
        return None


class TaxonomyValidationError(ValueError):
    """Raised when a taxonomy file violates schema or overlay conflict rules."""


def _validate_structural(data: dict, filename: str, is_overlay: bool) -> None:
    """Structural checks — type, required keys, allowed keys."""
    if not isinstance(data, dict):
        raise TaxonomyValidationError(f'{filename}: root must be an object')

    missing = _REQUIRED_TOP_KEYS - data.keys()
    if missing:
        raise TaxonomyValidationError(f'{filename}: missing required keys: {sorted(missing)}')

    unknown = data.keys() - _ALLOWED_TOP_KEYS
    if unknown:
        raise TaxonomyValidationError(f'{filename}: unknown keys: {sorted(unknown)}')

    if is_overlay:
        if data.get('extends') != 'base':
            raise TaxonomyValidationError(f'{filename}: overlay must declare "extends": "base"')
        if 'vertical' not in data:
            raise TaxonomyValidationError(f'{filename}: overlay must declare "vertical"')

    for list_key in (
        'polarity_ambiguous_outcome_subtypes',
        'polarity_ambiguous_signal_subtypes',
        'auto_recovery_outcome_subtypes',
    ):
        if list_key in data:
            value = data[list_key]
            if not isinstance(value, list):
                raise TaxonomyValidationError(f'{filename}: "{list_key}" must be a list')
            if len(value) != len(set(value)):
                raise TaxonomyValidationError(f'{filename}: "{list_key}" has duplicate entries')
            for item in value:
                # Subtypes must match ^[a-z_]+$ — lowercase letters + underscore only.
                if (not isinstance(item, str)
                        or not item
                        or not all(c == '_' or ('a' <= c <= 'z') for c in item)):
                    raise TaxonomyValidationError(
                        f'{filename}: "{list_key}" contains invalid subtype: {item!r} '
                        f'(must match ^[a-z_]+$)'
                    )

    if 'revenue_buckets' in data:
        buckets = data['revenue_buckets']
        if not isinstance(buckets, dict):
            raise TaxonomyValidationError(f'{filename}: "revenue_buckets" must be an object')
        unknown_buckets = buckets.keys() - _BUCKET_KEYS
        if unknown_buckets:
            raise TaxonomyValidationError(
                f'{filename}: unknown revenue buckets: {sorted(unknown_buckets)}. '
                f'Allowed: {sorted(_BUCKET_KEYS)}'
            )
        for bk, subs in buckets.items():
            if not isinstance(subs, list):
                raise TaxonomyValidationError(
                    f'{filename}: revenue_buckets.{bk} must be a list'
                )
            if len(subs) != len(set(subs)):
                raise TaxonomyValidationError(
                    f'{filename}: revenue_buckets.{bk} has duplicates'
                )

    if 'signal_roles' in data:
        roles = data['signal_roles']
        if not isinstance(roles, dict):
            raise TaxonomyValidationError(f'{filename}: "signal_roles" must be an object')
        seen: Dict[str, str] = {}
        for role, subs in roles.items():
            if role.startswith('_'):
                continue
            if not all(c == '_' or ('a' <= c <= 'z') for c in role):
                raise TaxonomyValidationError(f'{filename}: signal role {role!r} must match ^[a-z_]+$')
            if not isinstance(subs, list):
                raise TaxonomyValidationError(f'{filename}: signal_roles.{role} must be a list')
            for s in subs:
                if (not isinstance(s, str) or not s
                        or not all(c == '_' or ('a' <= c <= 'z') for c in s)):
                    raise TaxonomyValidationError(
                        f'{filename}: signal_roles.{role} contains invalid subtype: {s!r}')
                if s in seen:
                    raise TaxonomyValidationError(
                        f'{filename}: subtype {s!r} is in both signal roles '
                        f'{seen[s]!r} and {role!r} — a subtype has exactly one role')
                seen[s] = role


def _signal_role_pairs(data: dict) -> Dict[str, str]:
    return {
        s: role for role, subs in (data.get('signal_roles') or {}).items()
        if not role.startswith('_') for s in subs
    }


def _validate_overlay_vs_base(overlay: dict, base: dict, filename: str) -> None:
    """Overlay cannot contradict base — e.g. mark a subtype ambiguous that base
    places in a definitive polarity bucket, or put the same subtype in a
    different revenue bucket than base.
    """
    base_buckets = base.get('revenue_buckets', {})
    # Flatten base revenue subtypes → bucket
    base_subtype_to_bucket: Dict[str, str] = {}
    for bk, subs in base_buckets.items():
        for s in subs:
            base_subtype_to_bucket[s] = bk

    # 1. Overlay revenue buckets must not place a base subtype into a different bucket
    overlay_buckets = overlay.get('revenue_buckets', {})
    for bk, subs in overlay_buckets.items():
        for s in subs:
            if s in base_subtype_to_bucket and base_subtype_to_bucket[s] != bk:
                raise TaxonomyValidationError(
                    f'{filename}: subtype {s!r} is in base bucket '
                    f'"{base_subtype_to_bucket[s]}" — overlay cannot move it to "{bk}"'
                )

    # 2. Overlay ambiguous sets must not contain a subtype that base has already
    #    placed in a definitive polarity bucket (revenue bucket membership implies polarity).
    overlay_ambig_outcome = set(overlay.get('polarity_ambiguous_outcome_subtypes', []))
    for s in overlay_ambig_outcome:
        if s in base_subtype_to_bucket:
            raise TaxonomyValidationError(
                f'{filename}: subtype {s!r} is already in a definitive revenue bucket '
                f'in base — overlay cannot mark it polarity-ambiguous'
            )

    # 3. Overlay signal roles must not move a base subtype to a different role
    base_roles = _signal_role_pairs(base)
    for s, role in _signal_role_pairs(overlay).items():
        if s in base_roles and base_roles[s] != role:
            raise TaxonomyValidationError(
                f'{filename}: signal subtype {s!r} has base role {base_roles[s]!r} '
                f'— overlay cannot move it to {role!r}'
            )


def _merge(base: dict, overlay: Optional[dict]) -> Taxonomy:
    """Return Taxonomy formed by base + overlay. Overlay is additive."""
    def _get_list(d: dict, key: str) -> list:
        return d.get(key, []) or []

    ambig_outcome = set(_get_list(base, 'polarity_ambiguous_outcome_subtypes'))
    ambig_signal = set(_get_list(base, 'polarity_ambiguous_signal_subtypes'))
    auto_recovery = set(_get_list(base, 'auto_recovery_outcome_subtypes'))
    buckets: Dict[str, set] = {
        bk: set(subs) for bk, subs in base.get('revenue_buckets', {}).items()
    }

    roles: Dict[str, set] = {
        role: set(subs) for role, subs in (base.get('signal_roles') or {}).items()
        if not role.startswith('_')
    }

    if overlay:
        ambig_outcome.update(_get_list(overlay, 'polarity_ambiguous_outcome_subtypes'))
        ambig_signal.update(_get_list(overlay, 'polarity_ambiguous_signal_subtypes'))
        auto_recovery.update(_get_list(overlay, 'auto_recovery_outcome_subtypes'))
        for bk, subs in overlay.get('revenue_buckets', {}).items():
            buckets.setdefault(bk, set()).update(subs)
        for role, subs in (overlay.get('signal_roles') or {}).items():
            if not role.startswith('_'):
                roles.setdefault(role, set()).update(subs)

    return Taxonomy(
        version=base['version'],
        vertical=(overlay or {}).get('vertical'),
        polarity_ambiguous_outcome_subtypes=frozenset(ambig_outcome),
        polarity_ambiguous_signal_subtypes=frozenset(ambig_signal),
        revenue_bucket_map={bk: frozenset(subs) for bk, subs in buckets.items()},
        auto_recovery_outcome_subtypes=frozenset(auto_recovery),
        signal_roles={role: frozenset(subs) for role, subs in roles.items()},
        signal_role_map={s: role for role, subs in roles.items() for s in subs},
    )


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        raise TaxonomyValidationError(f'Taxonomy file not found: {path}')
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise TaxonomyValidationError(f'{path}: invalid JSON — {e}')


def _load_base() -> dict:
    path = os.path.join(_CONFIG_DIR, 'taxonomy_base.json')
    data = _load_json(path)
    _validate_structural(data, 'taxonomy_base.json', is_overlay=False)
    return data


def _load_overlay(vertical: str) -> Optional[dict]:
    # Normalize via aliases from vertical_registry if available
    try:
        from utils.vertical_registry import normalize_vertical
        vertical = normalize_vertical(vertical)
    except Exception:
        pass

    path = os.path.join(_CONFIG_DIR, f'taxonomy_{vertical}.json')
    if not os.path.exists(path):
        return None
    data = _load_json(path)
    _validate_structural(data, f'taxonomy_{vertical}.json', is_overlay=True)
    return data


def get_taxonomy(vertical: Optional[str] = None) -> Taxonomy:
    """Return effective taxonomy for a vertical (or base if None). Cached per-process."""
    cache_key = vertical
    if cache_key in _taxonomy_cache:
        return _taxonomy_cache[cache_key]

    base = _load_base()
    overlay = _load_overlay(vertical) if vertical else None
    if overlay:
        _validate_overlay_vs_base(overlay, base, f'taxonomy_{vertical}.json')

    tax = _merge(base, overlay)
    _taxonomy_cache[cache_key] = tax
    return tax


def validate_all_at_boot() -> List[str]:
    """Load + validate base + every overlay found on disk. Raise on first error.

    Call from application startup (app_v3_minimal.py). Intentionally fails loudly —
    an invalid taxonomy file should prevent the app from serving traffic with a
    corrupt polarity/revenue model.

    Returns list of verified file basenames for the startup log.
    """
    import glob

    verified = []

    base = _load_base()
    verified.append('taxonomy_base.json')

    for path in sorted(glob.glob(os.path.join(_CONFIG_DIR, 'taxonomy_*.json'))):
        basename = os.path.basename(path)
        if basename in ('taxonomy_base.json', 'taxonomy_schema.json'):
            continue
        data = _load_json(path)
        _validate_structural(data, basename, is_overlay=True)
        _validate_overlay_vs_base(data, base, basename)
        verified.append(basename)

    log.info('[taxonomy_loader] validated %d files at boot: %s', len(verified), verified)
    return verified


def reset_cache() -> None:
    """Testing hook — clear the in-process cache."""
    _taxonomy_cache.clear()
