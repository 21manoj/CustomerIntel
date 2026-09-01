"""
Lifecycle-Stage Weight Profiles
===============================

Accounts get weighted differently based on their lifecycle stage:
  - Onboarding (0-90 days): Heavy on adoption KPIs (TTFV, Onboarding Completion)
  - Stabilization (91-180 days): Heavy on engagement KPIs (NPS, QBR, Exec Sponsor)
  - Growth (181+ days): Heavy on commercial KPIs (Renewal Prob, Churn Risk, Expansion)

Stage is auto-determined from account age (contract_start or created_at).
Weights are stored on CustomerConfig.lifecycle_stage_weights (JSON).

Usage:
    from utils.lifecycle_stages import resolve_account_stage, get_stage_weights

    stage = resolve_account_stage(account, measurement_month, lifecycle_config)
    # stage = {'name': 'onboarding', 'pillar_weights': {...}, 'kpi_weights': {...}}
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Default lifecycle stage definitions
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_STAGES = [
    {
        'name': 'onboarding',
        'label': 'Onboarding',
        'min_days': 0,
        'max_days': 90,
        'description': 'First 90 days — focus on adoption, TTFV, onboarding completion',
        'pillar_weights': {
            'P1': 0.35,   # Product Adoption & Usage (heavy)
            'P2': 0.20,   # Customer Engagement
            'P3': 0.15,   # Customer Sentiment & Support
            'P4': 0.10,   # Partner & Ecosystem (low priority early)
            'P5': 0.20,   # Revenue & Growth
        },
        'kpi_emphasis': {
            'P1-KPI3': 'high',   # TTFV
            'P1-KPI5': 'high',   # Onboarding Completion Rate
            'P2-KPI5': 'high',   # CSM Interaction Frequency
        },
    },
    {
        'name': 'stabilization',
        'label': 'Stabilization',
        'min_days': 91,
        'max_days': 180,
        'description': 'Days 91-180 — focus on engagement, NPS, exec sponsor alignment',
        'pillar_weights': {
            'P1': 0.20,   # Product Adoption (still important)
            'P2': 0.30,   # Customer Engagement (heavy)
            'P3': 0.20,   # Customer Sentiment (NPS matters now)
            'P4': 0.10,   # Partner & Ecosystem
            'P5': 0.20,   # Revenue & Growth
        },
        'kpi_emphasis': {
            'P2-KPI1': 'high',   # Executive Sponsor Engagement
            'P2-KPI2': 'high',   # QBR Attendance Rate
            'P3-KPI3': 'high',   # NPS
        },
    },
    {
        'name': 'growth',
        'label': 'Growth & Renewal',
        'min_days': 181,
        'max_days': None,   # unbounded
        'description': 'Day 181+ — focus on renewal probability, expansion, churn risk',
        'pillar_weights': {
            'P1': 0.15,   # Product Adoption (maintenance)
            'P2': 0.15,   # Customer Engagement (steady state)
            'P3': 0.20,   # Customer Sentiment
            'P4': 0.15,   # Partner & Ecosystem
            'P5': 0.35,   # Revenue & Growth (heavy)
        },
        'kpi_emphasis': {
            'P5-KPI5': 'high',   # Renewal Probability
            'P5-KPI6': 'high',   # Churn Risk Score
            'P3-KPI3': 'high',   # NPS (always matters)
        },
    },
]

# Full lifecycle config with defaults
DEFAULT_LIFECYCLE_CONFIG = {
    'enabled': False,   # Opt-in — does not change existing behavior until enabled
    'date_field': 'contract_start',  # Which date to use for age calculation
    'stages': DEFAULT_STAGES,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Stage resolution
# ═══════════════════════════════════════════════════════════════════════════════

def _get_account_start_date(account, date_field: str = 'contract_start') -> Optional[date]:
    """
    Extract the start date for lifecycle stage calculation.

    Priority:
      1. profile_metadata[date_field] (e.g., contract_start)
      2. profile_metadata['contract_start'] (fallback)
      3. account.created_at (last resort)
    """
    meta = getattr(account, 'profile_metadata', None) or {}

    # Try specified date field first
    for field in [date_field, 'contract_start', 'contract_start_date']:
        raw = meta.get(field)
        if raw:
            try:
                if isinstance(raw, str):
                    return datetime.strptime(raw[:10], '%Y-%m-%d').date()
                elif isinstance(raw, (date, datetime)):
                    return raw if isinstance(raw, date) else raw.date()
            except (ValueError, TypeError):
                continue

    # Fallback to account.created_at
    created = getattr(account, 'created_at', None)
    if created:
        return created.date() if isinstance(created, datetime) else created

    return None


def resolve_account_stage(
    account,
    measurement_month: date,
    lifecycle_config: Optional[dict] = None,
) -> Optional[dict]:
    """
    Determine which lifecycle stage an account is in at a given measurement month.

    Args:
        account: Account model instance (needs profile_metadata and created_at)
        measurement_month: The month being scored (date object)
        lifecycle_config: CustomerConfig.lifecycle_stage_weights dict.
                         If None or not enabled, returns None (use default weights).

    Returns:
        Stage dict with 'name', 'pillar_weights', 'kpi_weights', etc.
        Or None if lifecycle stages are disabled or no match.
    """
    if not lifecycle_config or not lifecycle_config.get('enabled'):
        return None

    date_field = lifecycle_config.get('date_field', 'contract_start')
    start_date = _get_account_start_date(account, date_field)

    if not start_date:
        logger.debug(
            f"lifecycle_stages: no start date for account "
            f"{getattr(account, 'account_id', '?')}, using defaults"
        )
        return None

    # Calculate account age in days at this measurement month
    if isinstance(measurement_month, datetime):
        measurement_month = measurement_month.date()
    age_days = (measurement_month - start_date).days
    if age_days < 0:
        age_days = 0  # measurement before contract start

    # Match stage
    stages = lifecycle_config.get('stages', DEFAULT_STAGES)
    for stage in stages:
        min_d = stage.get('min_days', 0)
        max_d = stage.get('max_days')
        if age_days >= min_d and (max_d is None or age_days <= max_d):
            return stage

    # No stage matched — shouldn't happen with unbounded last stage
    return None


def get_stage_weights(
    stage: Optional[dict],
) -> tuple[Optional[dict], Optional[dict]]:
    """
    Extract pillar_weights and kpi_weights from a lifecycle stage.

    Returns:
        (pillar_weights, kpi_weights) — either can be None if not defined.
    """
    if not stage:
        return None, None

    pillar_weights = stage.get('pillar_weights')
    kpi_weights = stage.get('kpi_weights')
    return pillar_weights, kpi_weights


# ═══════════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════════

def validate_lifecycle_config(config: dict) -> list[str]:
    """
    Validate a lifecycle configuration dict.

    Returns list of error strings (empty = valid).
    """
    errors = []

    if not isinstance(config, dict):
        return ['Config must be a dict']

    stages = config.get('stages', [])
    if not stages:
        errors.append('No stages defined')
        return errors

    seen_names = set()
    prev_max = -1

    for i, stage in enumerate(stages):
        name = stage.get('name', '')
        if not name:
            errors.append(f'Stage {i}: missing name')
        if name in seen_names:
            errors.append(f'Stage {i}: duplicate name "{name}"')
        seen_names.add(name)

        min_d = stage.get('min_days', 0)
        max_d = stage.get('max_days')

        if min_d < 0:
            errors.append(f'Stage "{name}": min_days must be >= 0')
        if min_d <= prev_max:
            errors.append(f'Stage "{name}": overlaps with previous stage (min_days={min_d}, prev max={prev_max})')
        prev_max = max_d if max_d is not None else float('inf')

        # Validate pillar weights sum to ~1.0
        pw = stage.get('pillar_weights', {})
        if pw:
            total = sum(pw.values())
            if abs(total - 1.0) > 0.05:
                errors.append(f'Stage "{name}": pillar_weights sum to {total:.2f}, expected ~1.0')

    # Check last stage is unbounded
    last = stages[-1]
    if last.get('max_days') is not None:
        errors.append(f'Last stage "{last.get("name")}": max_days should be null (unbounded)')

    return errors


def normalize_stage_weights(config: dict) -> dict:
    """
    Normalize all pillar weights in each stage to sum to exactly 1.0.
    Returns a new config dict (does not mutate input).
    """
    import copy
    result = copy.deepcopy(config)

    for stage in result.get('stages', []):
        pw = stage.get('pillar_weights', {})
        if pw:
            total = sum(pw.values())
            if total > 0 and abs(total - 1.0) > 0.001:
                stage['pillar_weights'] = {k: v / total for k, v in pw.items()}

        kw = stage.get('kpi_weights', {})
        if kw:
            for pillar, weights in kw.items():
                if isinstance(weights, dict):
                    total = sum(weights.values())
                    if total > 0 and abs(total - 1.0) > 0.001:
                        kw[pillar] = {k: v / total for k, v in weights.items()}

    return result
