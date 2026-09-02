"""
Core data models — carried forward from CustomerSuccessAI-DataCenter (retired
2026-09-01), Tier 1 only: the context graph, its dependent identity/config
models, and the two tables confirmed to actually hold live data.

This is NOT a verbatim copy of the old repo's models.py. That file had 43
model classes; only 8 are here, deliberately, because the old repo's own
production database settled which ones are real:
  - PlaybookExecution (v1) had 0 live rows next to PlaybookExecutionV2's 189
    — only V2 exists here.
  - Of six KPI-shaped tables (KPI, DC2SKPI, KPIScore, KPITimeSeries,
    KPIUpload, KPIReferenceRange), only DC2SKPI had any live data (72,201
    rows) — used as the universal per-vertical KPI store despite its old
    name/docstring claiming vertical-specificity that never actually
    happened in practice. Renamed to KPIMeasurement here (was DC2SKPI);
    six equally-misnamed dc2s_* columns on CustomerConfig were renamed the
    same way. See utils/vertical_health.py's module docstring for the
    related fix (a permanently-dead "try a per-vertical Python module"
    resolution branch, removed rather than carried forward).
See docs/lessons/durable-engineering-lessons.md for the full pattern this
generalizes from (silent duplication, one path dead).
"""
from extensions import db
from datetime import datetime


class Customer(db.Model):
    __tablename__ = 'customers'
    customer_id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String, nullable=False)
    email = db.Column(db.String, unique=True)
    phone = db.Column(db.String)
    domain = db.Column(db.String, unique=True, nullable=True)  # Email domain for multi-tenant identification
    uuid = db.Column(db.String(60), nullable=True, unique=True)  # e.g. saas_cust_019c3409-...
    vertical = db.Column(db.String(20), nullable=True)  # saas, dc, msp
    # WS-2 2a: NULL = real customer data. Non-NULL tags the tenant's data as
    # generated rather than customer-asserted (e.g. 'synthetic_eval_profile'
    # for load-driver/eval_profile tenants) — one value per tenant, since a
    # tenant's data source doesn't vary row-by-row the way an individual
    # node/edge's observed/inferred/synthetic provenance does.
    data_origin = db.Column(db.String(30), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())


class User(db.Model):
    """Ported 2026-09-01 (Tier 2A) — create_customer's admin-user step needs it."""
    __tablename__ = 'users'
    user_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'))
    user_name = db.Column(db.String, nullable=False)
    email = db.Column(db.String, nullable=False)
    # 255, not the old repo's 128: that size fit Werkzeug 2.2.3's pinned
    # default (pbkdf2:sha256, ~110 chars) but not this build's unpinned
    # Werkzeug, whose current default (scrypt) produces a ~162-char hash —
    # caught by test_tier2_create_customer.py inserting a real row, not a
    # guess. Widening the column rather than pinning to the older, weaker
    # default hash method just to fit the old size.
    password_hash = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime)
    uuid = db.Column(db.String(60), nullable=True, unique=True)
    customer_uuid = db.Column(db.String(60), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())
    vertical = db.Column(db.String(50))
    role = db.Column(db.String(50))
    allowed_account_ids = db.Column(db.JSON, nullable=True)
    allowed_customer_ids = db.Column(db.JSON, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_contractor = db.Column(db.Boolean, default=False)
    magic_link_token = db.Column(db.String(100), nullable=True, index=True)
    magic_link_expires_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'user_name', name='unique_customer_username'),
        db.UniqueConstraint('email', name='unique_user_email'),
    )


