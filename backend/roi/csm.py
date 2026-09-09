"""
CSM scorecard / team capacity / daily actions / ranking.

    csm_scorecard(customer_id, csm_user_id)                    one CSM's book, real numbers
    team_capacity(customer_id)                                 every active CSM, workload + coverage gaps
    csm_daily_actions(customer_id, csm_user_id=None, top_n=10) next actions, ranked (reuses roi.priorities)
    csm_ranking(customer_id, metric='avg_health_weighted')     leaderboard across role='csm' users

CSM identity (2026-09-09): this schema has no separate CSM entity/table. A
CSM is a `User` row with role == 'csm'; their book is `User.allowed_account_ids`
— the SAME column app_api.auth.allows_account() already reads to gate the
session UI (reused here via that function, not reimplemented): unset (None)
means every account the tenant has (a tenant-wide CSM), an explicit list
means exactly that list, and an explicit EMPTY list means none (fail-closed,
matching allows_account's own semantics exactly). Nothing here invents a
roster: a tenant with no role='csm' users gets an honest empty result
('no_csm_users'), never a synthetic one.

Every number is grounded in real rows and carries a basis (measured |
derived | assumed) via roi.basis.money(), matching the rest of roi/. Risk,
opportunity and revenue exposure are never re-derived here — they come from
roi.priorities.investment_priorities() and playbooks.governance.list_interventions(),
each called ONCE per tool (portfolio-wide) and filtered down to the relevant
book in Python, not re-queried per account or per CSM (the N+1 pattern
already found twice elsewhere in this codebase — Wizard C calibration and
playbook governance evaluation).
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional

import utils.health_thresholds as ht
from roi.basis import money

RANKING_METRICS = ('avg_health_weighted', 'revenue_managed', 'addressable_risk_total', 'realized_revenue')

_EMPTY_BY_STATE = {'proposed': 0, 'approved': 0, 'sent': 0, 'closed_done': 0, 'closed_failed': 0, 'closed_cancelled': 0}
_EMPTY_BY_BAND = {'healthy': 0, 'at_risk': 0, 'critical': 0}


# ── CSM identity + book (the only place this module touches User) ──────

def _csm_user(customer_id: int, csm_user_id: int):
    """The tenant's own User row for csm_user_id, role == 'csm'. Raises ValueError (a tool-boundary
    ToolError, matching every other roi.* fail-closed lookup) for a missing id, another tenant's id,
    or a non-CSM role — never silently substitutes another user or crosses a tenant boundary."""
    from models import User
    u = User.query.filter_by(user_id=int(csm_user_id), customer_id=int(customer_id)).first()
    if u is None:
        raise ValueError(f'user {csm_user_id} not found for customer {customer_id}')
    if u.role != 'csm':
        raise ValueError(f"user {csm_user_id} has role {u.role!r}, not 'csm' — the get_csm_* tools require a role='csm' user")
    return u


def _book(customer_id: int, user) -> dict:
    """{account_ids, scope} — this CSM's book, using the EXACT semantics app_api.auth.allows_account
    already enforces on the session UI (imported and reused, not reimplemented here): an unset
    allowed_account_ids is every account of the tenant; an explicit list (including an empty one,
    meaning none) is exactly that list, filtered to accounts that actually belong to this tenant."""
    from models import Account
    from app_api.auth import allows_account
    tenant_accounts = Account.query.filter_by(customer_id=int(customer_id)).order_by(Account.account_id).all()
    if user.allowed_account_ids is None:
        return {'account_ids': [a.account_id for a in tenant_accounts], 'scope': 'entire_portfolio (allowed_account_ids unset)'}
    ids = [a.account_id for a in tenant_accounts if allows_account(user, a.account_id)]
    scope = 'explicit allowed_account_ids' if user.allowed_account_ids else 'explicit empty allowed_account_ids (sees no accounts)'
    return {'account_ids': ids, 'scope': scope}


# ── the one per-book computation every tool shares ──────────────────────

def _latest_health_by_account(account_ids: List[int]) -> Dict[int, object]:
    """One HealthScore per account_id — the latest measurement_month — in ONE query
    (account_id.in_), not N. Mirrors mcp_server/process_data_pipeline.py's _sync_account_status."""
    from models import HealthScore
    if not account_ids:
        return {}
    out: Dict[int, object] = {}
    for hs in HealthScore.query.filter(HealthScore.account_id.in_(account_ids)).order_by(
            HealthScore.account_id, HealthScore.measurement_month.desc()).all():
        out.setdefault(hs.account_id, hs)
    return out


