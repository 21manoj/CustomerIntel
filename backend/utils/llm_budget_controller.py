"""
Centralized LLM Budget Controller
===================================

ALL LLM callers must check this module before making API calls.
Provides per-customer daily/monthly budget caps with circuit breaker logic.

Usage:
    from utils.llm_budget_controller import can_call, record_usage, get_usage_summary

    if can_call(customer_id, 'signal_analyst'):
        # ... make LLM call ...
        record_usage(customer_id, 'signal_analyst', tokens_in=500, tokens_out=200, model='claude-sonnet-4-6')

Design:
    - Thread-safe: all state in PostgreSQL (no in-memory counters)
    - Fail-open: if budget check errors, the LLM call proceeds
    - Non-fatal: exceptions are caught and logged, never propagated
"""

import logging
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ── Default budget limits ──────────────────────────────────────────
DEFAULT_DAILY_LLM_CALLS = 200
DEFAULT_DAILY_LLM_BUDGET_USD = 10.0
DEFAULT_MONTHLY_LLM_BUDGET_USD = 200.0
DEFAULT_MAX_PROACTIVE_CALLS_PER_RUN = 50

# ── Cost rates (approximate, per million tokens) ───────────────────
COST_RATES = {
    'claude-sonnet-4-6': {'input_per_mtok': 3.0, 'output_per_mtok': 15.0},
    # Retired Sonnet-4 snapshot — kept for cost recompute over historical llm_usage_log rows.
    'claude-sonnet-4-20250514': {'input_per_mtok': 3.0, 'output_per_mtok': 15.0},
    'claude-3-5-sonnet-20241022': {'input_per_mtok': 3.0, 'output_per_mtok': 15.0},
    'gpt-4o-mini': {'input_per_mtok': 0.15, 'output_per_mtok': 0.60},
    'gpt-4o': {'input_per_mtok': 2.50, 'output_per_mtok': 10.0},
}
DEFAULT_COST_RATE = {'input_per_mtok': 3.0, 'output_per_mtok': 15.0}

# ── Circuit breaker thresholds ─────────────────────────────────────
WARNING_THRESHOLD_PCT = 80
BLOCK_THRESHOLD_PCT = 100


def _get_budget_limits(customer_id: int) -> dict:
    """
    Load per-customer budget limits from CustomerConfig, with defaults.

    Budget fields are stored in CustomerConfig.dc2s_kpi_overrides JSON
    under the key 'llm_budget' to avoid adding new columns:
      {"llm_budget": {"daily_calls": 300, "daily_usd": 15, "monthly_usd": 300, "max_proactive": 80}}
    """
    try:
        from models import CustomerConfig
        config = CustomerConfig.query.filter_by(customer_id=customer_id).first()
        if config and config.dc2s_kpi_overrides:
            overrides = config.dc2s_kpi_overrides
            if isinstance(overrides, dict):
                llm = overrides.get('llm_budget', {})
                return {
                    'daily_calls': llm.get('daily_calls', DEFAULT_DAILY_LLM_CALLS),
                    'daily_usd': llm.get('daily_usd', DEFAULT_DAILY_LLM_BUDGET_USD),
                    'monthly_usd': llm.get('monthly_usd', DEFAULT_MONTHLY_LLM_BUDGET_USD),
                    'max_proactive': llm.get('max_proactive', DEFAULT_MAX_PROACTIVE_CALLS_PER_RUN),
                }
    except Exception as e:
        logger.debug(f"LLM budget: could not load customer {customer_id} config: {e}")

    return {
        'daily_calls': DEFAULT_DAILY_LLM_CALLS,
        'daily_usd': DEFAULT_DAILY_LLM_BUDGET_USD,
        'monthly_usd': DEFAULT_MONTHLY_LLM_BUDGET_USD,
        'max_proactive': DEFAULT_MAX_PROACTIVE_CALLS_PER_RUN,
    }


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate cost in USD for a given model and token counts."""
    rates = COST_RATES.get(model, DEFAULT_COST_RATE)
    cost = (tokens_in / 1_000_000) * rates['input_per_mtok'] + \
           (tokens_out / 1_000_000) * rates['output_per_mtok']
    return round(cost, 6)


def _get_daily_usage(customer_id: int) -> dict:
    """Get today's usage aggregates from the llm_usage_log table."""
    try:
        from extensions import db
        today = date.today()
        result = db.session.execute(db.text("""
            SELECT
                COUNT(*) AS call_count,
                COALESCE(SUM(cost_estimate_usd), 0) AS total_cost,
                COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
                COALESCE(SUM(tokens_out), 0) AS total_tokens_out
            FROM llm_usage_log
            WHERE customer_id = :cid AND created_at::date = :today
        """), {'cid': customer_id, 'today': today}).fetchone()

        return {
            'call_count': int(result[0]),
            'total_cost_usd': float(result[1]),
            'total_tokens_in': int(result[2]),
            'total_tokens_out': int(result[3]),
        }
    except Exception as e:
        logger.debug(f"LLM budget: daily usage query failed for customer {customer_id}: {e}")
        return {'call_count': 0, 'total_cost_usd': 0.0, 'total_tokens_in': 0, 'total_tokens_out': 0}