class CustomerConfig(db.Model):
    __tablename__ = 'customer_configs'
    config_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), unique=True)

    kpi_upload_mode = db.Column(db.String, default='corporate')  # 'corporate' or 'account_rollup'
    category_weights = db.Column(db.Text)  # JSON string of category weights
    master_file_name = db.Column(db.String)  # Name of uploaded master file
    openai_api_key_encrypted = db.Column(db.Text, nullable=True)
    openai_api_key_updated_at = db.Column(db.DateTime, nullable=True)

    # Vertical identifier. No default -- a silent fallback here (previously
    # 'saas_premium', echoing the earlier dc2_s fallback the vertical
    # registry was refactored to remove) let a customer's actual requested
    # vertical get silently discarded whenever a caller forgot to set this
    # explicitly. utils.vertical_registry / _resolve_customer_vertical fail
    # closed on a NULL here (raise, not substitute) -- every writer must set
    # this explicitly, matching that same fail-closed contract.
    vertical = db.Column(db.String(50))  # 'saas_premium', 'dc2_s', 'datacenter_v1', ...

    # Renamed from the old repo's dc2s_* prefix (2026-09-01): every one of
    # these columns is read/written generically for every vertical, not
    # just dc2_s -- confirmed by the old repo's own code comments
    # ("works for any vertical") years after the prefix was added. Fixed
    # here, on day one, while it's free -- see docs/lessons on the
    # docstring-drift pattern this generalizes from.
    pillar_weights = db.Column(db.JSON, nullable=True)     # {"P1": 0.15, "P2": 0.20, ...}
    enabled_kpis = db.Column(db.JSON, nullable=True)       # ["P3-KPI1", "CUSTOM-GPU-1", ...]
    kpi_overrides = db.Column(db.JSON, nullable=True)      # {"P3-KPI1": {"target": 90}, ...}
    kpi_weights = db.Column(db.JSON, nullable=True)        # {"P3": {"P3-KPI1": 0.4, ...}, ...}
    kpi_definitions = db.Column(db.JSON, nullable=True)    # Custom KPI definitions
    lifecycle_stage_weights = db.Column(db.JSON, nullable=True)  # Lifecycle-stage weight profiles
    # Schema: {"enabled": bool, "date_field": str, "stages": [{name, min_days, max_days, pillar_weights, kpi_weights}]}

    nomenclature_overrides = db.Column(db.JSON, nullable=True)  # Deep-merged on top of vertical defaults

    config_version = db.Column(db.String(20), default='1.0')
    customized_by = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())


class Account(db.Model):
    __tablename__ = 'accounts'
    account_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)
    account_name = db.Column(db.String, nullable=False, index=True)
    revenue = db.Column(db.Numeric(15, 2), default=0)
    account_status = db.Column(db.String, default='active', index=True)  # active, at_risk, churned
    industry = db.Column(db.String, index=True)
    vertical = db.Column(db.String(50))
    region = db.Column(db.String, index=True)
    external_account_id = db.Column(db.String, index=True)  # External account ID from customer profile
    profile_metadata = db.Column(db.JSON)  # JSON field for customer profile data

    uuid = db.Column(db.String(60), nullable=True, unique=True)  # e.g. saas_acct_019c3409-...
    customer_uuid = db.Column(db.String(60), nullable=True)  # FK to customers.uuid
    created_at = db.Column(db.DateTime, server_default=db.func.now(), index=True)
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    # Arc Intelligence Engine columns
    arc_type       = db.Column(db.String(50),  nullable=True)   # crisis_recovery, champion_loss, etc.
    arc_phase      = db.Column(db.String(20),  nullable=True)   # baseline | intervention
    arc_confidence = db.Column(db.Float,       nullable=True)   # 0.0 - 1.0

    __table_args__ = (
        db.Index('idx_account_customer_status', 'customer_id', 'account_status'),
        db.Index('idx_account_customer_industry', 'customer_id', 'industry'),
        db.Index('idx_account_customer_region', 'customer_id', 'region'),
    )


class FeatureToggle(db.Model):
    __tablename__ = 'feature_toggles'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)
    feature_name = db.Column(db.String(100), nullable=False, index=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    config = db.Column(db.JSON)  # Feature-specific configuration
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'feature_name', name='unique_customer_feature'),
    )


# ============================================================
# CONTEXT GRAPH TABLES — the platform's actual moat
# ============================================================
# Feature flag: 'context_graph' in FeatureToggle table
# Three-tier storage: T1 permanent, T2 decaying (TTL), T3 ephemeral
# Every node/edge carries revenue_impact for CRO/CFO lens.

