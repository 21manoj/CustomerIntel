"""
Realized retention (wizards/wizard_b_hindsight.retention_figures) and its settings.

The four cases the figure used to get wrong, on plain values, plus the tenant override path:
  1. an outcome older than the window does not move the figure
  2. the starting value is what the cohort paid at the window start, not today's revenue
  3. an account that joined inside the window is in neither side
  4. thin evidence withholds the figure and says why
"""
import pytest

from wizards import wizard_b_settings
from wizards.wizard_b_hindsight import retention_figures

AS_OF = '2026-10-31T23:59:59'


def _cfg(**over):
    return wizard_b_settings.retention(None) | over


def _journey(aid, outcomes, first='2025-01-15T00:00:00'):
    eps = [{'kind': 'signal', 'date': first}]
    for date, bucket, amount, *sub in outcomes:
        eps.append({'kind': 'outcome', 'date': date, 'revenue_bucket': bucket, 'revenue': amount,
                    'subtype': sub[0] if sub else bucket})
    return {'account_id': aid, 'as_of': AS_OF, 'state': 'classified', 'arc': {'arc_type': 'steady'}, 'episodes': eps,
            'data_coverage': {'first_evidence_at': first}, 'phases': [],
            'summary': {'ending_health': 70, 'lowest_health': 60}}


def _book(n=6, revenue=100_000.0):
    return {i: revenue for i in range(1, n + 1)}, [_journey(i, []) for i in range(1, n + 1)]


def test_defaults_come_from_config_and_every_editable_key_has_one():
    d = wizard_b_settings.defaults()
    assert set(wizard_b_settings.get('realized_retention', 'limits')) == set(d)
    assert d['window_months'] > 0 and d['starting_revenue'] in ('reconstructed', 'current')


def test_no_movement_is_exactly_one():
    current, js = _book()
    p = retention_figures(current, js, _cfg())['portfolio']
    assert p['nrr'] == 1.0 and p['grr'] == 1.0 and p['starting_arr'] == 600_000.0 and not p['withheld']


def test_outcome_before_the_window_does_not_count():
    current, js = _book()
    js[0] = _journey(1, [('2024-06-01T00:00:00', 'lost', -40_000.0)], first='2024-01-01T00:00:00')
    r = retention_figures(current, js, _cfg())
    assert r['portfolio']['lost'] == 0.0 and r['portfolio']['nrr'] == 1.0 and r['portfolio']['outcomes_outside_window'] == 1
    # a window long enough to reach it counts it
    wide = retention_figures(current, js, _cfg(window_months=36))['portfolio']
    assert wide['lost'] == 40_000.0 and wide['nrr'] < 1.0


def test_starting_revenue_is_the_value_at_the_window_start():
    current, js = _book()
    current[1] = 60_000.0        # was 100K, lost 40K inside the window
    current[2] = 130_000.0       # was 100K, grew 30K inside the window
    js[0] = _journey(1, [('2026-05-01T00:00:00', 'lost', -40_000.0)])
    js[1] = _journey(2, [('2026-06-01T00:00:00', 'expansion', 30_000.0)])
    p = retention_figures(current, js, _cfg())['portfolio']
    assert p['starting_arr'] == 600_000.0
    assert p['nrr'] == round(590_000 / 600_000, 4) and p['grr'] == round(560_000 / 600_000, 4)
    assert p['net_arr'] == sum(current.values())
    # the old behaviour, still selectable: today's revenue as the start
    old = retention_figures(current, js, _cfg(starting_revenue='current'))['portfolio']
    assert old['starting_arr'] == 590_000.0 and old['nrr'] != p['nrr']


def test_loss_is_capped_at_what_the_account_paid():
    current, js = _book()
    js[0] = _journey(1, [('2026-05-01T00:00:00', 'lost', -900_000.0)])
    p = retention_figures(current, js, _cfg(starting_revenue='current'))['portfolio']
    assert p['lost'] == 100_000.0 and p['grr'] == round(500_000 / 600_000, 4) and p['grr'] >= 0


def test_account_that_joined_in_the_window_is_in_neither_side():
    current, js = _book()
    current[7] = 250_000.0
    js.append(_journey(7, [('2026-03-01T00:00:00', 'expansion', 250_000.0, 'new_logo')], first='2026-03-01T00:00:00'))
    p = retention_figures(current, js, _cfg())['portfolio']
    assert p['cohort_accounts'] == 6 and p['new_accounts_excluded'] == 1
    assert p['starting_arr'] == 600_000.0 and p['expansion'] == 0.0 and p['nrr'] == 1.0
    # one that joined before the window is part of the cohort
    js[-1] = _journey(7, [('2024-03-01T00:00:00', 'expansion', 250_000.0, 'new_logo')], first='2024-03-01T00:00:00')
    assert retention_figures(current, js, _cfg())['portfolio']['cohort_accounts'] == 7


def test_narrative_buckets_never_move_the_figure():
    current, js = _book()
    js[0] = _journey(1, [('2026-05-01T00:00:00', 'at_risk', -80_000.0), ('2026-06-01T00:00:00', 'protected', 80_000.0)])
    p = retention_figures(current, js, _cfg())['portfolio']
    assert p['nrr'] == 1.0 and p['protected_narrative'] == 160_000.0


@pytest.mark.parametrize('build, needle', [
    (lambda: _book(n=3), 'in the cohort'),
    (lambda: (_book()[0], [_journey(i, [], first='2026-10-01T00:00:00') for i in range(1, 7)]), 'of history'),
    (lambda: (_book()[0], [_journey(1, [('2026-05-01T00:00:00', 'lost', None)])] + _book()[1][1:]), 'carry no amount'),
    (lambda: _book(revenue=0.0), 'no starting revenue'),
])
def test_thin_evidence_withholds_the_figure_with_the_reason(build, needle):
    current, js = build()
    p = retention_figures(current, js, _cfg())['portfolio']
    assert p['withheld'] is True and p['nrr'] is None and p['grr'] is None and needle in p['withheld_reason']


def test_window_and_basis_are_stated():
    current, js = _book()
    r = retention_figures(current, js, _cfg(window_months=6))
    assert r['window'] == {'start': '2026-05-02', 'end': '2026-10-31', 'months': 6, 'anchor': 'data_as_of'}
    assert 'trailing 6 months' in r['basis'] and 'reconstructed' in r['basis']
    assert r['portfolio']['observed_months'] <= 6.1


def test_overrides_are_validated_against_the_limits():
    assert wizard_b_settings.validate({'window_months': '6', 'min_amount_coverage': 0.5, 'window_anchor': 'today'}) == {
        'window_months': 6, 'min_amount_coverage': 0.5, 'window_anchor': 'today'}
    for bad in ({'window_months': 0}, {'window_months': 2.5}, {'window_anchor': 'tomorrow'}, {'min_amount_coverage': 2},
                {'loss_buckets': ['x']}, {'window_months': True}, {'starting_revenue': 'guess'}):
        with pytest.raises(ValueError):
            wizard_b_settings.validate(bad)