def _rollup(customer_id: int, account_ids: List[int]) -> dict:
    """revenue managed, health distribution (+ the two-layer early-warning label, read straight off
    roi.priorities' own journey scoring — never re-derived), and intervention/outcome performance,
    for exactly this set of accounts. Two portfolio-wide queries regardless of book size
    (roi.priorities.investment_priorities, playbooks.governance.list_interventions), filtered down
    here — never re-run per account."""
    from roi.priorities import investment_priorities, compact

    ids = set(int(a) for a in account_ids)
    if not ids:
        empty = money(0.0, 'derived', ['derived: empty book'])
        return {
            'revenue_managed': empty,
            'health': {'accounts_scored': 0, 'accounts_unscored': [], 'avg_health_score_revenue_weighted': None,
                       'by_band': dict(_EMPTY_BY_BAND), 'by_early_warning_label': {}},
            'risk_opportunity': {'status': 'not_applicable', 'accounts_with_journey': 0, 'accounts_without_journey': [],
                                 'revenue_weighted_total': empty, 'addressable_weighted_total': empty,
                                 'by_lens': {'protect': 0, 'grow': 0}, 'pending_approvals': 0, 'top_accounts': []},
            'interventions': {'total': 0, 'by_state': dict(_EMPTY_BY_STATE), 'stuck': 0, 'delivery_problems': 0,
                              'realized_revenue': money(None, 'measured', note='empty book'), 'exposure_revenue_open': empty},
        }

    from models import Account
    from mcp_server.common import get_account_arr
    accounts = {a.account_id: a for a in Account.query.filter(Account.account_id.in_(ids)).all()}
    revenue_managed = sum(get_account_arr(a) for a in accounts.values())

    health_by_account = _latest_health_by_account(list(ids))
    by_band = dict(_EMPTY_BY_BAND)
    scored_revenue = weighted_health = 0.0
    for aid, hs in health_by_account.items():
        if hs.health_score is None:
            continue
        by_band[ht.classify(float(hs.health_score))] += 1
        rev = get_account_arr(accounts[aid]) if aid in accounts else 0.0
        weighted_health += float(hs.health_score) * rev
        scored_revenue += rev
    avg_health_weighted = round(weighted_health / scored_revenue, 2) if scored_revenue > 0 else None
    unscored = sorted(ids - set(health_by_account))

    pr = investment_priorities(int(customer_id))
    pr_rows = [r for r in pr.get('rows', []) if r['account_id'] in ids] if pr['status'] == 'ok' else []
    by_ew: Dict[str, int] = {}
    for r in pr_rows:
        label = r['factors']['leading']['label'] or 'none'
        by_ew[label] = by_ew.get(label, 0) + 1
    revenue_weighted_total = sum(r['revenue_weighted']['value'] for r in pr_rows)
    addressable_weighted_total = sum(r['addressable_weighted']['value'] for r in pr_rows)
    addressable_assumed = any(r['addressable_weighted']['basis'] == 'assumed' for r in pr_rows)
    addressable_chain = ['derived: Σ addressable_weighted over the book (roi.priorities.investment_priorities)']
    if addressable_assumed:
        addressable_chain.append("assumed: at least one account's discount is benchmark-sourced "
                                  '(see that account\'s own addressable_weighted for its citation)')
    by_lens = {'protect': sum(1 for r in pr_rows if r['lens'] == 'protect'), 'grow': sum(1 for r in pr_rows if r['lens'] == 'grow')}
    pending_approvals = sum(r['pending_approvals'] for r in pr_rows)
    accounts_without_journey = sorted(ids - {r['account_id'] for r in pr_rows})
    top_accounts = [{'account_id': r['account_id'], 'account_name': r['account_name'], **compact(r)}
                     for r in sorted(pr_rows, key=lambda r: -r['addressable_weighted']['value'])[:5]]

    from playbooks.governance import list_interventions
    li = list_interventions(int(customer_id))
    views = [v for v in li['interventions'] if v['account_id'] in ids]
    by_state = dict(_EMPTY_BY_STATE)
    stuck = delivery_problems = 0
    realized = 0.0
    realized_ids: List[int] = []
    exposure_open = 0.0
    for v in views:
        if v['state'] == 'closed':
            by_state[f"closed_{v['closed_state']}"] += 1
        else:
            by_state[v['state']] += 1
            if v.get('exposure_revenue') is not None:
                exposure_open += float(v['exposure_revenue'])
        stuck += int(bool(v['stuck']))
        delivery_problems += int(bool(v['delivery_problem']))
        oc = v.get('outcome') or {}
        if oc.get('revenue') is not None:
            realized += float(oc['revenue'])
            realized_ids.append(oc['node_id'])

    return {
        'revenue_managed': money(revenue_managed, 'derived', ['derived: Σ Account.revenue over the book (get_account_arr)']),
        'health': {'accounts_scored': len(health_by_account), 'accounts_unscored': unscored,
                   'avg_health_score_revenue_weighted': avg_health_weighted, 'by_band': by_band, 'by_early_warning_label': by_ew},
        'risk_opportunity': {
            'status': pr['status'], 'accounts_with_journey': len(pr_rows), 'accounts_without_journey': accounts_without_journey,
            'revenue_weighted_total': money(revenue_weighted_total, 'derived', ['derived: Σ revenue_weighted over the book (roi.priorities.investment_priorities)']),
            'addressable_weighted_total': money(addressable_weighted_total, 'assumed' if addressable_assumed else 'derived', addressable_chain),
            'by_lens': by_lens, 'pending_approvals': pending_approvals, 'top_accounts': top_accounts,
        },
        'interventions': {
            'total': len(views), 'by_state': by_state, 'stuck': stuck, 'delivery_problems': delivery_problems,
            'realized_revenue': money(realized, 'measured', [f'measured: outcome nodes {realized_ids}']) if realized_ids
                                else money(None, 'measured', note="no outcome reported yet on this book's interventions"),
            'exposure_revenue_open': money(exposure_open, 'derived', ['derived: account revenue on open intervention rows']),
        },
    }


