"""
Exposure — the money view for a vertical whose accounts cost money when they fail rather than
bring revenue to retain (catalog capability money_basis = 'exposure'). The companion to
roi.power_of_1, which prices a health point in retained revenue and stays unavailable here.

    exposure(customer_id, account_id=None)

Chain, every link labelled:
  tier, flags            derived   the account's own profile (the fields named in the economics file, read by
                                   utils.account_profile.profile_field: a named field or an uploaded column)
  failure cost           derived   the account's own recorded figure when the profile has one, else
                         assumed   the largest cost among the failure events that apply at its tier
                                   (config/economics/<vertical>.json; the largest, not the sum)
  health                 derived   the latest health row
  annual likelihood      assumed   the economics file's likelihood for the band the health falls in
  expected annual exposure assumed failure cost x likelihood
  avoidable exposure     assumed   failure cost x (likelihood now - likelihood one band better)
  exposure avoided       assumed   on closed interventions that carry a health reading before and after:
                                   failure cost x (likelihood before - likelihood after), zero when the
                                   band did not change
  playbook cost          assumed   config/investment/<vertical>.json, estimated cost x times run

Exposure and cost are reported side by side. No ratio of one to the other is computed anywhere:
the exposure figures rest on published averages, and dividing two assumed numbers would read as a
return that nothing here supports. An account with no tier or no health score is listed as not
priced, with the reason; it is never given a default.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from roi import settings
from roi.basis import assumed_link, money
from utils.account_profile import profile_field

NOT_PRICED_NO_TIER = 'no_tier'
NOT_PRICED_BAD_TIER = 'unknown_tier'
NOT_PRICED_NO_HEALTH = 'no_health_score'


def likelihood_for(health: float, econ: dict) -> dict:
    """The likelihood band a health score falls in: {'value', 'min_health', 'index'} (index 0 = best band)."""
    bands = econ['annual_failure_likelihood_by_health']['bands']
    for i, b in enumerate(bands):
        if float(health) >= float(b['min_health']):
            return {'value': float(b['value']), 'min_health': float(b['min_health']), 'index': i}
    last = bands[-1]                                     # below 0 cannot happen on a 0-100 score; stay total anyway
    return {'value': float(last['value']), 'min_health': float(last['min_health']), 'index': len(bands) - 1}


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ('true', 'yes', 'y', '1')


def failure_cost(account, econ: dict) -> dict:
    """{'money': money(), 'tier', 'source': 'recorded'|'event'|None, 'event', 'not_priced'} for one account."""
    fields = econ['account_fields']
    tier_raw = profile_field(account, fields['tier'])
    tier = str(tier_raw).strip().lower() if tier_raw not in (None, '') else None
    recorded = profile_field(account, fields['recorded_failure_cost'])
    if recorded not in (None, ''):
        try:
            value = float(recorded)
        except (TypeError, ValueError):
            value = None
        if value is not None and value >= 0:
            return {'tier': tier, 'source': 'recorded', 'event': None, 'not_priced': None,
                    'money': money(value, 'derived', [f"derived: the account's own recorded {fields['recorded_failure_cost']}"])}
    if tier is None:
        return {'tier': None, 'source': None, 'event': None, 'not_priced': NOT_PRICED_NO_TIER,
                'money': money(None, 'assumed', note=f"no {fields['tier']} on the account profile, so no failure cost can be chosen")}
    if tier not in econ['tiers']:
        return {'tier': tier, 'source': None, 'event': None, 'not_priced': NOT_PRICED_BAD_TIER,
                'money': money(None, 'assumed', note=f"tier {tier!r} is not one of {econ['tiers']}")}
    best = None
    for eid, ev in econ['failure_events'].items():
        if ev.get('applies_when') and not _truthy(profile_field(account, ev['applies_when'])):
            continue
        cost = float(ev['cost_by_tier'][tier])
        if best is None or cost > best[1]:
            best = (eid, cost, ev)
    if best is None:
        return {'tier': tier, 'source': None, 'event': None, 'not_priced': 'no_failure_event_applies',
                'money': money(None, 'assumed', note='no failure event in the economics file applies to this account')}
    eid, cost, ev = best
    return {'tier': tier, 'source': 'event', 'event': {'id': eid, 'label': ev['label']}, 'not_priced': None,
            'money': money(cost, 'assumed', [f'derived: tier {tier} from the account profile', assumed_link(ev['basis'])],
                           note=f"{ev['label']} at the {tier} tier")}


def account_exposure(account, econ: dict) -> dict:
    from models import HealthScore
    from mcp_server.common import get_account_arr
    fc = failure_cost(account, econ)
    hs = HealthScore.query.filter_by(account_id=account.account_id).order_by(HealthScore.measurement_month.desc()).first()
    health = float(hs.health_score) if hs is not None and hs.health_score is not None else None
    lik_cfg = econ['annual_failure_likelihood_by_health']
    lik_link = assumed_link(lik_cfg['basis'])
    regulated = _truthy(profile_field(account, econ['account_fields']['regulated_flag']))
    row = {
        'account_id': account.account_id, 'account_name': account.account_name,
        'tier': fc['tier'], 'regulated_flag': regulated,
        'contract_value': money(get_account_arr(account), 'derived', ['derived: Account.revenue (get_account_arr)']),
        'failure_cost': fc['money'], 'failure_cost_source': fc['source'], 'failure_event': fc['event'],
        'health_now': health, 'measurement_month': hs.measurement_month.isoformat() if hs is not None else None,
        'annual_failure_likelihood': None, 'expected_annual_exposure': None, 'next_band': None, 'not_priced': fc['not_priced'],
    }
    if regulated:
        row['regulated_note'] = econ['regulated_flag_note']['text']
    if fc['not_priced']:
        row['expected_annual_exposure'] = money(None, 'assumed', note=fc['money'].get('note'))
        return row
    if health is None:
        row['not_priced'] = NOT_PRICED_NO_HEALTH
        row['expected_annual_exposure'] = money(None, 'assumed', note='no health score yet, so no likelihood band')
        return row
    lik = likelihood_for(health, econ)
    cost = fc['money']['value']
    chain = list(fc['money']['basis_chain']) + [f'derived: health {health} on the latest health row', lik_link]
    row['annual_failure_likelihood'] = lik['value']
    row['expected_annual_exposure'] = money(cost * lik['value'], 'assumed', chain)
    if lik['index'] > 0:
        better = econ['annual_failure_likelihood_by_health']['bands'][lik['index'] - 1]
        row['next_band'] = {
            'min_health': float(better['min_health']), 'annual_failure_likelihood': float(better['value']),
            'health_points_needed': round(float(better['min_health']) - health, 2),
            'avoidable_exposure': money(cost * (lik['value'] - float(better['value'])), 'assumed', chain,
                                        note='expected annual exposure removed if health reaches the next likelihood band'),
        }
    return row


def _avoided(views: List[dict], hooks: Dict[int, dict], rows_by_account: Dict[int, dict], econ: dict) -> dict:
    """Exposure avoided on closed interventions with a health reading before and after, per playbook."""
    per: Dict[str, dict] = {}
    for v in views:
        s = per.setdefault(v['playbook_id'], {'playbook_id': v['playbook_id'], 'interventions': 0, 'runs': 0, 'closed_done': 0,
                                              'with_health_before_and_after': 0, 'avoided': 0.0, 'intervention_ids': []})
        s['interventions'] += 1
        s['runs'] += 1 if v.get('approved_at') else 0      # a proposal nobody approved took no analyst time
        if not (v['state'] == 'closed' and v['closed_state'] == 'done'):
            continue
        s['closed_done'] += 1
        h = hooks.get(v.get('node_id')) if v.get('node_id') else None
        before = (h.get('health_before') or {}).get('last') if h else None
        after = (h.get('health_after') or {}).get('last') if h else None
        acct = rows_by_account.get(v['account_id'])
        cost = acct['failure_cost']['value'] if acct else None
        if before is None or after is None or cost is None:
            continue
        s['with_health_before_and_after'] += 1
        s['avoided'] += cost * (likelihood_for(before, econ)['value'] - likelihood_for(after, econ)['value'])
        s['intervention_ids'].append(v['intervention_id'])
    return per


def exposure(customer_id: int, account_id: Optional[int] = None) -> dict:
    """Expected annual exposure per account and across the book, what reaching the next likelihood band
    would remove, the exposure avoided on closed interventions, and what the playbooks cost to run. Exposure
    and cost are separate blocks; no ratio between them is returned."""
    from models import Account
    from utils.vertical_registry import get_vertical_for_customer
    from journeys.read import origin_block
    from playbooks.governance import list_interventions
    from roi.measured import intervention_hooks
    vertical = get_vertical_for_customer(customer_id)          # raises: no fallback vertical
    econ = settings.exposure_economics(vertical)               # raises: not an exposure vertical / no file
    q = Account.query.filter_by(customer_id=int(customer_id))
    if account_id is not None:
        q = q.filter_by(account_id=int(account_id))
    accounts = q.order_by(Account.account_id).all()
    lik_cfg = econ['annual_failure_likelihood_by_health']
    out = {
        'customer_id': int(customer_id), 'vertical': vertical, 'account_id': int(account_id) if account_id is not None else None,
        **origin_block(customer_id), 'money_basis': 'exposure',
        'economics': {'file': settings.economics_path(vertical).name, 'basis': econ['basis'], 'horizon_months': econ['horizon_months'],
                      'tiers': econ['tiers'],
                      'failure_events': {eid: {'label': ev['label'], 'applies_when': ev.get('applies_when'), 'cost_by_tier': ev['cost_by_tier'],
                                               'basis': ev['basis'], 'sources': ev['sources']} for eid, ev in econ['failure_events'].items()},
                      'annual_failure_likelihood_by_health': lik_cfg,
                      'regulated_flag_note': econ['regulated_flag_note']},
        'note': 'Every exposure figure is assumed: published averages and stated scale-downs from the economics file, applied to '
                'this tenant\'s own tiers and health scores. A vendor\'s own recorded failure cost replaces the published figure. '
                'Exposure and playbook cost are reported side by side and are not divided into a return. Nothing here is a forecast.',
    }
    if not accounts:
        out.update({'status': 'no_accounts', 'accounts': [], 'portfolio': None, 'playbooks': None})
        return out
    rows = [account_exposure(a, econ) for a in accounts]
    priced = [r for r in rows if not r['not_priced']]
    lik_link = assumed_link(lik_cfg['basis'])
    agg_chain = ['derived: sum over priced accounts', 'assumed: failure costs from the economics file (or the account\'s own recorded figure)', lik_link]

    by_tier = []
    for t in econ['tiers']:
        mine = [r for r in rows if r['tier'] == t]
        p = [r for r in mine if not r['not_priced']]
        by_tier.append({
            'tier': t, 'accounts': len(mine), 'priced_accounts': len(p), 'regulated_flag_accounts': sum(1 for r in mine if r['regulated_flag']),
            'contract_value': money(sum(r['contract_value']['value'] for r in mine), 'derived', ['derived: sum of Account.revenue in tier']),
            'expected_annual_exposure': money(sum(r['expected_annual_exposure']['value'] for r in p), 'assumed', agg_chain) if p
            else money(None, 'assumed', note='no priced account in this tier'),
        })
    not_priced: Dict[str, List[int]] = {}
    for r in rows:
        if r['not_priced']:
            not_priced.setdefault(r['not_priced'], []).append(r['account_id'])

    views = list_interventions(int(customer_id))['interventions']
    if account_id is not None:
        views = [v for v in views if v['account_id'] == int(account_id)]
    hooks = intervention_hooks(customer_id)
    avoided = _avoided(views, hooks, {r['account_id']: r for r in rows}, econ)

    playbooks = None
    try:
        inv = settings.investment(vertical)
    except settings.InvestmentConfigError as e:
        playbooks = {'status': 'no_investment_file', 'reason': str(e), 'rows': []}
    else:
        hourly_link = assumed_link(inv['csm_hourly_cost']['basis'])
        prow = []
        for pid, pb in sorted(inv['playbooks'].items()):
            a = avoided.get(pid, {'interventions': 0, 'runs': 0, 'closed_done': 0, 'with_health_before_and_after': 0, 'avoided': 0.0, 'intervention_ids': []})
            unit = float(pb['estimated_cost_per_execution']['value'])
            hours_link = assumed_link(pb['estimated_csm_hours']['basis'])
            prow.append({
                'playbook_id': pid, 'interventions': a['interventions'], 'runs': a['runs'], 'closed_done': a['closed_done'],
                'estimated_hours': pb['estimated_csm_hours']['value'],
                'estimated_cost_per_execution': money(unit, 'assumed', [hourly_link, hours_link]),
                'estimated_cost_to_date': money(unit * a['runs'], 'assumed', [hourly_link, hours_link, f"derived: x {a['runs']} interventions approved (of {a['interventions']} proposed)"]),
                'exposure_avoided': money(a['avoided'], 'assumed', agg_chain + [f"derived: health before and after on interventions {a['intervention_ids']}"])
                if a['with_health_before_and_after'] else
                money(None, 'assumed', note='no closed intervention of this playbook has a health reading before and after yet'),
                'closed_with_health_before_and_after': a['with_health_before_and_after'],
            })
        extra = sorted(set(avoided) - set(inv['playbooks']))
        playbooks = {
            'status': 'ok', 'hourly_cost': inv['csm_hourly_cost'], 'rows': prow, 'playbooks_without_a_cost_entry': extra,
            'estimated_cost_to_date': money(sum(r['estimated_cost_to_date']['value'] for r in prow), 'assumed',
                                            [hourly_link, 'assumed: estimated hours per playbook', 'derived: x interventions approved']),
            'note': 'Cost is what the approved playbook runs are estimated to take in analyst time; a proposal nobody approved costs nothing. It sits beside exposure avoided; the two are not divided.',
        }

    out.update({
        'status': 'ok',
        'portfolio': {
            'accounts': len(rows), 'priced_accounts': len(priced), 'not_priced': not_priced,
            'regulated_flag_accounts': sum(1 for r in rows if r['regulated_flag']),
            'contract_value': money(sum(r['contract_value']['value'] for r in rows), 'derived', ['derived: sum of Account.revenue (get_account_arr)']),
            'expected_annual_exposure': money(sum(r['expected_annual_exposure']['value'] for r in priced), 'assumed', agg_chain) if priced
            else money(None, 'assumed', note='no account could be priced'),
            'avoidable_exposure_next_band': money(sum(r['next_band']['avoidable_exposure']['value'] for r in priced if r['next_band']), 'assumed', agg_chain,
                                                  note='if every account below the best band reached the next one') if priced
            else money(None, 'assumed', note='no account could be priced'),
            'by_tier': by_tier,
        },
        'playbooks': playbooks,
        'accounts': rows,
    })
    return out
