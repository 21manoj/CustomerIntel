"""
CS Pulse MCP — CSM scorecard / capacity / daily actions / ranking (roi/csm.py).

    get_csm_scorecard(customer_id, csm_user_id)
    get_team_capacity(customer_id)
    get_csm_daily_actions(customer_id, csm_user_id=None, top_n=10)
    get_csm_ranking(customer_id, metric='avg_health_weighted')

Reads only. Keyed over HTTP (onboarding_tool_registry.KEYED_TOOLS); the
computation lives in roi/csm.py, sharing roi.priorities.investment_priorities
and playbooks.governance.list_interventions with the rest of roi/ rather than
re-deriving risk, revenue exposure or intervention state.

CSM identity: this schema has no separate CSM entity. A CSM is a User row
with role == 'csm'; their book is User.allowed_account_ids — the same column
app_api.auth.allows_account() already uses to gate the session UI (unset =
every account the tenant has; an explicit list, including an empty one, is
exactly that list). A tenant with no role='csm' users gets an honest empty
result ('no_csm_users') from get_team_capacity / get_csm_ranking, never a
fabricated roster — see roi/csm.py's module docstring for the full reasoning.
"""
from mcp_server.cs_pulse_mcp_server import mcp, _check_mcp_enabled, _get_flask_app, ToolError
from mcp_server.auth import require_auth_if_key_present as _require_auth_if_key_present


def _read(customer_id, fn):
    _check_mcp_enabled()
    app = _get_flask_app()
    with app.app_context():
        from models import Customer
        from extensions import db
        if not db.session.get(Customer, int(customer_id)):
            raise ToolError(f'Customer {customer_id} not found.')
        try:
            return fn()
        except ValueError as e:                     # unknown CSM / wrong role / unknown vertical: fail closed, say why
            raise ToolError(str(e))


@mcp.tool
def get_csm_scorecard(customer_id: int, csm_user_id: int) -> dict:
    """One CSM's book, in real numbers. csm_user_id must be a User row on THIS
    customer_id with role == 'csm' (raises if missing, foreign, or a different
    role) — their book is User.allowed_account_ids: unset means every account
    in the tenant (book.scope says which), else exactly that list.

    revenue_managed is Σ Account.revenue over the book (derived). health is
    each book account's LATEST HealthScore only: accounts_scored /
    accounts_unscored, by_band (healthy/at_risk/critical, health_thresholds.json),
    avg_health_score_revenue_weighted (revenue-weighted average, None if
    nothing is scored yet), and by_early_warning_label — the two-layer
    leading-vs-trailing label (aligned/early_warning/recovery_watch/
    leading_only/none) read off each account's own journey, the same field
    get_investment_priorities' factors.leading already carries; never
    re-derived here. risk_opportunity is get_investment_priorities' own rows
    filtered to this book — revenue_weighted_total and addressable_weighted_total
    (rank, don't sum, across accounts — inherited caveat), by_lens,
    pending_approvals, and the book's top 5 accounts by addressable_weighted
    (roi.priorities.compact, plus account_id/name); accounts_without_journey
    haven't been through Wizard A yet, not scored 0. interventions is
    playbooks.governance.list_interventions filtered to this book: counts by
    state, stuck / delivery_problems, realized_revenue (measured, cited
    outcome nodes) vs exposure_revenue_open (derived, account revenue on rows
    still open) — two numbers, never summed, matching get_roi.

    Args:
        customer_id: The customer ID
        csm_user_id: A User.user_id on this customer with role == 'csm'
    """
    _require_auth_if_key_present('get_csm_scorecard', customer_id)
    from roi.csm import csm_scorecard
    return _read(customer_id, lambda: csm_scorecard(int(customer_id), int(csm_user_id)))


