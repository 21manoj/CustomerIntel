"""
Unit tests for utils/provenance.py — pure helpers, no DB.

Run:
    pytest kpi-dashboard/backend/tests/test_provenance.py -v
"""

import pytest

from utils.provenance import (
    OBSERVED,
    INFERRED,
    SYNTHETIC,
    ALL_SOURCES,
    TRUSTWORTHY_SOURCES,
    DEFAULT_EDGE_CONFIDENCE_THRESHOLD,
    normalize,
    is_trustworthy,
    trustworthy_sources,
    is_trustworthy_edge,
)


class TestVocabulary:
    def test_canonical_values(self):
        assert OBSERVED == 'observed'
        assert INFERRED == 'inferred'
        assert SYNTHETIC == 'synthetic'

    def test_all_sources_complete(self):
        assert set(ALL_SOURCES) == {OBSERVED, INFERRED, SYNTHETIC}

    def test_synthetic_excluded_from_trustworthy(self):
        # The whole point of the refactor: synthetic must be excluded by default.
        assert SYNTHETIC not in TRUSTWORTHY_SOURCES
        assert OBSERVED in TRUSTWORTHY_SOURCES
        assert INFERRED in TRUSTWORTHY_SOURCES


class TestNormalize:
    def test_legacy_customer_to_observed(self):
        assert normalize('customer') == OBSERVED

    def test_legacy_system_to_inferred(self):
        # Conservative: 'system' is ambiguous so it maps to 'inferred'.
        # Synthetic must be opted into explicitly by the writer.
        assert normalize('system') == INFERRED

    def test_canonical_values_pass_through(self):
        assert normalize(OBSERVED) == OBSERVED
        assert normalize(INFERRED) == INFERRED
        assert normalize(SYNTHETIC) == SYNTHETIC

    def test_none_stays_none_fail_closed(self):
        # Was `normalize(None) == OBSERVED` — a NULL source trusted by
        # default. That fail-open let count_trustworthy_causal_edges admit
        # every edge forever (it read edge.source, an attribute edges don't
        # have). A missing value must never become the most-trusted value.
        assert normalize(None) is None

    def test_unknown_passes_through(self):
        # So readers can detect drift / new values appearing.
        assert normalize('weird_value') == 'weird_value'


class TestIsTrustworthy:
    def test_observed_is_trustworthy(self):
        assert is_trustworthy('observed') is True

    def test_inferred_is_trustworthy(self):
        assert is_trustworthy('inferred') is True

    def test_synthetic_is_not_trustworthy(self):
        assert is_trustworthy('synthetic') is False

    def test_legacy_customer_is_trustworthy(self):
        # Backward compat during rolling deploy.
        assert is_trustworthy('customer') is True

    def test_legacy_system_is_trustworthy(self):
        # 'system' normalizes to 'inferred' which is trustworthy.
        # Pre-migration rows are admitted; migration upgrades them.
        assert is_trustworthy('system') is True

    def test_none_is_trustworthy(self):
        # None defaults to 'observed' which is trustworthy.
        # Fail-closed (2026-08-24): a missing source is NOT trustworthy.
        assert is_trustworthy(None) is False


class TestTrustworthySources:
    def test_default_excludes_synthetic(self):
        sources = trustworthy_sources()
        assert SYNTHETIC not in sources
        assert OBSERVED in sources
        assert INFERRED in sources

    def test_extra_synthetic_includes_synthetic(self):
        sources = trustworthy_sources(extra=(SYNTHETIC,))
        assert SYNTHETIC in sources
        assert OBSERVED in sources
        assert INFERRED in sources

    def test_default_is_immutable_tuple(self):
        # Caller must not be able to mutate the constant.
        assert isinstance(trustworthy_sources(), tuple)


class _FakeEdge:
    """Stand-in for ContextEdge — only the attrs we read."""
    def __init__(self, confidence=None):
        self.confidence = confidence


