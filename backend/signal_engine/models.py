#!/usr/bin/env python3
"""
QSIM Signal Engine — Database models.

Extends the existing QualitativeSignal model with enrichment columns
and adds AlertRecord for audit trail.

IMPORTANT: These models are only active when FEATURE_SIGNAL_ENGINE=true.
The migration is idempotent (ADD COLUMN IF NOT EXISTS).
"""

from extensions import db
from datetime import datetime


# ============================================================
# Signal source and intent enums
# ============================================================

VALID_SOURCE_TYPES = {'slack', 'email', 'transcript', 'manual', 'kpi_derived'}

VALID_INTENTS = {
    'renewal_risk', 'expansion_interest', 'champion_change',
    'product_frustration', 'feature_request', 'executive_escalation',
    'pricing_concern', 'competitor_mention', 'deployment_blocker',
    'nps_drop_indicator',
}

VALID_URGENCY_LEVELS = {'critical', 'high', 'medium', 'low'}

# Tier 1 subtypes that bypass composite fusion and fire immediate alerts
TIER_1_SUBTYPES = {'champion_loss', 'executive_escalation', 'churn_risk'}


# ============================================================
# AlertRecord — audit trail for every notification fired
# ============================================================

class AlertRecord(db.Model):
    """Tracks every alert dispatched by the signal engine.

    Provides full audit trail: what signal triggered it, who received it,
    what channel it went through, and delivery status.
    """
    __tablename__ = 'alert_records'

    id = db.Column(db.Integer, primary_key=True)
    signal_ref = db.Column(db.String(100), nullable=False, index=True)  # composite_signal_id or cg_node_id
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.account_id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)

    alert_type = db.Column(db.String(30), nullable=False)  # tier_1_preemption, tier_2_composite
    channel = db.Column(db.String(30), nullable=False)  # slack, email, crm_task
    recipient = db.Column(db.String(255), nullable=True)
    urgency = db.Column(db.String(20), nullable=False)  # critical, high, medium, low

    delivery_status = db.Column(db.String(20), default='pending')  # pending, delivered, failed, acknowledged
    suppression_reason = db.Column(db.String(255), nullable=True)  # e.g. "CG collision with node 1234"

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    delivered_at = db.Column(db.DateTime, nullable=True)
    acknowledged_at = db.Column(db.DateTime, nullable=True)

    # JSON details for debugging
    details = db.Column(db.JSON, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'signal_ref': self.signal_ref,
            'account_id': self.account_id,
            'customer_id': self.customer_id,
            'alert_type': self.alert_type,
            'channel': self.channel,
            'recipient': self.recipient,
            'urgency': self.urgency,
            'delivery_status': self.delivery_status,
            'suppression_reason': self.suppression_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'delivered_at': self.delivered_at.isoformat() if self.delivered_at else None,
        }


# ============================================================
# QualitativeSignal enrichment columns (migration helper)
# ============================================================

# These columns are added to the existing QualitativeSignal table
# via idempotent ALTER TABLE IF NOT EXISTS.

ENRICHMENT_COLUMNS = {
    'source_type': "VARCHAR(30)",                # slack, email, transcript, manual, kpi_derived
    'raw_text': "TEXT",                           # Original text (encrypted at rest)
    'sentiment_score': "FLOAT",                   # -1.0 to +1.0
    'relationship_sentiment': "FLOAT",            # Vendor relationship sentiment
    'product_sentiment': "FLOAT",                 # Product sentiment
    'urgency_score': "FLOAT",                     # 0.0 to 1.0
    'escalation_probability': "FLOAT",            # 0.0 to 1.0
    'intent_signals': "JSONB",                    # List of intent codes
    'stakeholder_roles': "JSONB",                 # List of {role, name}
    'suggested_action': "VARCHAR(500)",           # LLM-recommended next action
    'confidence': "JSONB",                        # Per-field confidence scores
    'requires_review': "BOOLEAN DEFAULT FALSE",   # True if any confidence < 0.6
    'llm_model_version': "VARCHAR(100)",          # Model ID for audit
    'composite_signal_id': "VARCHAR(100)",        # Dedup parent link
    'dedup_confidence': "FLOAT",                  # Merge confidence
    'cg_node_id': "INTEGER",                      # Linked context graph node
    'alert_suppressed': "BOOLEAN DEFAULT FALSE",  # Suppressed due to CG collision
    'structural_urgency': "VARCHAR(20)",          # critical/high/medium/low
    'effective_urgency': "VARCHAR(20)",            # max(structural, llm)
    'consent_verified': "BOOLEAN DEFAULT FALSE",  # Transcript consent
}


def ensure_enrichment_columns(engine):
    """Add QSIM enrichment columns to qualitative_signals table (idempotent).

    Call this on app startup when FEATURE_SIGNAL_ENGINE=true.
    Uses ADD COLUMN IF NOT EXISTS — safe to run repeatedly.
    """
    from sqlalchemy import text
    with engine.connect() as conn:
        for col_name, col_type in ENRICHMENT_COLUMNS.items():
            try:
                conn.execute(text(
                    f"ALTER TABLE qualitative_signals "
                    f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
            except Exception:
                pass  # Column may already exist
        conn.commit()


# Leading-vs-trailing early-warning columns on health_scores. The qual (leading)
# score and its divergence from the KPI (trailing) score are surfaced as their own
# fields — a leading indicator's value is the GAP, not a blended composite.
DIVERGENCE_COLUMNS = {
    'qual_score': "NUMERIC(5,2)",       # leading — qual signal score (0-100)
    'divergence': "NUMERIC(5,2)",       # leading - trailing (signed)
    'early_warning': "VARCHAR(24)",     # early_warning | recovery_watch | aligned
}


def ensure_divergence_columns(engine):
    """Add leading/divergence/early_warning columns to health_scores (idempotent)."""
    from sqlalchemy import text
    with engine.connect() as conn:
        for col_name, col_type in DIVERGENCE_COLUMNS.items():
            try:
                conn.execute(text(
                    f"ALTER TABLE health_scores "
                    f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                ))
            except Exception:
                pass  # Column may already exist
        conn.commit()


def ensure_alert_records_table(engine):
    """Create alert_records table if it doesn't exist (idempotent)."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS alert_records (
                id SERIAL PRIMARY KEY,
                signal_ref VARCHAR(100) NOT NULL,
                account_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                alert_type VARCHAR(30) NOT NULL,
                channel VARCHAR(30) NOT NULL,
                recipient VARCHAR(255),
                urgency VARCHAR(20) NOT NULL,
                delivery_status VARCHAR(20) DEFAULT 'pending',
                suppression_reason VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW(),
                delivered_at TIMESTAMP,
                acknowledged_at TIMESTAMP,
                details JSONB
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_alert_records_account ON alert_records(account_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_alert_records_customer ON alert_records(customer_id)"
        ))
        conn.commit()