def _get_monthly_usage(customer_id: int) -> dict:
    """Get current month's usage aggregates from the llm_usage_log table."""
    try:
        from extensions import db
        today = date.today()
        month_start = today.replace(day=1)
        result = db.session.execute(db.text("""
            SELECT
                COUNT(*) AS call_count,
                COALESCE(SUM(cost_estimate_usd), 0) AS total_cost,
                COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
                COALESCE(SUM(tokens_out), 0) AS total_tokens_out
            FROM llm_usage_log
            WHERE customer_id = :cid AND created_at >= :month_start
        """), {'cid': customer_id, 'month_start': month_start}).fetchone()

        return {
            'call_count': int(result[0]),
            'total_cost_usd': float(result[1]),
            'total_tokens_in': int(result[2]),
            'total_tokens_out': int(result[3]),
        }
    except Exception as e:
        logger.debug(f"LLM budget: monthly usage query failed for customer {customer_id}: {e}")
        return {'call_count': 0, 'total_cost_usd': 0.0, 'total_tokens_in': 0, 'total_tokens_out': 0}


def can_call(customer_id: int, module: str, estimated_tokens: int = 1000) -> bool:
    """
    Check whether an LLM call is allowed for this customer.

    Returns True if within budget, False if over budget.
    FAIL-OPEN: if the check itself errors, returns True (allows the call).

    Args:
        customer_id:      Customer (tenant) ID.
        module:           Caller module name (e.g. 'signal_analyst', 'ask_ai_v2').
        estimated_tokens: Estimated total tokens for cost pre-check.
    """
    try:
        limits = _get_budget_limits(customer_id)
        daily = _get_daily_usage(customer_id)

        # ── Check daily call count ──
        daily_calls_pct = (daily['call_count'] / limits['daily_calls'] * 100) if limits['daily_calls'] > 0 else 0
        if daily_calls_pct >= BLOCK_THRESHOLD_PCT:
            logger.warning(
                f"LLM budget: BLOCKED customer {customer_id} module={module} — "
                f"daily calls {daily['call_count']}/{limits['daily_calls']} (100%)"
            )
            return False

        # ── Check daily spend ──
        daily_spend_pct = (daily['total_cost_usd'] / limits['daily_usd'] * 100) if limits['daily_usd'] > 0 else 0
        if daily_spend_pct >= BLOCK_THRESHOLD_PCT:
            logger.warning(
                f"LLM budget: BLOCKED customer {customer_id} module={module} — "
                f"daily spend ${daily['total_cost_usd']:.4f}/${limits['daily_usd']:.2f} (100%)"
            )
            return False

        # ── Check monthly spend ──
        monthly = _get_monthly_usage(customer_id)
        monthly_spend_pct = (monthly['total_cost_usd'] / limits['monthly_usd'] * 100) if limits['monthly_usd'] > 0 else 0
        if monthly_spend_pct >= BLOCK_THRESHOLD_PCT:
            logger.warning(
                f"LLM budget: BLOCKED customer {customer_id} module={module} — "
                f"monthly spend ${monthly['total_cost_usd']:.4f}/${limits['monthly_usd']:.2f} (100%)"
            )
            return False

        # ── Warning at 80% threshold ──
        if daily_calls_pct >= WARNING_THRESHOLD_PCT or daily_spend_pct >= WARNING_THRESHOLD_PCT:
            logger.warning(
                f"LLM budget: customer {customer_id} at "
                f"{max(daily_calls_pct, daily_spend_pct):.0f}% daily limit "
                f"({daily['call_count']}/{limits['daily_calls']} calls, "
                f"${daily['total_cost_usd']:.4f}/${limits['daily_usd']:.2f})"
            )

        if monthly_spend_pct >= WARNING_THRESHOLD_PCT:
            logger.warning(
                f"LLM budget: customer {customer_id} at {monthly_spend_pct:.0f}% monthly limit "
                f"(${monthly['total_cost_usd']:.4f}/${limits['monthly_usd']:.2f})"
            )

        return True

    except Exception as e:
        # Fail-open: if budget check fails, allow the call
        logger.error(f"LLM budget: can_call check failed for customer {customer_id}: {e}", exc_info=True)
        return True


