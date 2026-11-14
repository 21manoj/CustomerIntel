"""tprm_v1 as a scored vertical: six pillars, a 33-KPI master list, a 12-KPI core set.

What these pin:
  * the catalog's shape (weights sum to 1 at both levels, two core KPIs per pillar);
  * a target of 0 is a real target (score_kpi used to treat it as "no target" and return the raw value);
  * a vendor measured on the core 12 alone gets a full score -- unmeasured KPIs are left out and the
    weights rescaled, never counted as zero;
  * the committed synthetic demo file only uses core codes and matches its generator.
"""
import csv
import io
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = {'S-KPI1', 'S-KPI2', 'O-KPI1', 'O-KPI2', 'R-KPI1', 'R-KPI2', 'C-KPI1', 'C-KPI2', 'P-KPI1', 'P-KPI4', 'G-KPI2', 'G-KPI3'}


@pytest.fixture(scope='module')
def catalog():
    from utils.vertical_registry import get_kpis, get_pillars
    return get_pillars('tprm_v1'), get_kpis('tprm_v1')


def test_six_pillars_33_kpis_12_core(catalog):
    pillars, kpis = catalog
    assert set(pillars) == {'S', 'O', 'R', 'C', 'P', 'G'}
    assert abs(sum(p['weight_l2'] for p in pillars.values()) - 1) < 1e-9
    assert len(kpis) == 33
    assert {c for c, k in kpis.items() if k.get('core')} == CORE
    for code in pillars:
        mine = [k for k in kpis.values() if k['pillar'] == code]
        assert abs(sum(k['weight_l1'] for k in mine) - 1) < 1e-9, code
        assert sum(1 for k in mine if k.get('core')) == 2, code
        assert pillars[code]['kpi_count'] == len(mine)


def test_tprm_is_scored_but_has_no_revenue_economics():
    from utils.vertical_registry import SIGNALS_ONLY_VERTICALS, NO_REVENUE_ECONOMICS_VERTICALS
    from roi import settings
    assert 'tprm_v1' not in SIGNALS_ONLY_VERTICALS
    assert 'tprm_v1' in NO_REVENUE_ECONOMICS_VERTICALS
    with pytest.raises(settings.EconomicsConfigError):       # fails closed: a vendor has no ARR to price
        settings.economics('tprm_v1')


def test_zero_target_is_a_target(catalog):
    """'0 findings past due' is the target. Before the fix score_kpi saw target 0 as falsy and returned the raw value."""
    from utils.generic_scorer import score_kpi
    _, kpis = catalog
    k = kpis['S-KPI2']                                       # vulnerabilities past due: healthy 0, critical > 3
    assert k['target']['value'] == 0
    assert score_kpi(0, k) == 100
    assert score_kpi(3, k) == pytest.approx(50)
    assert score_kpi(10, k) == 0
    assert score_kpi(0, k) > score_kpi(1, k) > score_kpi(3, k) > score_kpi(6, k)


def test_every_kpi_scores_best_at_its_healthy_end_and_worst_at_its_critical_end(catalog):
    from utils.generic_scorer import score_kpi
    _, kpis = catalog
    for code, k in kpis.items():
        r = k['ranges']
        best, worst = (r['healthy']['max'], r['critical']['min']) if k['higher_is_better'] else (r['healthy']['min'], r['critical']['max'])
        assert score_kpi(best, k) == pytest.approx(100), code
        assert score_kpi(worst, k) == pytest.approx(0), code
        assert score_kpi(k['target']['value'], k) >= 70 - 1e-9, code


def test_core_twelve_alone_gives_a_full_score(catalog):
    """A bank that sends only the core 12 is scored on those; the other 21 are left out, not zero."""
    from utils.generic_scorer import score_account_health_explained
    pillars, kpis = catalog
    at_target = {c: kpis[c]['target']['value'] for c in CORE}
    r = score_account_health_explained(at_target, kpis, pillars)
    assert set(r['pillars']) == set(pillars)
    assert sorted(r['kpi_codes_used']) == sorted(CORE)
    assert r['health'] >= 70                                 # every core KPI exactly on target -> healthy, not dragged down
    best = {c: (kpis[c]['ranges']['healthy']['max'] if kpis[c]['higher_is_better'] else kpis[c]['ranges']['healthy']['min']) for c in CORE}
    assert score_account_health_explained(best, kpis, pillars)['health'] == pytest.approx(100)


def test_a_pillar_with_no_measurement_is_left_out(catalog):
    from utils.generic_scorer import score_account_health_explained
    pillars, kpis = catalog
    r = score_account_health_explained({'S-KPI1': 90, 'S-KPI2': 0}, kpis, pillars)
    assert set(r['pillars']) == {'S'}
    assert r['health'] == pytest.approx(r['pillars']['S'])


def test_synthetic_demo_file_matches_its_generator_and_uses_core_codes_only():
    import importlib.util
    path = os.path.join(BACKEND, 'demo', 'tprm', 'make_core_kpis.py')
    spec = importlib.util.spec_from_file_location('make_core_kpis', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    generated = [{k: str(v) for k, v in row.items()} for row in mod.rows()]
    with open(os.path.join(BACKEND, 'demo', 'tprm', 'kpi_measurements.csv'), encoding='utf-8') as f:
        committed = list(csv.DictReader(io.StringIO(f.read())))
    assert committed == generated
    assert {r['kpi_code'] for r in committed} == CORE
    assert len(committed) == 7 * 12 * 7                      # vendors x core KPIs x months
