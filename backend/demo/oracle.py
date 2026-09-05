"""
Extractor overrides for the demo generator.

The engine picks its extractor itself: the Anthropic model when
ANTHROPIC_API_KEY is set, else its keyword stub. The generator can force
one of three for a run, and the scorecard is labelled with whichever
answered (``model_version``):

  'model'   whatever the engine does (real model with a key, stub without)
  'stub'    the engine's keyword stub, key or not — the honest floor
  'oracle'  the manifest's own labels played back as an extraction. Not a
            model result: it exists so the demo narratives can be seeded
            and the journey/backtest path tested without an API key. A
            scorecard under the oracle is 100% by construction and says so.
  'auto'    (the default, also None) 'model' when ANTHROPIC_API_KEY is set,
            'oracle' when it is not — a keyless seed still tells the §3
            stories; the scorecard names the oracle so nobody reads it as
            model accuracy. Ask for 'stub' to measure the floor.

Nothing here edits signal_engine/: the pipeline imports
``enrichment.enrich_signal`` at call time, so the override swaps that
attribute for the duration of one generator run and restores it after.
"""
from __future__ import annotations

import contextlib
import os
from typing import Callable, Dict, Iterable, Optional

ORACLE_MODEL_VERSION = 'oracle_manifest_labels'
EXTRACTORS = ('auto', 'model', 'stub', 'oracle')


def resolve_extractor(which) -> str:
    """'auto' / None → 'model' with an API key, 'oracle' without."""
    if which is None or which == 'auto':
        return 'model' if os.environ.get('ANTHROPIC_API_KEY') else 'oracle'
    return which


def oracle_extractor(comms: Iterable[dict]) -> Callable:
    """enrich_signal-shaped callable that answers with the labels of the
    communication whose text matches (normalized), else no signals."""
    from signal_engine.pipeline import normalize_text
    import utils.health_thresholds as ht
    labels: Dict[str, dict] = {normalize_text(c['text']): c for c in comms}
    defaults = ht.leading_indicator_config()['default_sentiment_by_polarity']

    def extract(signal_id, raw_text, account_id, customer_id, vertical, taxonomy=None, roster=None):
        from signal_engine.enrichment import normalize_extraction
        from utils.taxonomy_loader import get_taxonomy
        taxonomy = taxonomy or get_taxonomy(vertical)
        c = labels.get(normalize_text(raw_text))
        signals = []
        for sub in (c or {}).get('expected_subtypes', []):
            role = taxonomy.signal_role(sub)
            pol = taxonomy.role_polarity(role)
            sent = c.get('expected_sentiment')
            if sent is None:
                sent = defaults['negative'] if pol < 0 else defaults['positive'] if pol > 0 else defaults['neutral']
            signals.append({'subtype': sub, 'quote': raw_text[:200], 'sentiment_score': float(sent),
                            'urgency_score': 0.5, 'escalation_probability': 0.1, 'confidence': 0.95,
                            'people': [{'name': p['name'], 'title': p.get('role')} for p in c.get('participants', [])]})
        out = normalize_extraction({'signals': signals, 'requires_review': False, 'is_duplicate': False,
                                    'suggested_action': None}, taxonomy)
        out['llm_model_version'] = ORACLE_MODEL_VERSION
        return out
    return extract


@contextlib.contextmanager
def extractor_override(which: Optional[str], comms: Iterable[dict] = ()):
    """Swap the engine's extractor for one run. `which` is one of EXTRACTORS
    (None = 'auto'); a callable is used as-is."""
    import signal_engine.enrichment as enrichment
    which = which if callable(which) else resolve_extractor(which)
    original = enrichment.enrich_signal
    saved_key = os.environ.get('ANTHROPIC_API_KEY')
    try:
        if callable(which):
            enrichment.enrich_signal = which
        elif which == 'oracle':
            enrichment.enrich_signal = oracle_extractor(comms)
        elif which == 'stub':
            os.environ.pop('ANTHROPIC_API_KEY', None)      # the engine's own no-key path
        elif which != 'model':
            raise ValueError(f'extractor must be one of {EXTRACTORS} or a callable, got {which!r}')
        yield
    finally:
        enrichment.enrich_signal = original
        if saved_key is not None:
            os.environ['ANTHROPIC_API_KEY'] = saved_key