class ContextNode(db.Model):
    """
    Graph node representing an entity in the account context graph.

    Node types: ACCOUNT, SIGNAL, STAKEHOLDER, DECISION, OUTCOME, EXTERNAL_CONTEXT
    Every node belongs to exactly one account (tenant isolation).
    Revenue impact fields support the CRO/CFO outcome-focused lens.
    """
    __tablename__ = 'context_nodes'

    node_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.account_id'), nullable=False, index=True)

    node_type = db.Column(db.String(30), nullable=False, index=True)
    # SIGNAL, STAKEHOLDER, DECISION, OUTCOME, EXTERNAL_CONTEXT
    node_subtype = db.Column(db.String(50), index=True)
    # e.g. SIGNAL→kpi_change|ticket|nps; DECISION→playbook|escalation|exec_engagement

    # Node origin: 'customer' = CSV upload; 'system' = Wizard A / signal_analyst / urgent_scanner
    source = db.Column(db.String(20), nullable=False, default='customer')

    # Storage tier: 1=permanent, 2=decaying, 3=ephemeral
    tier = db.Column(db.SmallInteger, nullable=False, default=2)

    title = db.Column(db.String(500))
    properties = db.Column(db.JSON, nullable=False, default=dict)
    # Flexible payload — schema varies by node_type

    # Revenue impact (CRO/CFO lens — every node should answer "so what in $?")
    revenue_impact = db.Column(db.Numeric(15, 2))       # ARR at risk or protected
    revenue_impact_type = db.Column(db.String(30))       # at_risk, protected, expansion, lost
    confidence = db.Column(db.Numeric(3, 2), default=1.0) # 0.00-1.00

    source_platform = db.Column(db.String(50))           # sfdc, hubspot, intercom, cs_pulse, csv_import
    source_event_id = db.Column(db.String(200))          # External ID for dedup
    source_ref = db.Column(db.String(200))               # e.g. SFDC Opportunity ID

    occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    expires_at = db.Column(db.DateTime)                   # NULL = never expires (Tier 1)
    weight_decay = db.Column(db.Numeric(3, 2), default=1.0) # Current weight after decay

    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    __table_args__ = (
        db.Index('idx_ctx_node_account_type', 'account_id', 'node_type'),
        db.Index('idx_ctx_node_customer', 'customer_id', 'node_type'),
        db.Index('idx_ctx_node_occurred', 'account_id', 'occurred_at'),
        db.Index('idx_ctx_node_tier_expires', 'tier', 'expires_at'),
        db.Index('idx_ctx_node_source', 'source_platform', 'source_event_id'),
    )

    def to_dict(self):
        return {
            'node_id': self.node_id,
            'customer_id': self.customer_id,
            'account_id': self.account_id,
            'node_type': self.node_type,
            'node_subtype': self.node_subtype,
            'source': self.source,
            'tier': self.tier,
            'title': self.title,
            'properties': self.properties,
            'revenue_impact': float(self.revenue_impact) if self.revenue_impact else None,
            'revenue_impact_type': self.revenue_impact_type,
            'confidence': float(self.confidence) if self.confidence else None,
            'source_platform': self.source_platform,
            'source_event_id': self.source_event_id,
            'occurred_at': self.occurred_at.isoformat() if self.occurred_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }


