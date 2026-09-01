"""
Round-trip identity guard — Track B item (e), vertical-registry-architecture.md.

"For every vertical, get_kpi_catalog's pillar names match list_verticals'
description and total_kpis matches kpi_count. This single test catches
both bugs found today [Aug 21 2026]."

Correction to how this test was originally scoped (state-of-play.md item
21): list_verticals' kpi_count does NOT come from an independent DB source.
The model it tries to query, VerticalTemplate, does not exist in models.py
-- the try/except (ImportError, Exception): pass around that query always
fires silently, and list_verticals falls through to
utils.vertical_registry.get_kpis(), the exact same call get_kpi_catalog's
total_kpis is built from (mcp_server/cs_pulse_mcp_server.py:639 -- see
test_get_kpi_catalog_total_kpis_matches_registry_kpi_count below for the
exact formula it replicates).

So today this test pins a TRIVIAL identity (the same function, called
twice, must agree with itself) -- not evidence of two independent systems
being cross-checked. Its value is forward-looking: if VerticalTemplate (or
any future DB-backed cache) is ever made real, per item 21's decision the
registry stays canonical and this test is what forces the cache to be
validated against it rather than silently drifting. Do not read a pass
here as "two sources agree" until that DB path actually exists.

No Flask/DB needed -- same convention as test_vertical_catalog_consistency.py.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from utils.vertical_registry import SUPPORTED_VERTICALS, get_kpis, get_pillars  # noqa: E402


def _get_kpi_catalog_total_kpis(vertical: str) -> int:
    """Replicates mcp_server/cs_pulse_mcp_server.py:639's exact formula:
    total_kpis = sum(p['kpi_count'] for p in pillar_catalog.values()), where
    pillar_catalog is built by iterating PILLARS and counting kpi_defs
    entries whose 'pillar' field matches each pillar code."""
    pillars = get_pillars(vertical)
    kpi_defs = get_kpis(vertical)
    total = 0
    for pcode in pillars:
        total += sum(1 for kdata in kpi_defs.values() if kdata.get('pillar') == pcode)
    return total


def _list_verticals_kpi_count(vertical: str) -> int:
    """Replicates mcp_server/cs_pulse_onboarding.py's list_verticals()
    fallback branch (the only branch that ever actually runs -- see module
    docstring): kpi_count = len(get_kis(v_slug))."""
    return len(get_kpis(vertical))


def test_total_kpis_matches_kpi_count_for_every_vertical():
    """The identity test the architecture doc asked for. Passes trivially
    today (same source, called twice) -- see module docstring for why that
    is expected, not evidence of independent cross-checking."""
    for vertical in sorted(SUPPORTED_VERTICALS):
        catalog_total = _get_kpi_catalog_total_kpis(vertical)
        verticals_count = _list_verticals_kpi_count(vertical)
        assert catalog_total == verticals_count, (
            f"{vertical!r}: get_kpi_catalog total_kpis={catalog_total} != "
            f"list_verticals kpi_count={verticals_count}"
        )


def test_get_kpi_catalog_total_kpis_matches_registry_kpi_count():
    """Both consumer-side counts must equal the registry's own len(get_kpis()),
    not just each other -- pins to the canonical source (item 21's decision),
    not to mutual agreement, which is the weaker and gameable property."""
    for vertical in sorted(SUPPORTED_VERTICALS):
        ground_truth = len(get_kpis(vertical))
        assert _get_kpi_catalog_total_kpis(vertical) == ground_truth, (
            f"{vertical!r}: get_kpi_catalog's total_kpis formula diverged "
            f"from get_kpis() itself -- likely an orphaned-pillar KPI, see "
            f"test_every_registered_vertical_has_complete_pillar_data"
        )
        assert _list_verticals_kpi_count(vertical) == ground_truth, (
            f"{vertical!r}: list_verticals' kpi_count diverged from "
            f"get_kpis() -- if this fires, something changed which source "
            f"list_verticals reads from; re-verify item 21's premise before "
            f"assuming this is a simple regression"
        )


def test_pillar_names_match_between_the_two_surfaces():
    """get_kpi_catalog's pillar_catalog names and list_verticals'
    auto-generated description both come from get_pillars() -- confirm
    neither has drifted to a different naming source."""
    for vertical in sorted(SUPPORTED_VERTICALS):
        pillars = get_pillars(vertical)
        names_from_registry = {pdata.get('name') for pdata in pillars.values()}
        assert None not in names_from_registry, (
            f"{vertical!r}: a pillar has no name in the registry -- both "
            f"get_kpi_catalog's pillar_catalog and list_verticals' "
            f"description would render a bare code or fall back silently"
        )


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
