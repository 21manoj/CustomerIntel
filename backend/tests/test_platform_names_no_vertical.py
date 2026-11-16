"""
The platform must not know any vertical by name.

A vertical (SaaS, datacenter, bank vendor risk, ...) is an application on top of the platform and
is defined by files under config/ alone. Someone deploying a new vertical at a customer site adds
config; they must never have to edit platform code. Two guards:

1. No string in platform code names a vertical. Docstrings and comments may (they explain history);
   executable strings may not — `if vertical == '<name>'` is exactly the leak this exists to stop.
2. What a vertical switches on or off is a capability declared in its own catalog, and a vertical
   that exists only as config files gets those capabilities with no code change.
"""
import ast
import json
import os
import re

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Not platform code: tests, demo data builders, one-off scripts, eval harnesses, frozen migrations, config.
NOT_PLATFORM = {'tests', 'demo', 'scripts', 'evals', 'migrations', 'config', '.venv', 'venv', 'node_modules'}


def _vertical_names():
    from utils.vertical_registry import SUPPORTED_VERTICALS, _CATALOG_DIR
    slugs = {f[:-len('_kpi_catalog.json')] for f in os.listdir(_CATALOG_DIR) if f.endswith('_kpi_catalog.json')}
    return sorted(set(SUPPORTED_VERTICALS) | slugs)


def _doc_strings(tree):
    """Bare string statements: docstrings and stand-alone prose blocks. They execute nothing."""
    return {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}


def test_platform_code_names_no_vertical():
    names = _vertical_names()
    assert names, 'no verticals discovered — the guard would pass vacuously'
    pat = re.compile(r'(?<![a-z0-9_])(' + '|'.join(map(re.escape, names)) + r')(?![a-z0-9_])')
    hits = []
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in NOT_PLATFORM and not d.startswith(('__', '.'))]
        for fn in files:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            docs = _doc_strings(tree)
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and pat.search(n.value):
                    hits.append(f'{os.path.relpath(path, BACKEND)}:{n.lineno}: {n.value[:80]!r}')
    assert not hits, ('platform code names a vertical — declare a capability in the vertical\'s catalog '
                      '(utils.vertical_registry.CAPABILITY_DEFAULTS) or a name in config/verticals.json instead:\n  '
                      + '\n  '.join(sorted(hits)))


def test_every_catalog_declares_only_known_capabilities():
    from utils.vertical_registry import SUPPORTED_VERTICALS, CAPABILITY_DEFAULTS, capabilities
    for v in sorted(SUPPORTED_VERTICALS):
        assert set(capabilities(v)) == set(CAPABILITY_DEFAULTS), v


def test_a_vertical_that_says_nothing_gets_the_defaults():
    from utils.vertical_registry import SUPPORTED_VERTICALS, CAPABILITY_DEFAULTS, capabilities, _find_json_catalog_path
    silent = [v for v in sorted(SUPPORTED_VERTICALS)
              if 'capabilities' not in json.load(open(_find_json_catalog_path(v), encoding='utf-8'))]
    assert silent, 'expected at least one catalog with no capabilities block'
    for v in silent:
        assert capabilities(v) == CAPABILITY_DEFAULTS, v


@pytest.fixture
def config_only_vertical(tmp_path, monkeypatch):
    """A vertical that exists only as a catalog file in a scratch config dir — what a field
    deployment adds. Nothing in the platform has heard of it."""
    from utils import vertical_registry as reg

    def make(name, caps):
        body = {'version': '1.0', 'vertical': name, 'pillars': {}, 'kpis': {}}
        if caps is not None:
            body['capabilities'] = caps
        (tmp_path / f'{name}_kpi_catalog.json').write_text(json.dumps(body), encoding='utf-8')
        return name

    monkeypatch.setattr(reg, '_CATALOG_DIR', str(tmp_path))
    monkeypatch.setattr(reg, '_capabilities_cache', {})
    return make


def test_a_config_only_vertical_gets_what_its_catalog_declares(config_only_vertical):
    from utils.vertical_registry import capability
    v = config_only_vertical('supplier_quality_v1', {'phase_transition_edges': True, 'revenue_economics': False})
    assert capability(v, 'phase_transition_edges') is True
    assert capability(v, 'revenue_economics') is False
    assert capability(v, 'kpi_tiers_file') is None          # not declared -> default


def test_an_unknown_capability_is_rejected_not_ignored(config_only_vertical):
    from utils.vertical_registry import capability, capabilities
    v = config_only_vertical('typo_v1', {'phase_transition_edgez': True})
    with pytest.raises(ValueError, match='unknown capabilities'):
        capabilities(v)
    with pytest.raises(KeyError):
        capability('anything', 'not_a_capability')


def test_a_vertical_with_no_catalog_gets_defaults_rather_than_an_error():
    from utils.vertical_registry import capability
    assert capability('no_such_vertical_xyz', 'phase_transition_edges') is False
    assert capability('no_such_vertical_xyz', 'kpi_tiers_file') is None


def test_declared_capabilities_match_what_the_code_used_to_hard_wire():
    """The behaviour before capabilities existed, restated: one vertical chained phase-transition
    edges and had no revenue economics; one offered KPI tiers. Read from config, not from names in code."""
    from utils.vertical_registry import SUPPORTED_VERTICALS, capability, NO_REVENUE_ECONOMICS_VERTICALS
    chained = {v for v in SUPPORTED_VERTICALS if capability(v, 'phase_transition_edges')}
    tiered = {v for v in SUPPORTED_VERTICALS if capability(v, 'kpi_tiers_file')}
    assert chained == {'tprm_v1'}
    assert set(NO_REVENUE_ECONOMICS_VERTICALS) == {'tprm_v1'}
    assert tiered == {'saas_premium'}
