"""
Canonical account-health read service — Wave 1 Workstream A (Aug 4, 2026).

DECISION (Manoj, Aug 4 2026, resolves V6 Open Decision #22's store half):
the canonical health store is HealthScore/PillarScore — what the process_data
pipeline writes and MCP reads. HealthTrend is being drained: its writers keep
dual-writing during a verification window, its readers migrate here, and a
divergence logger flags any (account, month) where the two stores disagree.

Before this module, account health had 4-5 independently-maintained read
paths (MCP common.py, Flask kpi_api.py via HealthTrend + a 50.0 proxy,
Ask AI's _execute_direct fallback defaulting to 0, health_trend_api,
verticals/dc2_s api_routes with a 4-tuple variant) — the same account could
show different numbers per surface, and "no data" was represented three
different ways (50.0 / 0 / None). See AUDIT_REPORT_E2E_2026-08-03.md §4.7.

Rules this module enforces:
- Missing data is EXPLICIT: ``missing=True`` + reason. Never a magic default
  score (no 50.0 sentinel, no silent 0).
- Pillar rows are month-pinned to the health row's measurement_month
  (mixing months across pillars was one of the drift axes).
- Returns a dataclass, not a tuple — the old copies had a 3-tuple vs
  4-tuple arity fork that made code un-movable between layers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Log at WARNING when HealthTrend disagrees with HealthScore by more than
# this many points for the same account — evidence for the drain decision.
DIVERGENCE_TOLERANCE_PTS = 0.5


@dataclass
class AccountHealth:
    account_id: int
    health_score: Optional[float] = None
    health_status: Optional[str] = None
    measurement_month: Optional[date] = None
    pillars: dict = field(default_factory=dict)
    source: str = "health_scores"        # 'health_scores' | 'none'
    account_status: Optional[str] = None  # e.g. 'active' / 'churned' (tenant-checked reads only)
    missing: bool = False
    missing_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.missing and self.health_score is not None


def get_account_health(account_id: int, customer_id: int | None = None,
                       check_divergence: bool = True) -> AccountHealth:
    """Canonical single-account health read.

    Args:
        account_id: the account to read.
        customer_id: when provided, enforces tenant isolation — an account
            belonging to a different customer returns missing
            ('not_found_or_wrong_tenant') rather than data.
        check_divergence: compare against HealthTrend (legacy store) and log
            a WARNING when they disagree — drain-verification instrumentation.

    Never returns a fabricated score: callers must handle ``missing``.
    """
    from models import HealthScore, PillarScore
    import utils.health_thresholds as ht

    account_status = None
    if customer_id is not None:
        from models import Account
        acct = Account.query.filter_by(
            account_id=account_id, customer_id=customer_id,
        ).first()
        if not acct:
            return AccountHealth(
                account_id=account_id, source="none", missing=True,
                missing_reason="not_found_or_wrong_tenant",
            )
        account_status = acct.account_status

    hs = (
        HealthScore.query.filter_by(account_id=account_id)
        .order_by(HealthScore.measurement_month.desc())
        .first()
    )
    if not hs or hs.health_score is None:
        return AccountHealth(
            account_id=account_id, source="none",
            account_status=account_status,
            missing=True, missing_reason="no_health_scores",
        )

    health = float(hs.health_score)
    status = hs.health_status or ht.classify(health)

    # Pillars: contributing_pillars JSON first, else PillarScore rows
    # MONTH-PINNED to the health row's measurement_month.
    pillars: dict = {}
    if hs.contributing_pillars:
        pillars = {k: float(v) for k, v in hs.contributing_pillars.items()}
    else:
        for ps in PillarScore.query.filter_by(
            account_id=account_id, measurement_month=hs.measurement_month,
        ).all():
            if ps.pillar_score is not None:
                pillars[ps.pillar_code] = float(ps.pillar_score)

    if check_divergence:
        _log_healthtrend_divergence(account_id, health)

    return AccountHealth(
        account_id=account_id,
        health_score=health,
        health_status=status,
        measurement_month=hs.measurement_month,
        pillars=pillars,
        source="health_scores",
        account_status=account_status,
    )


def get_account_health_history(account_id: int, customer_id: int,
                               months: int = 12) -> list[dict]:
    """Canonical health history (HealthScore rows, newest-first capped then
    returned oldest-first). customer_id is MANDATORY — this replaces the
    HealthTrend-based reader that had no tenant filter (audit C-18)."""
    from models import Account, HealthScore

    acct = Account.query.filter_by(
        account_id=account_id, customer_id=customer_id,
    ).first()
    if not acct:
        return []

    rows = (
        HealthScore.query.filter_by(account_id=account_id)
        .order_by(HealthScore.measurement_month.desc())
        .limit(months)
        .all()
    )
    out = []
    for hs in reversed(rows):
        if hs.health_score is None:
            continue
        out.append({
            "month": hs.measurement_month.strftime("%Y-%m")
                     if hs.measurement_month else None,
            "overall_score": float(hs.health_score),
            "health_status": hs.health_status,
            "pillars": {k: float(v) for k, v in (hs.contributing_pillars or {}).items()},
            "change_from_last_month": float(hs.change_from_last_month)
                     if getattr(hs, "change_from_last_month", None) is not None else None,
        })
    return out


def get_precalculated_scores_tuple(account_id: int, with_month: bool = False):
    """Back-compat shim for the four legacy ``get_precalculated_scores``
    copies (mcp_server/common.py, mcp_server/cs_pulse_mcp_server.py,
    utils/vertical_health.py [3-tuple] and verticals/dc2_s/api_routes.py
    [4-tuple]). New code should call get_account_health() directly."""
    try:
        ah = get_account_health(account_id)
    except Exception as e:
        logger.debug("get_precalculated_scores_tuple(%s) failed: %s", account_id, e)
        ah = AccountHealth(account_id=account_id, missing=True, missing_reason="error")
    if not ah.ok:
        return (None, None, None, None) if with_month else (None, None, None)
    if with_month:
        return ah.health_score, ah.health_status, ah.pillars, ah.measurement_month
    return ah.health_score, ah.health_status, ah.pillars


def _log_healthtrend_divergence(account_id: int, canonical_score: float) -> None:
    """Drain-verification instrumentation (spec A2): while HealthTrend is
    still dual-written, log when it disagrees with the canonical store.
    After >=1 week of clean logs on a live environment, HealthTrend writes
    can be stopped (Release 2 of the drain plan). Non-fatal by design."""
    try:
        from models import HealthTrend
        trend = (
            HealthTrend.query.filter_by(account_id=account_id)
            .order_by(HealthTrend.year.desc(), HealthTrend.month.desc())
            .first()
        )
        if trend is None or trend.overall_health_score is None:
            return
        legacy = float(trend.overall_health_score)
        if abs(legacy - canonical_score) > DIVERGENCE_TOLERANCE_PTS:
            logger.warning(
                "health_store_divergence account=%s health_scores=%.1f "
                "health_trends=%.1f (delta=%.1f) — legacy store stale; "
                "see AUDIT_REMEDIATION_WAVE1_SPEC.md A2",
                account_id, canonical_score, legacy, legacy - canonical_score,
            )
    except Exception:
        pass