# ── the four tools ───────────────────────────────────────────────────────

def csm_scorecard(customer_id: int, csm_user_id: int) -> dict:
    from journeys.read import origin_block
    user = _csm_user(customer_id, csm_user_id)
    book = _book(customer_id, user)
    roll = _rollup(customer_id, book['account_ids'])
    return {
        'customer_id': int(customer_id), **origin_block(customer_id),
        'csm': {'user_id': user.user_id, 'name': user.user_name, 'email': user.email, 'active': user.active},
        'book': {'scope': book['scope'], 'account_count': len(book['account_ids']), 'account_ids': book['account_ids']},
        **roll,
        'note': "book = User.allowed_account_ids (unset means every account in the tenant, matching "
                "app_api.auth.allows_account's own semantics; an explicit empty list means none, fail-closed). "
                "avg_health_score_revenue_weighted and by_band use each account's LATEST HealthScore row only; "
                "accounts_unscored have none yet. risk_opportunity is roi.priorities.investment_priorities' own "
                "rows filtered to this book (rank, don't sum, across accounts — see that tool's own note); "
                "accounts_without_journey haven't been through Wizard A yet, not scored 0. interventions comes "
                "from playbooks.governance.list_interventions filtered to this book: realized_revenue is measured "
                "(cited outcome nodes) and exposure_revenue_open is derived (account revenue on rows still open) — "
                "two numbers, never summed, matching get_roi's convention.",
    }


