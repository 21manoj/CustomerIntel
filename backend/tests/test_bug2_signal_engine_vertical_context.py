"""
Regression: signal_engine enrichment silently used dc2_s's vertical context
for every unrecognized vertical.

Aug 21 2026 vertical-coupling audit, Bug 2. `signal_engine/enrichment.py`
built its LLM enrichment prompt from a `VERTICAL_CONTEXT` dict that only
had 'dc2_s' and 'saas_premium' entries, with the lookup falling back to
`VERTICAL_CONTEXT['dc2_s']` for anything else:

    v_ctx = VERTICAL_CONTEXT.get(vertical, VERTICAL_CONTEXT['dc2_s'])

So a datacenter_v1 (GPU-rental neocloud) or healthcare_provider tenant's
qualitative signals would be enriched using dc2_s's rack/thermal/colocation
framing — wrong industry terminology fed straight into the LLM prompt.

Currently inert in production (FEATURE_SIGNAL_ENGINE=false on live EC2), but
real and worth fixing before that flag flips.

No Flask app or DB needed — VERTICAL_CONTEXT is a plain module-level dict
and the fallback builder only reads JSON KPI catalogs via vertical_registry.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_datacenter_v1_has_its_own_context_not_dc2_s():
    from signal_engine.enrichment import VERTICAL_CONTEXT

    assert "datacenter_v1" in VERTICAL_CONTEXT, (
        "datacenter_v1 has no curated VERTICAL_CONTEXT entry."
    )
    dc_ctx = VERTICAL_CONTEXT["datacenter_v1"]
    dc2s_ctx = VERTICAL_CONTEXT["dc2_s"]

    assert dc_ctx != dc2s_ctx, (
        "datacenter_v1's context is identical to dc2_s's — looks like it's "
        "still borrowing dc2_s's framing."
    )
    # datacenter_v1's own pillar names (per config/datacenter_v1_kpi_catalog.json)
    # must appear, and dc2_s's pillar names must not.
    assert "Fleet Utilization" in dc_ctx["pillars"]
    assert "Deployment Velocity" not in dc_ctx["pillars"]


def test_healthcare_provider_has_its_own_context_not_dc2_s():
    from signal_engine.enrichment import VERTICAL_CONTEXT

    assert "healthcare_provider" in VERTICAL_CONTEXT, (
        "healthcare_provider is a registered vertical "
        "(config/healthcare_provider_kpi_catalog.json exists) but has no "
        "curated VERTICAL_CONTEXT entry."
    )
    hc_ctx = VERTICAL_CONTEXT["healthcare_provider"]
    dc2s_ctx = VERTICAL_CONTEXT["dc2_s"]

    assert hc_ctx != dc2s_ctx
    assert "Patient Outcomes" in hc_ctx["pillars"]
    assert "Deployment Velocity" not in hc_ctx["pillars"]


def test_unrecognized_vertical_fallback_is_not_dc2_s():
    """The actual bug: VERTICAL_CONTEXT.get(vertical, VERTICAL_CONTEXT['dc2_s'])
    silently handed dc2_s's context to any vertical without a curated entry.
    Simulate the exact lookup enrich_signal() performs and confirm it no
    longer resolves to dc2_s's context for a vertical with no curated entry."""
    from signal_engine import enrichment
    from signal_engine.enrichment import VERTICAL_CONTEXT, _build_generic_vertical_context

    fake_vertical = "totally_unregistered_vertical_xyz"
    assert fake_vertical not in VERTICAL_CONTEXT

    # This mirrors the exact line in enrich_signal().
    resolved = VERTICAL_CONTEXT.get(fake_vertical) or _build_generic_vertical_context(fake_vertical)

    assert resolved != VERTICAL_CONTEXT["dc2_s"], (
        "An unrecognized vertical must not silently resolve to dc2_s's "
        "context (racks/GPUs/thermal framing)."
    )
    assert "racks" not in resolved["key_terms"].lower()
    assert "gpus" not in resolved["key_terms"].lower() or fake_vertical == "dc2_s"


def test_generic_fallback_derives_real_data_for_registered_vertical():
    """For a vertical that IS registered (has a real JSON catalog) but has
    no curated VERTICAL_CONTEXT entry, the generic fallback must derive its
    pillars from that vertical's actual catalog, not fabricate content."""
    from signal_engine.enrichment import _build_generic_vertical_context
    from utils.vertical_registry import SUPPORTED_VERTICALS, get_pillars

    curated = {"dc2_s", "saas_premium", "datacenter_v1", "healthcare_provider"}
    uncurated_registered = SUPPORTED_VERTICALS - curated
    if not uncurated_registered:
        import pytest
        pytest.skip("No uncurated-but-registered vertical exists right now")

    vertical = sorted(uncurated_registered)[0]
    ctx = _build_generic_vertical_context(vertical)
    real_pillars = get_pillars(vertical)
    for pid, pdef in real_pillars.items():
        assert pid in ctx["pillars"]