class ContextEdge(db.Model):
    """
    Typed, weighted, temporal edge between two context graph nodes.

    Edge types: CAUSED_BY, INDICATES, LED_TO, CORRELATES_WITH,
                INVOLVES, BELONGS_TO, BENCHMARKED_BY, SOURCED_FROM, SUPERSEDES

    Edges carry revenue context: "this signal CAUSED_BY that failure
    and the combined chain puts $2.4M ARR at risk."
    """
    __tablename__ = 'context_edges'

    edge_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=True, index=True)
    from_node_id = db.Column(db.Integer, db.ForeignKey('context_nodes.node_id', ondelete='CASCADE'), nullable=False, index=True)
    to_node_id = db.Column(db.Integer, db.ForeignKey('context_nodes.node_id', ondelete='CASCADE'), nullable=False, index=True)

    edge_type = db.Column(db.String(30), nullable=False, index=True)
    lag_days = db.Column(db.Integer)
    # CAUSED_BY, INDICATES, LED_TO, CORRELATES_WITH, INVOLVES,
    # BELONGS_TO, BENCHMARKED_BY, SOURCED_FROM, SUPERSEDES

    weight = db.Column(db.Numeric(3, 2), nullable=False, default=1.0)  # 0.00-1.00
    # No column-level default on confidence: WS-2 2c (utils/edge_factory.py)
    # writes an explicit confidence=None for inferred edges (no calibrated
    # point estimate), and SQLAlchemy's client-side ColumnDefault fires at
    # flush time even when the value was explicitly set to None — silently
    # replacing it with 1.0 and defeating the entire point (a fabricated
    # point estimate disguised as a real one). Every caller that WANTS a
    # default already gets one from a function-level parameter default
    # (add_edge/upsert_edge both default confidence=1.0 in their own
    # signatures) — a column-level default here is redundant with those and
    # was the sole source of that bug in the old repo. Do not re-add one.
    confidence = db.Column(db.Numeric(3, 2))

    revenue_impact = db.Column(db.Numeric(15, 2))        # $ impact of this causal link
    revenue_impact_type = db.Column(db.String(30))

    properties = db.Column(db.JSON, default=dict)
    # e.g. {"lag_days": 14, "evidence": "ticket volume spike preceded churn signal"}

    source_platform = db.Column(db.String(50))
    created_by = db.Column(db.String(50))                 # "cs_pulse_engine", "csv_import", "mcp_sfdc"

    occurred_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, server_default=db.func.now())

    # Supersession. NULL (the default, no backfill) means "this edge is
    # live." A non-NULL value is the edge_id of the edge that superseded it
    # — set by utils.supersession.apply_supersession(), called from
    # upsert_edge() as a new edge is written on the same (from_node_id,
    # to_node_id, edge_type) triple. Plain typed column, no FK constraint.
    # Every edge-reading function's hot-path predicate is
    # `WHERE superseded_by IS NULL`, which needs to be indexable, so this is
    # a real column rather than a properties-JSON key.
    superseded_by = db.Column(db.Integer, nullable=True)

    from_node = db.relationship('ContextNode', foreign_keys=[from_node_id], backref='outgoing_edges')
    to_node = db.relationship('ContextNode', foreign_keys=[to_node_id], backref='incoming_edges')

    __table_args__ = (
        db.Index('idx_ctx_edge_from', 'from_node_id', 'edge_type'),
        db.Index('idx_ctx_edge_to', 'to_node_id', 'edge_type'),
        db.Index('idx_ctx_edge_type', 'edge_type'),
        db.Index('idx_ctx_edge_pair', 'from_node_id', 'to_node_id', 'edge_type'),
        db.Index('idx_ctx_edge_superseded_by', 'superseded_by'),
    )

    def to_dict(self):
        return {
            'edge_id': self.edge_id,
            'from_node_id': self.from_node_id,
            'to_node_id': self.to_node_id,
            'edge_type': self.edge_type,
            'weight': float(self.weight) if self.weight else None,
            'confidence': float(self.confidence) if self.confidence else None,
            'revenue_impact': float(self.revenue_impact) if self.revenue_impact else None,
            'revenue_impact_type': self.revenue_impact_type,
            'properties': self.properties,
            'source_platform': self.source_platform,
            'created_by': self.created_by,
            'occurred_at': self.occurred_at.isoformat() if self.occurred_at else None,
            'superseded_by': self.superseded_by,
        }


class QualitativeSignal(db.Model):
    """
    Qualitative signals — customer engagement signals (emails, meetings,
    tickets, Slack, transcripts). Required by signal_engine/ (5 of its 7
    files query or write this table directly); the QSIM enrichment columns
    below (source_type through consent_verified) are already part of the
    live schema, not a separate migration-time addition.
    """
    __tablename__ = 'qualitative_signals'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    signal_id = db.Column(db.String(50), nullable=False)  # tenant-scoped via composite unique
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.account_id'), nullable=False, index=True)
    signal_date = db.Column(db.Date, nullable=False, index=True)
    signal_type = db.Column(db.String(50), nullable=True, index=True)  # email, meeting, ticket, etc.
    content = db.Column(db.Text, nullable=True)
    sentiment = db.Column(db.String(50), nullable=True, index=True)  # positive, negative, neutral
    stakeholder_level = db.Column(db.String(50), nullable=True)
    stakeholder_title = db.Column(db.String(255), nullable=True)
    sentiment_score = db.Column(db.Numeric, nullable=True)
    keywords = db.Column(db.Text, nullable=True)
    is_narrative_signal = db.Column(db.Boolean, nullable=True)

    # QSIM Signal Engine enrichment columns — nullable, only populated when
    # FEATURE_SIGNAL_ENGINE=true.
    source_type = db.Column(db.String(30), nullable=True)           # slack, email, transcript, manual
    raw_text = db.Column(db.Text, nullable=True)                    # Original unstructured text
    relationship_sentiment = db.Column(db.Float, nullable=True)     # -1.0 to +1.0
    product_sentiment = db.Column(db.Float, nullable=True)          # -1.0 to +1.0
    urgency_score = db.Column(db.Float, nullable=True)              # 0.0 to 1.0
    escalation_probability = db.Column(db.Float, nullable=True)     # 0.0 to 1.0
    intent_signals = db.Column(db.JSON, nullable=True)              # List of intent codes
    stakeholder_roles = db.Column(db.JSON, nullable=True)           # [{role, name}]
    suggested_action = db.Column(db.String(500), nullable=True)     # LLM-recommended action
    confidence = db.Column(db.JSON, nullable=True)                  # Per-field confidence scores
    requires_review = db.Column(db.Boolean, default=False)          # True if confidence < 0.6
    llm_model_version = db.Column(db.String(100), nullable=True)    # Model ID for audit
    composite_signal_id = db.Column(db.String(100), nullable=True)  # Dedup parent link
    dedup_confidence = db.Column(db.Float, nullable=True)           # Merge confidence
    cg_node_id = db.Column(db.Integer, nullable=True)               # Linked CG node
    alert_suppressed = db.Column(db.Boolean, default=False)         # CG collision suppressed
    structural_urgency = db.Column(db.String(20), nullable=True)    # critical/high/medium/low
    effective_urgency = db.Column(db.String(20), nullable=True)     # max(structural, llm)
    consent_verified = db.Column(db.Boolean, default=False)         # Transcript consent

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'signal_id', name='uq_customer_signal_id'),
        db.Index('idx_signals_customer_account', 'customer_id', 'account_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'signal_id': self.signal_id,
            'customer_id': self.customer_id,
            'account_id': self.account_id,
            'signal_date': self.signal_date.isoformat() if self.signal_date else None,
            'signal_type': self.signal_type,
            'signal_text': self.content,  # API compatibility alias
            'content': self.content,
            'sentiment': self.sentiment,
            'stakeholder_level': self.stakeholder_level,
            'stakeholder_title': self.stakeholder_title,
            'sentiment_score': float(self.sentiment_score) if self.sentiment_score else None,
            'keywords': self.keywords,
            'is_narrative_signal': self.is_narrative_signal,
        }