def team_capacity(customer_id: int) -> dict:
    from models import User, Account
    from journeys.read import origin_block
    csms = User.query.filter_by(customer_id=int(customer_id), role='csm', active=True).order_by(User.user_id).all()
    tenant_ids = [a.account_id for a in Account.query.filter_by(customer_id=int(customer_id)).all()]
    out = {'customer_id': int(customer_id), **origin_block(customer_id)}
    if not csms:
        out.update({'status': 'no_csm_users', 'csms': [],
                    'coverage': {'tenant_accounts': len(tenant_ids), 'covered_accounts': 0,
                                'uncovered_account_ids': sorted(tenant_ids), 'uncovered_count': len(tenant_ids),
                                'accounts_covered_by_multiple_csms': [], 'any_csm_scoped_to_entire_portfolio': False},
                    'note': "no active User rows with role='csm' exist for this tenant — nothing to report. "
                            "Invite one with role='csm' (POST /app/api/users, or app_api.users.invite) to use this tool."})
        return out
    rows, books, scopes = [], {}, []
    for u in csms:
        b = _book(customer_id, u)
        books[u.user_id] = set(b['account_ids'])
        scopes.append(b['scope'])
        roll = _rollup(customer_id, b['account_ids'])
        rows.append({
            'user_id': u.user_id, 'name': u.user_name, 'email': u.email, 'book_scope': b['scope'],
            'account_count': len(b['account_ids']), 'revenue_managed': roll['revenue_managed'],
            'avg_health_score_revenue_weighted': roll['health']['avg_health_score_revenue_weighted'],
            'by_band': roll['health']['by_band'],
            'open_interventions': roll['interventions']['by_state']['proposed'] + roll['interventions']['by_state']['approved']
                                  + roll['interventions']['by_state']['sent'],
            'pending_approvals': roll['risk_opportunity']['pending_approvals'],
            'stuck_interventions': roll['interventions']['stuck'],
            'addressable_weighted_total': roll['risk_opportunity']['addressable_weighted_total'],
        })
    covered = set().union(*books.values()) if books else set()
    cnt = Counter(aid for ids in books.values() for aid in ids)
    overlap = sorted(aid for aid, n in cnt.items() if n > 1)
    uncovered = sorted(set(tenant_ids) - covered)
    out.update({
        'status': 'ok', 'csms': rows,
        'coverage': {'tenant_accounts': len(tenant_ids), 'covered_accounts': len(covered),
                    'uncovered_account_ids': uncovered, 'uncovered_count': len(uncovered),
                    'accounts_covered_by_multiple_csms': overlap,
                    'any_csm_scoped_to_entire_portfolio': any(s.startswith('entire_portfolio') for s in scopes)},
        'note': "workload proxies (open_interventions, pending_approvals, stuck_interventions) are real counts "
                "from Intervention rows on each CSM's book; there is no capacity/target-accounts config anywhere "
                "in this schema, so no over/under-capacity verdict is computed here — compare the numbers across "
                "CSMs yourself. 'book' = User.allowed_account_ids (unset means every account in the tenant); "
                "inactive CSM users (User.active is False) are excluded entirely, including from coverage.",
    })
    return out


def csm_daily_actions(customer_id: int, csm_user_id: Optional[int] = None, top_n: int = 10) -> dict:
    from journeys.read import origin_block
    from roi.priorities import investment_priorities
    from playbooks.governance import list_interventions

    pr = investment_priorities(int(customer_id))
    out = {'customer_id': int(customer_id), **origin_block(customer_id), 'vertical': pr.get('vertical'), 'status': pr['status']}

    account_filter = None
    book_scope = 'portfolio_wide'
    csm_view = None
    if csm_user_id is not None:
        user = _csm_user(customer_id, csm_user_id)
        b = _book(customer_id, user)
        account_filter = set(b['account_ids'])
        book_scope = b['scope']
        csm_view = {'user_id': user.user_id, 'name': user.user_name, 'email': user.email}
    out['csm'] = csm_view
    out['book_scope'] = book_scope

    if pr['status'] != 'ok':
        out.update({'actions': [], 'actions_total_before_limit': 0, 'stuck_interventions': [],
                    'summary': {'accounts_considered': 0, 'pending_approvals': 0, 'stuck': 0}, 'hint': pr.get('hint')})
        return out

    rows = pr['rows'] if account_filter is None else [r for r in pr['rows'] if r['account_id'] in account_filter]
    floor = pr['list_floor']

    li = list_interventions(int(customer_id))
    stuck_rows = [v for v in li['interventions'] if (v['stuck'] or v['delivery_problem'])
                  and (account_filter is None or v['account_id'] in account_filter)]

    actions = []
    for r in rows:
        if r['pending_approvals']:
            action_type = 'approve_intervention'
        elif not r['open_interventions'] and r['priority_factor'] >= floor:
            action_type = 'evaluate_playbook' if r['lens'] == 'protect' else 'pursue_expansion'
        else:
            continue        # already has a non-pending, non-stuck response in flight, or below the floor with nothing open
        actions.append({
            'account_id': r['account_id'], 'account_name': r['account_name'], 'action_type': action_type,
            'lens': r['lens'], 'secondary_lens': r['secondary_lens'], 'priority_factor': r['priority_factor'],
            'revenue_weighted': r['revenue_weighted'], 'addressable_weighted': r['addressable_weighted'],
            'open_interventions': r['open_interventions'], 'why': r['cites']['quote'], 'cites': r['cites'],
        })
    actions.sort(key=lambda a: -a['addressable_weighted']['value'])

    out.update({
        'actions': actions[:int(top_n)], 'actions_total_before_limit': len(actions),
        'stuck_interventions': [{'intervention_id': v['intervention_id'], 'account_id': v['account_id'], 'account_name': v['account_name'],
                                 'playbook_id': v['playbook_id'], 'state': v['state'], 'stuck_days': v['stuck_days'],
                                 'delivery_problem': v['delivery_problem']} for v in stuck_rows],
        'summary': {'accounts_considered': len(rows), 'pending_approvals': sum(r['pending_approvals'] for r in rows), 'stuck': len(stuck_rows)},
        'note': "ranked by addressable_weighted from get_investment_priorities (reused, not re-derived — see that "
                "tool's own docstring for the factors behind it). action_type is 'approve_intervention' when the "
                "account has a proposed intervention waiting; 'evaluate_playbook' / 'pursue_expansion' (by lens) "
                "when nothing is open yet and the account clears power_of_1.json's list_floor; an account with an "
                "open, non-stuck intervention already in flight gets no new action today — there is nothing new to "
                "decide. stuck_interventions lists sent-but-unreported or delivery-failed rows regardless of the "
                "account's current priority ranking, since a stalled intervention needs attention on its own "
                "timeline, not the account's risk ranking. csm_user_id is optional: omitted, this is the whole "
                "portfolio's action list; given, it's scoped to that CSM's book (User.allowed_account_ids).",
    })
    return out