def record_usage(
    customer_id: int,
    module: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str = 'claude-sonnet-4-6',
    cost_estimate: Optional[float] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """
    Log an LLM call to the llm_usage_log table.

    Non-fatal: if logging fails, the error is logged but not raised.

    Args:
        customer_id:   Customer (tenant) ID.
        module:        Caller module name.
        tokens_in:     Input token count.
        tokens_out:    Output token count.
        model:         Model identifier.
        cost_estimate: Pre-computed cost in USD. If None, auto-estimated.
        success:       Whether the LLM call succeeded.
        error_message: Error message if the call failed.
    """
    try:
        from extensions import db

        if cost_estimate is None:
            cost_estimate = estimate_cost(model, tokens_in, tokens_out)

        db.session.execute(db.text("""
            INSERT INTO llm_usage_log
                (customer_id, module, model, tokens_in, tokens_out, cost_estimate_usd, success, error_message)
            VALUES
                (:cid, :module, :model, :tokens_in, :tokens_out, :cost, :success, :error_msg)
        """), {
            'cid': customer_id,
            'module': module,
            'model': model,
            'tokens_in': tokens_in,
            'tokens_out': tokens_out,
            'cost': cost_estimate,
            'success': success,
            'error_msg': error_message,
        })
        db.session.commit()

        logger.debug(
            f"LLM budget: recorded {module} call for customer {customer_id} — "
            f"{tokens_in}in/{tokens_out}out, ${cost_estimate:.6f}, model={model}"
        )

    except Exception as e:
        logger.error(f"LLM budget: record_usage failed for customer {customer_id}: {e}", exc_info=True)
        try:
            from extensions import db as _db
            _db.session.rollback()
        except Exception:
            pass


def get_usage_summary(customer_id: int, period: str = 'daily') -> dict:
    """
    Get usage statistics for a customer.

    Args:
        customer_id: Customer (tenant) ID.
        period:      'daily' or 'monthly'.

    Returns:
        Dict with call_count, total_cost_usd, total_tokens_in, total_tokens_out,
        plus per-module breakdown.
    """
    try:
        from extensions import db

        if period == 'monthly':
            today = date.today()
            start = today.replace(day=1)
            date_filter = "created_at >= :start"
            params = {'cid': customer_id, 'start': start}
        else:
            today = date.today()
            date_filter = "created_at::date = :today"
            params = {'cid': customer_id, 'today': today}

        # Aggregate totals
        totals = _get_daily_usage(customer_id) if period == 'daily' else _get_monthly_usage(customer_id)

        # Per-module breakdown
        rows = db.session.execute(db.text(f"""
            SELECT
                module,
                COUNT(*) AS call_count,
                COALESCE(SUM(cost_estimate_usd), 0) AS total_cost,
                COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
                COALESCE(SUM(tokens_out), 0) AS total_tokens_out
            FROM llm_usage_log
            WHERE customer_id = :cid AND {date_filter}
            GROUP BY module
            ORDER BY total_cost DESC
        """), params).fetchall()

        by_module = {}
        for row in rows:
            by_module[row[0]] = {
                'call_count': int(row[1]),
                'total_cost_usd': float(row[2]),
                'total_tokens_in': int(row[3]),
                'total_tokens_out': int(row[4]),
            }

        return {
            'period': period,
            'customer_id': customer_id,
            **totals,
            'by_module': by_module,
        }

    except Exception as e:
        logger.error(f"LLM budget: get_usage_summary failed for customer {customer_id}: {e}", exc_info=True)
        return {
            'period': period,
            'customer_id': customer_id,
            'call_count': 0,
            'total_cost_usd': 0.0,
            'total_tokens_in': 0,
            'total_tokens_out': 0,
            'by_module': {},
        }


def get_budget_status(customer_id: int) -> dict:
    """
    Get remaining budget and percentage used.

    Returns:
        Dict with daily and monthly budget status including remaining amounts
        and percentage used.
    """
    try:
        limits = _get_budget_limits(customer_id)
        daily = _get_daily_usage(customer_id)
        monthly = _get_monthly_usage(customer_id)

        daily_calls_pct = round(daily['call_count'] / limits['daily_calls'] * 100, 1) if limits['daily_calls'] > 0 else 0
        daily_spend_pct = round(daily['total_cost_usd'] / limits['daily_usd'] * 100, 1) if limits['daily_usd'] > 0 else 0
        monthly_spend_pct = round(monthly['total_cost_usd'] / limits['monthly_usd'] * 100, 1) if limits['monthly_usd'] > 0 else 0

        return {
            'customer_id': customer_id,
            'daily': {
                'calls_used': daily['call_count'],
                'calls_limit': limits['daily_calls'],
                'calls_remaining': max(0, limits['daily_calls'] - daily['call_count']),
                'calls_pct': daily_calls_pct,
                'spend_usd': round(daily['total_cost_usd'], 4),
                'spend_limit_usd': limits['daily_usd'],
                'spend_remaining_usd': round(max(0, limits['daily_usd'] - daily['total_cost_usd']), 4),
                'spend_pct': daily_spend_pct,
            },
            'monthly': {
                'calls_used': monthly['call_count'],
                'spend_usd': round(monthly['total_cost_usd'], 4),
                'spend_limit_usd': limits['monthly_usd'],
                'spend_remaining_usd': round(max(0, limits['monthly_usd'] - monthly['total_cost_usd']), 4),
                'spend_pct': monthly_spend_pct,
            },
            'limits': limits,
            'circuit_breaker': {
                'warning_pct': WARNING_THRESHOLD_PCT,
                'block_pct': BLOCK_THRESHOLD_PCT,
                'daily_calls_status': 'blocked' if daily_calls_pct >= BLOCK_THRESHOLD_PCT
                                      else 'warning' if daily_calls_pct >= WARNING_THRESHOLD_PCT
                                      else 'ok',
                'daily_spend_status': 'blocked' if daily_spend_pct >= BLOCK_THRESHOLD_PCT
                                      else 'warning' if daily_spend_pct >= WARNING_THRESHOLD_PCT
                                      else 'ok',
                'monthly_spend_status': 'blocked' if monthly_spend_pct >= BLOCK_THRESHOLD_PCT
                                        else 'warning' if monthly_spend_pct >= WARNING_THRESHOLD_PCT
                                        else 'ok',
            },
        }

    except Exception as e:
        logger.error(f"LLM budget: get_budget_status failed for customer {customer_id}: {e}", exc_info=True)
        return {
            'customer_id': customer_id,
            'error': str(e),
            'daily': {'calls_used': 0, 'calls_limit': DEFAULT_DAILY_LLM_CALLS, 'spend_usd': 0},
            'monthly': {'calls_used': 0, 'spend_usd': 0, 'spend_limit_usd': DEFAULT_MONTHLY_LLM_BUDGET_USD},
            'limits': _get_budget_limits(customer_id),
        }


def get_max_proactive_calls(customer_id: int) -> int:
    """Get the max proactive calls per run for a customer."""
    limits = _get_budget_limits(customer_id)
    return limits['max_proactive']


# ════════════════════════════════════════════════════════════════════
# llm_call() — the one-call-site wrapper that forces cost tracking
# ════════════════════════════════════════════════════════════════════
# Background: Apr 20, 2026 — 6 production files were making raw
# `client.messages.create(...)` / `chat.completions.create(...)` calls
# without invoking record_usage(). $0.45 of real Anthropic spend was
# invisible to the budget dashboard. A per-call-site memory warning
# ("remember to call record_usage") failed — the next engineer
# forgets and the leak recurs.
#
# llm_call() makes the contract unbypassable at the language level:
# every success path emits record_usage on the way out; every exception
# path emits record_usage with success=False. Caller passes the client,
# module name, customer_id, model, and API kwargs — gets back the raw
# response object to handle like a normal SDK call.
#
# A grep-based pre-commit hook (scripts/check_llm_wrapper.sh) bans
# direct .messages.create / .chat.completions.create calls outside
# this function + the approved list of existing sites.


def llm_call(
    client,
    customer_id: int,
    module: str,
    model: str,
    **api_kwargs,
):
    """Wrapper around LLM SDK calls that forces record_usage on every exit.

    Supports both Anthropic (`client.messages.create`) and OpenAI
    (`client.chat.completions.create`). Auto-detects by client type.

    Args:
        client:       anthropic.Anthropic or OpenAI client instance
        customer_id:  tenant ID — passed to record_usage
        module:       caller name (e.g. 'signal_analyst', 'tier1_enrichment')
        model:        model identifier (e.g. 'claude-sonnet-4-6')
        **api_kwargs: passed through to the underlying SDK method
                      (messages, system, max_tokens, temperature, etc.)

    Returns:
        The raw SDK response object (caller parses .content / .choices).

    Raises:
        Whatever the SDK raises — but record_usage(success=False) is emitted
        before the exception propagates.
    """
    # Detect SDK flavor.
    is_anthropic = hasattr(client, 'messages') and hasattr(client.messages, 'create')
    is_openai = (
        hasattr(client, 'chat')
        and hasattr(client.chat, 'completions')
        and hasattr(client.chat.completions, 'create')
    )

    if not is_anthropic and not is_openai:
        raise TypeError(
            f'llm_call(): unrecognized client type {type(client).__name__}. '
            f'Expected anthropic.Anthropic or openai.OpenAI.'
        )

    try:
        if is_anthropic:
            response = client.messages.create(model=model, **api_kwargs)
            usage = getattr(response, 'usage', None)
            tokens_in = getattr(usage, 'input_tokens', 0) if usage else 0
            tokens_out = getattr(usage, 'output_tokens', 0) if usage else 0
        else:  # openai
            response = client.chat.completions.create(model=model, **api_kwargs)
            usage = getattr(response, 'usage', None)
            tokens_in = getattr(usage, 'prompt_tokens', 0) if usage else 0
            tokens_out = getattr(usage, 'completion_tokens', 0) if usage else 0

        # Success path — always record.
        try:
            record_usage(
                customer_id=customer_id,
                module=module,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                model=model,
                success=True,
            )
        except Exception as _rec_err:
            logger.debug(f'llm_call: record_usage failed (success path): {_rec_err}')

        return response

    except Exception as e:
        # Failure path — record with success=False, re-raise.
        try:
            record_usage(
                customer_id=customer_id,
                module=module,
                tokens_in=0,
                tokens_out=0,
                model=model,
                success=False,
                error_message=str(e)[:500],
            )
        except Exception as _rec_err:
            logger.debug(f'llm_call: record_usage failed (failure path): {_rec_err}')
        raise


def can_call_and_run(
    client,
    customer_id: int,
    module: str,
    model: str,
    estimated_tokens: int = 1000,
    **api_kwargs,
):
    """llm_call() + budget gate. Raises PermissionError if budget exceeded.

    Convenience helper for callers that want both the record_usage
    enforcement AND the circuit-breaker budget check in one call.
    """
    if not can_call(customer_id, module, estimated_tokens=estimated_tokens):
        raise PermissionError(
            f'LLM budget exceeded for customer {customer_id} module={module}. '
            f'Circuit breaker is tripped — see get_budget_status.'
        )
    return llm_call(client, customer_id, module, model, **api_kwargs)
