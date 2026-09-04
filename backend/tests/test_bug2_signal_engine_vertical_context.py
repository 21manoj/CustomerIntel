"""
Regression (Aug 21 2026 vertical-coupling audit, Bug 2): enrichment used to
build its LLM prompt from a hand-written VERTICAL_CONTEXT dict with two
entries and fell back to dc2_s's rack/thermal framing for every other
vertical. v2 removed the dict: build_vertical_context derives the prompt
context from the vertical's own KPI catalog, for every vertical, and a
vertical it cannot read gets a neutral stub — never another vertical's.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_every_registered_vertical_gets_its_own_catalog_context():
    from signal_engine.enrichment import build_vertical_context
    from utils.vertical_registry import SUPPORTED_VERTICALS, get_pillars
    seen = {}
    for vertical in sorted(SUPPORTED_VERTICALS):
        ctx = build_vertical_context(vertical)
        for pid in get_pillars(vertical):
            assert pid in ctx['pillars'], (vertical, pid)
        seen[vertical] = ctx['pillars']
    assert len(set(seen.values())) == len(seen), 'two verticals share prompt context'


def test_datacenter_v1_is_not_dc2_s_framing():
    from signal_engine.enrichment import build_vertical_context
    dc = build_vertical_context('datacenter_v1')
    assert 'Fleet Utilization' in dc['pillars'] and 'Deployment Velocity' not in dc['pillars']


def test_unregistered_vertical_is_neutral_not_dc2_s():
    from signal_engine.enrichment import build_vertical_context
    ctx = build_vertical_context('totally_unregistered_vertical_xyz')
    assert 'racks' not in ctx['key_terms'].lower() and 'Deployment Velocity' not in ctx['pillars']


def test_no_hand_written_vertical_context_survives():
    import signal_engine.enrichment as e
    assert not hasattr(e, 'VERTICAL_CONTEXT')