def csm_ranking(customer_id: int, metric: str = 'avg_health_weighted') -> dict:
    from models import User
    from journeys.read import origin_block
    if metric not in RANKING_METRICS:
        raise ValueError(f'metric must be one of {RANKING_METRICS}')
    csms = User.query.filter_by(customer_id=int(customer_id), role='csm', active=True).order_by(User.user_id).all()
    out = {'customer_id': int(customer_id), **origin_block(customer_id), 'metric': metric}
    if not csms:
        out.update({'status': 'no_csm_users', 'ranked': [],
                    'note': "no active User rows with role='csm' exist for this tenant — nothing to rank."})
        return out
    rows = []
    for u in csms:
        b = _book(customer_id, u)
        roll = _rollup(customer_id, b['account_ids'])
        rows.append({
            'user_id': u.user_id, 'name': u.user_name, 'email': u.email, 'book_scope': b['scope'],
            'account_count': len(b['account_ids']), 'revenue_managed': roll['revenue_managed'],
            'avg_health_score_revenue_weighted': roll['health']['avg_health_score_revenue_weighted'],
            'addressable_risk_total': roll['risk_opportunity']['addressable_weighted_total'],
            'realized_revenue': roll['interventions']['realized_revenue'], 'by_band': roll['health']['by_band'],
        })

    def _key(row):
        v = {'avg_health_weighted': row['avg_health_score_revenue_weighted'], 'revenue_managed': row['revenue_managed']['value'],
             'addressable_risk_total': row['addressable_risk_total']['value'], 'realized_revenue': row['realized_revenue']['value']}[metric]
        return (v is None, -(v or 0))       # None sorts last regardless of direction

    rows.sort(key=_key)
    for i, row in enumerate(rows, start=1):
        row['rank'] = i
    out.update({
        'status': 'ok', 'ranked': rows,
        'note': "sorted descending by the chosen metric's value (rank 1 = highest; None sorts last). Reflects the "
                "CURRENT state of each CSM's assigned book (User.allowed_account_ids, or the entire tenant when "
                "unset) — book size and starting difficulty are NOT normalized for, so this is not a measure of "
                "skill or effort in isolation, only of where each book stands today. For avg_health_weighted and "
                "revenue_managed, higher reads as healthier / bigger; for addressable_risk_total, higher means "
                "MORE at-risk revenue remains in that book — the accounts most needing attention, not a "
                "performance score; for realized_revenue, higher means more measured $ has been protected or "
                "expanded through that CSM's closed interventions so far (None, not zero, when nothing has been "
                "reported yet). avg_health_weighted / addressable_risk_total both exclude accounts with no "
                "journey yet (see each row's own risk_opportunity block via get_csm_scorecard for the detail).",
    })
    return out
