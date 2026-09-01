"""
Meta-test: outcome-subtype taxonomy must be loaded from JSON, not hardcoded.

Apr 25 2026 — added after the Wizard B `_DEFINITIVE_*` drift incident.
Wizard B had inline `_DEFINITIVE_LOST = {...}` and `_DEFINITIVE_EXPANSION = {...}`
sets that fell behind `taxonomy_base.json` once the JSON was extended with new
subtypes (`revenue_expanded`, `revenue_growth`). Result: silent under-forecast
of NRR by ~7pp on tenants that produced these subtypes.

This test fails the build if anyone reintroduces an inline hardcoded set of
revenue-bucket subtypes anywhere under `backend/`. The acceptable shape is:

    from utils.taxonomy_loader import get_taxonomy
    _tax = get_taxonomy(vertical)
    lost_subtypes      = _tax.revenue_bucket_map['lost']
    expansion_subtypes = _tax.revenue_bucket_map['expansion']

Allowed exceptions (regex-explicit):
- The taxonomy JSON files themselves (they ARE the source of truth).
- `taxonomy_loader.py` (it implements the loader).
- This test file (it lists the bucket names).
- Migration scripts under `migrations/`.
- Production rev_type_map in onboarding_api_v2_config_aware.py — that file
  performs subtype→bucket collapse on ingest; its dict is dual to the JSON
  but operates at a different layer. Wizard B no longer mirrors it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Regex patterns targeting the exact anti-pattern that drifted in Wizard B:
# inline `_DEFINITIVE_*` sets containing canonical subtypes. Subtype→bucket
# *dict* literals (rev_type_map in onboarding_api_v2_config_aware.py and the
# similar pattern in utils/context_graph.py) are a separate anti-pattern
# that's harder to migrate (those operate at ingest layer, not forecast layer)
# and is tracked in a separate backlog item: `taxonomy_dict_migration`.
SUSPICIOUS_PATTERNS = [
    # Inline _DEFINITIVE_* set assignment — the exact shape Wizard B had
    re.compile(
        r"_DEFINITIVE_(LOST|EXPANSION|PIPELINE|AT_RISK|PROTECTED)\s*=\s*[\{\[]"
    ),
    # Module-level frozenset with multiple canonical subtypes
    # (catches `_X = frozenset({'churn_lost', 'contraction'})` style)
    re.compile(
        r"=\s*frozenset\(\s*[\{\[][^)]*['\"]churn_lost['\"][^)]*['\"]contraction['\"]"
    ),
    re.compile(
        r"=\s*frozenset\(\s*[\{\[][^)]*['\"]expansion_closed['\"][^)]*['\"]new_logo['\"]"
    ),
]

# Files explicitly allowed to contain canonical subtype literals
ALLOW_LIST_NAMES = {
    "test_no_hardcoded_outcome_subtypes.py",  # this file
    "taxonomy_loader.py",                     # the loader
    "taxonomy_base.json",                     # the JSON (won't be scanned anyway)
    "context_graph_invariants.py",            # may legitimately reference for invariant checks
}

# Path-prefix exclusions
EXCLUDE_PATH_PREFIXES = (
    "config/",                  # JSON taxonomy files
    "migrations/",              # one-shot scripts
    "tests/",                   # test fixtures may stub the canonical sets
    "verticals/_template/",     # template files containing reference fixtures
    "load-driver/",             # external tool with its own copy of the JSON
)


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, matched_pattern_str) violations in file."""
    if path.name in ALLOW_LIST_NAMES:
        return []
    rel = path.relative_to(BACKEND_ROOT).as_posix()
    if any(rel.startswith(p) for p in EXCLUDE_PATH_PREFIXES):
        return []
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []
    violations: list[tuple[int, str]] = []
    for pat in SUSPICIOUS_PATTERNS:
        for m in pat.finditer(text):
            lineno = text[:m.start()].count("\n") + 1
            violations.append((lineno, m.group(0)[:120]))
    return violations


@pytest.mark.parametrize("py_file", sorted(BACKEND_ROOT.rglob("*.py")))
def test_no_hardcoded_revenue_bucket_set(py_file):
    """Each Python file under backend/ must not redefine revenue-bucket subtypes."""
    violations = _scan_file(py_file)
    assert not violations, (
        f"{py_file.relative_to(BACKEND_ROOT)} contains hardcoded outcome-subtype "
        f"set(s). Load from utils.taxonomy_loader.get_taxonomy() instead.\n"
        + "\n".join(f"  line {ln}: {snip}" for ln, snip in violations)
    )


def test_taxonomy_loader_exposes_required_buckets():
    """Sanity check: the JSON taxonomy is loadable and has the buckets Wizard B
    expects (lost, expansion, pipeline, at_risk, protected)."""
    import sys
    sys.path.insert(0, str(BACKEND_ROOT))
    from utils.taxonomy_loader import get_taxonomy

    tax = get_taxonomy()
    required = {"lost", "expansion", "pipeline", "at_risk", "protected"}
    actual = set(tax.revenue_bucket_map.keys())
    missing = required - actual
    assert not missing, f"taxonomy_base.json missing buckets: {missing}"

    # Each bucket must have at least one subtype
    for bucket in required:
        assert tax.revenue_bucket_map[bucket], f"bucket {bucket!r} is empty in JSON"


def test_definitive_subtypes_match_apr25_baseline():
    """Regression guard: the canonical subtypes Wizard B relies on for NRR must
    remain in the JSON `lost` and `expansion` buckets. If a future PR removes
    one of these from the JSON, this test fails to alert the author that
    Wizard B's NRR semantics are about to change."""
    import sys
    sys.path.insert(0, str(BACKEND_ROOT))
    from utils.taxonomy_loader import get_taxonomy

    tax = get_taxonomy()
    must_be_lost = {"churn_lost", "contraction"}
    must_be_expansion = {"expansion_closed", "new_logo"}
    must_be_pipeline = {"expansion_approved", "expansion_opportunity"}

    actual_lost = set(tax.revenue_bucket_map["lost"])
    actual_exp = set(tax.revenue_bucket_map["expansion"])
    actual_pipe = set(tax.revenue_bucket_map["pipeline"])

    assert must_be_lost.issubset(actual_lost), (
        f"`lost` bucket missing canonical subtypes: {must_be_lost - actual_lost}"
    )
    assert must_be_expansion.issubset(actual_exp), (
        f"`expansion` bucket missing canonical subtypes: {must_be_expansion - actual_exp}"
    )
    assert must_be_pipeline.issubset(actual_pipe), (
        f"`pipeline` bucket missing canonical subtypes: {must_be_pipeline - actual_pipe}"
    )

    # Pipeline subtypes must NOT also appear in expansion (would double-count NRR)
    overlap = actual_pipe & actual_exp
    assert not overlap, (
        f"Pipeline subtypes also appear in expansion bucket: {overlap}. "
        f"This would cause double-counting in Wizard B NRR forecast."
    )
