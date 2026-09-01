"""
No-silent-substitution guard — Track B item (f), vertical-registry-architecture.md.

"Any code path that returns a config whose slug differs from the one
requested raises." This is the general form of the original Aug 21 2026
audit's Finding 1: get_kpi_catalog served dc2_s's content while reporting
its own vertical as datacenter_v1.

utils.vertical_registry._find_json_catalog_path() already can't load the
wrong FILE — the path is built from the requested vertical's name. What
was missing is a check on the file's CONTENT: nothing stopped a catalog
JSON being copied to a new vertical's filename without its internal
"vertical" field (or its actual pillar/KPI content) being updated to
match. utils.generic_scorer.load_catalog_from_json() now takes an
expected_vertical and raises on a declared-vertical mismatch; every
registered catalog now declares its own vertical (healthcare_provider and
manufacturing_iot didn't before this).

No Flask/DB needed — same convention as test_vertical_catalog_consistency.py.
"""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from utils.generic_scorer import load_catalog_from_json  # noqa: E402
from utils.vertical_registry import SUPPORTED_VERTICALS, _find_json_catalog_path  # noqa: E402


def test_every_registered_json_catalog_declares_its_own_vertical():
    """The guard is only as good as the metadata it checks -- every
    catalog with a JSON file must declare 'vertical', and it must match
    the vertical it's registered under."""
    missing, mismatched = [], []
    for vertical in sorted(SUPPORTED_VERTICALS):
        path = _find_json_catalog_path(vertical)
        if not path:
            continue  # legacy Python-module-only vertical, out of scope here
        with open(path) as f:
            data = json.load(f)
        declared = data.get('vertical')
        if declared is None:
            missing.append(vertical)
        elif declared != vertical:
            mismatched.append((vertical, declared))

    assert not mismatched, f"catalog declares the wrong vertical: {mismatched}"
    assert not missing, (
        f"catalogs with no 'vertical' field, so the no-silent-substitution "
        f"guard can't check them: {missing}"
    )


def test_mismatched_vertical_field_raises():
    """Round-trip proof: a catalog that declares a vertical other than the
    one it's being loaded for must raise, not silently load."""
    real_path = _find_json_catalog_path('datacenter_v1')
    assert real_path is not None, "test assumes datacenter_v1 has a JSON catalog"

    with open(real_path) as f:
        data = json.load(f)
    assert data.get('vertical') == 'datacenter_v1'

    with pytest.raises(ValueError, match="declares vertical='datacenter_v1'"):
        load_catalog_from_json(real_path, expected_vertical='saas_premium')


def test_matching_vertical_field_loads_normally():
    real_path = _find_json_catalog_path('datacenter_v1')
    kpis, pillars = load_catalog_from_json(real_path, expected_vertical='datacenter_v1')
    assert kpis and pillars


def test_no_expected_vertical_is_a_noop_check():
    """Callers that don't know/care what vertical they expect (none left in
    this codebase after this fix, but the parameter is optional) must not
    be broken by the new check."""
    real_path = _find_json_catalog_path('datacenter_v1')
    kpis, pillars = load_catalog_from_json(real_path)
    assert kpis and pillars


def test_missing_vertical_field_is_not_flagged_as_mismatch(tmp_path):
    """A catalog with no 'vertical' field at all must not raise -- this is
    a mismatch guard, not a new required-field validator. (The completeness
    guard for that is test_every_registered_json_catalog_declares_its_own_vertical
    above, which is a separate, softer check.)"""
    p = tmp_path / "no_vertical_field.json"
    p.write_text(json.dumps({
        "pillars": {"P1": {"name": "X", "weight_l2": 1.0}},
        "kpis": {"P1-KPI1": {"name": "Y", "pillar": "P1", "weight_l1": 1.0}},
    }))
    kpis, pillars = load_catalog_from_json(str(p), expected_vertical='anything_at_all')
    assert kpis and pillars


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