class TestIsTrustworthyEdge:
    def test_high_confidence_passes(self):
        assert is_trustworthy_edge(_FakeEdge(confidence=0.85)) is True

    def test_at_threshold_passes(self):
        assert is_trustworthy_edge(
            _FakeEdge(confidence=DEFAULT_EDGE_CONFIDENCE_THRESHOLD)
        ) is True

    def test_below_threshold_fails(self):
        assert is_trustworthy_edge(_FakeEdge(confidence=0.5)) is False

    def test_just_below_threshold_fails(self):
        assert is_trustworthy_edge(
            _FakeEdge(confidence=DEFAULT_EDGE_CONFIDENCE_THRESHOLD - 0.01)
        ) is False

    def test_null_confidence_passes(self):
        # Backward compat: rows pre-dating per-edge confidence.
        assert is_trustworthy_edge(_FakeEdge(confidence=None)) is True

    def test_custom_threshold(self):
        edge = _FakeEdge(confidence=0.75)
        assert is_trustworthy_edge(edge, min_confidence=0.7) is True
        assert is_trustworthy_edge(edge, min_confidence=0.8) is False

    def test_zero_threshold_admits_everything(self):
        assert is_trustworthy_edge(_FakeEdge(confidence=0.01), min_confidence=0.0) is True

    def test_invalid_confidence_passes(self):
        # Robust to bad data — don't crash on malformed rows.
        edge = type('E', (), {'confidence': 'not_a_number'})()
        assert is_trustworthy_edge(edge) is True


class TestCountTrustworthyCausalEdges:
    """Pure-function tests on the dual-gate counting logic.

    The DB query path is exercised in integration tests; here we only
    verify that the gate logic correctly counts each bucket.
    """

    def test_requires_scope(self):
        from utils.provenance import count_trustworthy_causal_edges
        with pytest.raises(ValueError):
            count_trustworthy_causal_edges()

    def test_synthetic_dominates_confidence(self):
        # Mirror the gate logic in pure Python — the function combines
        # provenance + confidence; here we re-derive each bucket label
        # against a synthetic fixture set.
        from utils.provenance import is_trustworthy, is_trustworthy_edge

        edges = [
            type('E', (), {'source': 'observed', 'confidence': 0.9})(),  # trust
            type('E', (), {'source': 'inferred', 'confidence': 0.8})(),  # trust
            type('E', (), {'source': 'inferred', 'confidence': 0.5})(),  # low_conf
            type('E', (), {'source': 'synthetic', 'confidence': 0.95})(),  # synth
            type('E', (), {'source': 'synthetic', 'confidence': 0.5})(),  # synth
            type('E', (), {'source': None, 'confidence': None})(),  # null → UNTRUSTED (fail-closed, 2026-08-24)
        ]
        trustworthy = sum(
            1 for e in edges
            if is_trustworthy(e.source) and is_trustworthy_edge(e)
        )
        dropped_synth = sum(
            1 for e in edges if not is_trustworthy(e.source)
        )
        dropped_low = sum(
            1 for e in edges
            if is_trustworthy(e.source) and not is_trustworthy_edge(e)
        )
        # Fail-closed (2026-08-24): a NULL source is untrusted. The old
        # expectation (null -> trusted, trustworthy == 3) encoded the exact
        # fail-open that let count_trustworthy_causal_edges admit every edge.
        assert trustworthy == 2   # observed/0.9, inferred/0.8
        assert dropped_synth == 3  # 2 synthetic + 1 null
        assert dropped_low == 1
        assert trustworthy + dropped_synth + dropped_low == len(edges)


class TestNoRawSourceFilterVocabularyMismatch:
    """Item 23 phase-2 sweep (2026-08-25): apply_node_source_filter was deleted
    — dead (test-only callers) and wrong if ever adopted, since its raw
    `.in_((OBSERVED, INFERRED))` filter, unlike is_trustworthy()/normalize(),
    silently excluded the legacy 'customer' DB default. Any future SQL-level
    node-source filter must normalize() or include 'customer' explicitly.
    This pins that trap so it can't be reintroduced unnoticed."""

    def test_customer_default_is_trustworthy_but_not_in_raw_allowlist(self):
        # The trap: is_trustworthy() (normalize()-based) says yes...
        assert is_trustworthy('customer') is True
        # ...but a raw `.in_(trustworthy_sources())` SQL filter would say no.
        assert 'customer' not in trustworthy_sources()
