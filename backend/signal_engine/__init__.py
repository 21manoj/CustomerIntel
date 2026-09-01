"""
QSIM Signal Engine — Qualitative Signal Intelligence Module.

Feature-toggled: Requires FEATURE_SIGNAL_ENGINE=true AND per-customer
CONTEXT_GRAPH feature enabled.

Architecture:
  Stage 1 - Ingestion:     Raw signals from Slack/Email/Transcript
  Stage 2 - Enrichment:    LLM-powered extraction (sentiment, intent, urgency)
  Stage 3 - Deduplication:  Cross-channel merge within configurable windows
  Stage 4 - CG Collision:   Check against existing context graph nodes
  Stage 5 - Fusion:         Composite health modifier (quant + qual)
  Stage 6 - Alert Routing:  Dispatch via webhook engine

Modules:
  models       — Extended QualitativeSignal columns + AlertRecord table
  urgency      — Structural urgency classifier (deterministic, no LLM)
  collision    — Context graph collision check + pre-emption
  fusion       — Composite score fusion with cold-start ramp
  ingest_api   — Flask blueprint for webhook ingestion endpoints
  enrichment   — LLM enrichment service (Phase 1 — stub with manual override)
"""

__all__ = [
    'urgency',
    'collision',
    'fusion',
    'ingest_api',
]
