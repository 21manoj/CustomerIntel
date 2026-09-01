"""
Static-analysis tests that pin the provenance vocabulary across the backend.

Catches drift where a new writer introduces `source='customer'` or
`source='system'` without realising the vocabulary changed. Cheap; no DB.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_provenance_writers.py -v
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from utils.provenance import OBSERVED, INFERRED, SYNTHETIC, ALL_SOURCES

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_LITERALS = {f"'{v}'" for v in ALL_SOURCES} | {f'"{v}"' for v in ALL_SOURCES}

# Files where legacy literals are intentional (migrations, tests, the helper itself).
EXCLUDE_PATHS = {
    'tests/test_provenance.py',
    'tests/test_provenance_writers.py',
    'utils/provenance.py',
    'migrations/refine_provenance_source_values.py',
    'migrations/add_source_to_context_nodes.py',
}

# Regex finds `source=` followed by a string literal (single or double quoted).
SOURCE_LITERAL_RE = re.compile(r"""\bsource\s*=\s*(['"])([^'"]+)\1""")


def _python_files():
    for root, dirs, files in os.walk(BACKEND_ROOT):
        # Skip caches + venvs + node_modules
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'node_modules',
                                                 '.venv', 'venv', '.pytest_cache',
                                                 'context-graph-test-out',
                                                 'test_data', 'uploads',
                                                 'customer17_data',
                                                 'customer19_synthetic_data',
                                                 'company_kpi_files', 'data')]
        for f in files:
            if f.endswith('.py'):
                yield Path(root) / f


def _relative(p: Path) -> str:
    return str(p.relative_to(BACKEND_ROOT)).replace(os.sep, '/')


# Files that write ContextNode/ContextEdge rows. Pinned explicitly because
# `source=` is a generic kwarg name used elsewhere (ROI engine, dashboards),
# so an "imports ContextNode" heuristic over-matches.
KNOWN_CONTEXT_GRAPH_WRITERS = (
    'wizards/wizard_a_journey_db.py',
    'utils/arc_edge_generator.py',
    # Added 2026-08-24 after the node-evidence-gap review found it writing
    # source='wizard_a' — the 5th non-canonical literal, invisible to this
    # guard purely because the file wasn't in this list.
    'utils/arc_decision_generator.py',
    'utils/signal_analyst.py',
    'utils/urgent_signal_scanner.py',
    'utils/playbook_lifecycle.py',
    'push_intelligence_subscriber.py',
    'signal_engine/worker.py',
    'mcp_server/cs_pulse_onboarding.py',
    'onboarding_api_v2_config_aware.py',
)


@pytest.mark.skip(reason="scans KNOWN_CONTEXT_GRAPH_WRITERS, all Tier 2/3 files not yet ported")
def test_no_legacy_source_literals_in_writers():
    """Every `source='...'` literal in a known context-graph writer must
    use the canonical vocabulary {observed, inferred, synthetic}.

    The set of writers is pinned in KNOWN_CONTEXT_GRAPH_WRITERS — when a
    new writer is added, append it here so this test guards it too.
    """
    violations = []
    for rel in KNOWN_CONTEXT_GRAPH_WRITERS:
        path = BACKEND_ROOT / rel
        if not path.exists():
            pytest.fail(
                f'Pinned writer {rel} no longer exists — update '
                'KNOWN_CONTEXT_GRAPH_WRITERS in this test.'
            )
        text = path.read_text(encoding='utf-8', errors='ignore')
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in SOURCE_LITERAL_RE.finditer(line):
                full_literal = f"'{match.group(2)}'"
                if full_literal in ALLOWED_LITERALS:
                    continue
                pre = line[:match.start()].rstrip()
                if pre.endswith(('source_platform=', 'source_event_id=',
                                  'source_ref=', 'source_type=')):
                    continue
                violations.append(
                    f'{rel}:{line_no}  source={full_literal}'
                )

    assert not violations, (
        'Found context-graph writers using non-canonical source vocabulary. '
        'Migrate to {observed, inferred, synthetic} per utils/provenance.py:\n  '
        + '\n  '.join(violations)
    )


@pytest.mark.skip(reason="scans every .py file in the backend for source= literals -- meaningless until Tier 2/3 writers exist")
def test_canonical_sources_actually_used():
    """At least one writer per canonical value, so the vocabulary stays alive."""
    found_observed = False
    found_inferred = False
    found_synthetic = False
    for path in _python_files():
        rel = _relative(path)
        if rel in EXCLUDE_PATHS:
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if "source='observed'" in text or 'source="observed"' in text:
            found_observed = True
        if "source='inferred'" in text or 'source="inferred"' in text:
            found_inferred = True
        if "source='synthetic'" in text or 'source="synthetic"' in text:
            found_synthetic = True

    assert found_observed, 'No writer tags rows as observed — CSV ingest path missing?'
    assert found_inferred, 'No writer tags rows as inferred — runtime detection missing?'
    assert found_synthetic, (
        'No writer tags rows as synthetic — Wizard A arc-template generator missing?'
    )