@mcp.tool
def get_team_capacity(customer_id: int) -> dict:
    """Workload and coverage across every ACTIVE role='csm' User of this
    tenant — status is 'no_csm_users' (csms: [], full coverage gap reported
    against the whole tenant) rather than an error or a fabricated roster
    when none exist yet.

    Per CSM: book_scope + account_count (User.allowed_account_ids, unset =
    entire tenant), revenue_managed, avg_health_score_revenue_weighted,
    by_band, and three workload proxies read straight off their book's
    Intervention rows — open_interventions (proposed+approved+sent),
    pending_approvals, stuck_interventions — plus addressable_weighted_total
    (get_investment_priorities' own figure, filtered to the book). There is
    no capacity/target-accounts config anywhere in this schema, so no
    over/under-capacity verdict is computed; compare the numbers yourself.

    coverage: tenant_accounts vs covered_accounts (the union of every CSM's
    book), uncovered_account_ids (in no CSM's book — a real gap, surfaced,
    never guessed at), accounts_covered_by_multiple_csms, and whether any
    CSM's book is the unset (entire-portfolio) case, which alone makes
    coverage total.

    Args:
        customer_id: The customer ID
    """
    _require_auth_if_key_present('get_team_capacity', customer_id)
    from roi.csm import team_capacity
    return _read(customer_id, lambda: team_capacity(int(customer_id)))


@mcp.tool
def get_csm_daily_actions(customer_id: int, csm_user_id: int = None, top_n: int = 10) -> dict:
    """The next actions, ranked — reuses get_investment_priorities' own
    addressable_weighted ranking rather than a second scoring pass. Omit
    csm_user_id for the whole portfolio's action list; pass it to scope to
    one CSM's book (User.allowed_account_ids on that role='csm' user; raises
    if the id is missing, foreign, or not a CSM).

    Each row of get_investment_priorities becomes an action only when there
    is something new to decide today: 'approve_intervention' when the
    account has a proposed intervention waiting; 'evaluate_playbook' (lens
    protect) or 'pursue_expansion' (lens grow) when nothing is open yet and
    the account clears power_of_1.json's list_floor. An account with an
    open, non-stuck intervention already in flight gets no action — nothing
    new to do. actions is capped at top_n (ranked by addressable_weighted,
    descending); actions_total_before_limit is the uncapped count.

    stuck_interventions is separate and NOT capped: every sent-but-unreported
    or delivery-failed row in scope (playbooks.governance's own stuck /
    delivery_problem flags), regardless of the account's current priority —
    a stalled intervention needs attention on its own timeline. status
    mirrors get_investment_priorities' (e.g. 'no_journeys' with its hint,
    actions empty) when the tenant has no journeys yet.

    Args:
        customer_id: The customer ID
        csm_user_id: Scope to one CSM's book (optional; omit for the whole portfolio)
        top_n: Max rows in `actions` (default 10); stuck_interventions is never truncated
    """
    _require_auth_if_key_present('get_csm_daily_actions', customer_id)
    from roi.csm import csm_daily_actions
    return _read(customer_id, lambda: csm_daily_actions(int(customer_id), int(csm_user_id) if csm_user_id is not None else None, int(top_n)))


@mcp.tool
def get_csm_ranking(customer_id: int, metric: str = 'avg_health_weighted') -> dict:
    """A leaderboard of this tenant's ACTIVE role='csm' Users, sorted
    descending by `metric` (rank 1 = highest; None sorts last). status is
    'no_csm_users' (ranked: []) rather than an error or a fabricated roster
    when none exist.

    metric is one of: avg_health_weighted (revenue-weighted average of each
    book's latest HealthScore — higher reads healthier), revenue_managed (Σ
    Account.revenue over the book — higher is bigger), addressable_risk_total
    (get_investment_priorities' addressable_weighted, summed over the book —
    higher means MORE at-risk revenue remains there, i.e. needs the most
    attention, NOT a performance score), or realized_revenue (measured $
    from that book's closed-done interventions with a reported outcome —
    higher means more has been protected/expanded so far; None, not zero,
    when nothing has been reported).

    This reflects the CURRENT state of each CSM's assigned book — book size
    and starting difficulty (which accounts they were handed) are not
    normalized for, so it is not a measure of skill or effort in isolation,
    only of where each book stands today. Raises for an unrecognized metric.

    Args:
        customer_id: The customer ID
        metric: One of avg_health_weighted | revenue_managed | addressable_risk_total | realized_revenue
    """
    _require_auth_if_key_present('get_csm_ranking', customer_id)
    from roi.csm import csm_ranking
    return _read(customer_id, lambda: csm_ranking(int(customer_id), str(metric)))
