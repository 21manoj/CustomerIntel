"""
Signal engine — schema helpers.

The enrichment columns live on models.QualitativeSignal; this module only
carries the idempotent ALTER used to bring an existing database forward
(server.py calls ensure_enrichment_columns at boot — additive, no Alembic
in this build).
"""
from __future__ import annotations

ENRICHMENT_COLUMNS = {
    'source_type': "VARCHAR(30)",                # manual, email, slack, transcript, ticket, crm_activity, meeting, external
    'raw_text': "TEXT",
    'sentiment_score': "FLOAT",                   # -1.0 to +1.0
    'relationship_sentiment': "FLOAT",
    'product_sentiment': "FLOAT",
    'urgency_score': "FLOAT",                     # 0.0 to 1.0 (LLM perceived)
    'escalation_probability': "FLOAT",            # 0.0 to 1.0
    'intent_signals': "JSONB",                    # list of intent codes (enrichment.VALID_INTENTS)
    'stakeholder_roles': "JSONB",                 # list of {role, name}
    'suggested_action': "VARCHAR(500)",
    'confidence': "JSONB",                        # per-field confidence
    'requires_review': "BOOLEAN DEFAULT FALSE",
    'llm_model_version': "VARCHAR(100)",
    'composite_signal_id': "VARCHAR(100)",
    'dedup_confidence': "FLOAT",
    'cg_node_id': "INTEGER",                      # the OBSERVED node the pipeline wrote
    'alert_suppressed': "BOOLEAN DEFAULT FALSE",
    'structural_urgency': "VARCHAR(20)",          # role floor (urgency.py)
    'effective_urgency': "VARCHAR(20)",           # max(structural, perceived)
    'consent_verified': "BOOLEAN DEFAULT FALSE",  # transcripts
    'content_hash': "VARCHAR(64)",                # exact-duplicate detection
    'occurred_at': "TIMESTAMP",                   # event time (signal_date is a Date)
    'source_ref': "VARCHAR(255)",                 # ticket id / message ts / CRM activity id
    'extractions': "JSONB",                       # every signal the model found in the text
    'use_case': "VARCHAR(120)",                  # the account use case the signal is about
    'attributes': "JSONB",                        # customer extensions
}


def ensure_enrichment_columns(engine) -> None:
    """ADD COLUMN IF NOT EXISTS for every enrichment column — safe to repeat."""
    from sqlalchemy import text
    with engine.connect() as conn:
        for col_name, col_type in ENRICHMENT_COLUMNS.items():
            conn.execute(text(f"ALTER TABLE qualitative_signals ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
        conn.commit()