class KPIMeasurement(db.Model):
    """
    Universal per-vertical-account KPI store. Renamed from the old repo's
    DC2SKPI (2026-09-01) -- despite that name, it was used for every
    vertical, not just dc2_s, confirmed live in the old repo (72,201 rows,
    all verticals, zero rows in any of the five other KPI-shaped tables
    that existed there). The old repo's own docstring already admitted the
    name didn't match reality and left the rename as a "later" backlog
    item that was never done; fixed here on day one instead, while it's
    free -- see docs/lessons on the docstring-drift pattern this
    generalizes from.
    """
    __tablename__ = 'kpi_measurements'

    kpi_id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.account_id'), nullable=False, index=True)
    kpi_code = db.Column(db.String(50), nullable=False, index=True)
    value = db.Column(db.Numeric(10, 2), nullable=False)
    target = db.Column(db.Numeric(10, 2))
    pillar = db.Column(db.String(10), index=True)
    weight = db.Column(db.Numeric(5, 4))
    status = db.Column(db.String(20))
    measured_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'kpi_code', 'measured_at', name='unique_kpi_measurement'),
        db.Index('idx_kpi_measurement_account_code', 'account_id', 'kpi_code'),
    )

    def to_dict(self):
        return {
            'kpi_id': self.kpi_id,
            'account_id': self.account_id,
            'kpi_code': self.kpi_code,
            'value': float(self.value),
            'target': float(self.target) if self.target else None,
            'pillar': self.pillar,
            'weight': float(self.weight) if self.weight else None,
            'status': self.status,
            'measured_at': self.measured_at.isoformat() if self.measured_at else None,
        }


class CsvUploadStaging(db.Model):
    """Holds raw uploaded CSV content between upload_csv() and the later
    process_data() call, keyed by (customer_id, file_type). Added 2026-09-01
    (Tier 2A) as this build's replacement for the old repo's disk-based
    staging (upload_csv wrote to verticals/customerNNN-{vertical}/{subdir}/
    -- a per-customer filesystem layout Tier 1 already established doesn't
    exist here, DB + JSON catalogs only). A re-upload of the same file_type
    before processing replaces the staged content (upsert on the unique
    constraint) rather than the old repo's disk-append -- simpler, and
    correct for the canonical 4-CSV-then-process_data() flow this build
    targets; the old repo's append mode existed for a different pattern
    (incremental uploads across separate already-processed runs) not in
    scope here.
    """
    __tablename__ = 'csv_upload_staging'
    staging_id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)
    file_type = db.Column(db.String(100), nullable=False)  # canonical filename, e.g. 'accounts.csv'
    csv_content = db.Column(db.Text, nullable=False)
    row_count = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'file_type', name='unique_customer_staged_file'),
    )


