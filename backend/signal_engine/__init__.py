"""
Signal engine v2 — every qualitative signal becomes cited evidence on the
journey, through one pipeline whatever the source.

  pipeline     ingest (dedup, event time, participants) → classify → reconcile
               polarity → resolve people → OBSERVED ContextNode → journey rebuild
  enrichment   LLM extraction for free text (intents, sentiment, urgency, people)
  urgency      role floor ⊕ perceived urgency
  ingest_api   framework-agnostic request handling; http = Starlette routes
  email_receiver / slack_events   webhook adapters (signature + customer toggle)
  worker       background drain of un-materialized signals
  settings     config/signal_engine.json
  models       enrichment-column migration helper

Toggles: FEATURE_SIGNAL_ENGINE (server-wide, default on); per-customer
FeatureToggle 'signal_engine' gates the webhook sources.
"""

__all__ = ['pipeline', 'enrichment', 'urgency', 'ingest_api', 'http', 'email_receiver',
           'slack_events', 'worker', 'settings', 'models']