class JourneyData(db.Model):
    """One journey per account — Wizard A v2's output (Tier 2A-5, 2026-09-02).

    journey_json is schema v3 (see journeys/journey_builder.py): evidence-
    bearing episodes, phases with transition triggers, an evidence-cited arc
    hypothesis (or 'steady' / 'unclassified'), the leading-vs-trailing
    divergence series with first-warning dates, counterfactual hooks around
    interventions, the expected-path overlay from the story-arc template,
    and the shared feature vector Wizards B and D both read. The old repo's
    v2 journey was the health table re-keyed (one event per month, sentiment
    = sign of the health delta) — see docs/design/wizard-a-assessment.md.
    """
    __tablename__ = 'journey_data'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.customer_id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.account_id'), nullable=False, index=True)
    journey_json = db.Column(db.JSON, nullable=False)
    total_weeks = db.Column(db.Integer)
    journey_pattern = db.Column(db.String(50))     # arc_type, or 'steady' / 'unclassified'
    generator_version = db.Column(db.String(20), default='3.0')
    generated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'account_id', name='unique_account_journey'),
    )


class HealthScore(db.Model):
    """L3: Overall health score (weighted average of pillar scores)."""
    __tablename__ = 'health_scores'

    health_score_id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.account_id'), nullable=False, index=True)
    measurement_month = db.Column(db.Date, nullable=False, index=True)

    health_score = db.Column(db.Numeric(5, 2))      # 0-100 weighted average (kpi_only today; composite when Signal-DNA ships)
    health_status = db.Column(db.String(20))        # excellent, good, warning, critical
    trend = db.Column(db.String(20))                # improving, declining, stable
    change_from_last_month = db.Column(db.Numeric(5, 2))

    # Score decomposition — separation principle: kpi_only_score is the pure
    # KPI-weighted score, NEVER contains signal or composite contribution,
    # and is the ONLY score that should feed NRR forecasting / churn
    # probability models. health_score == kpi_only_score today; they
    # diverge once unified (signal-blended) scoring ships.
    kpi_only_score = db.Column(db.Numeric(5, 2))    # kpi_only — safe for NRR/churn math
    composite_score = db.Column(db.Numeric(5, 2))   # signal-blended composite (future)

    # Leading (qualitative) score + its divergence from trailing (kpi_only).
    # These are the surfaced early-warning artifacts — a leading indicator's
    # value is in its GAP from the KPIs, not in a blend that averages the
    # gap away. This is the two-layer indicator model: LEADING (qualitative
    # signals) vs TRAILING (KPI rollup) health is the core differentiator —
    # never silently blend these into one number by default.
    qual_score = db.Column(db.Numeric(5, 2))        # LEADING — qual signal score (0-100)
    divergence = db.Column(db.Numeric(5, 2))        # leading - trailing (signed)
    early_warning = db.Column(db.String(24))        # 'early_warning' | 'recovery_watch' | 'aligned'

    contributing_pillars = db.Column(db.JSON)  # {"P1": 80, "P2": 92, ...}
    pillar_weights = db.Column(db.JSON)        # {"P1": 0.15, "P2": 0.20, ...}

    calculated_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'measurement_month', name='unique_health_score'),
        db.Index('idx_health_score_account_month', 'account_id', 'measurement_month'),
        db.Index('idx_health_score_status', 'health_status'),
    )

    def to_dict(self):
        return {
            'health_score_id': self.health_score_id,
            'account_id': self.account_id,
            'health_score': float(self.health_score) if self.health_score else None,
            'kpi_only_score': float(self.kpi_only_score) if self.kpi_only_score else (
                float(self.health_score) if self.health_score else None
            ),
            'composite_score': float(self.composite_score) if self.composite_score else None,
            'qual_score': float(self.qual_score) if self.qual_score is not None else None,
            'divergence': float(self.divergence) if self.divergence is not None else None,
            'early_warning': self.early_warning,
            'health_status': self.health_status,
            'trend': self.trend,
            'change_from_last_month': float(self.change_from_last_month) if self.change_from_last_month else None,
            'contributing_pillars': self.contributing_pillars,
            'pillar_weights': self.pillar_weights,
            'measurement_month': self.measurement_month.isoformat() if self.measurement_month else None,
        }
