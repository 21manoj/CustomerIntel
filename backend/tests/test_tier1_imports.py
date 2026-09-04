"""
Tier 1 checkpoint: every carried-over module must import cleanly inside a
real Flask + SQLAlchemy app context, and the schema must create without
error. This is the "imports clean" gate — the first, cheapest bar from the
checkpoint structure agreed for this build; it does not by itself prove
correctness (that's the live-parity check, separately).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from extensions import db


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    db.init_app(app)
    return app


def test_models_import_and_create_all():
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        assert 'context_nodes' in db.metadata.tables
        assert 'context_edges' in db.metadata.tables
        assert 'kpi_measurements' in db.metadata.tables
        assert 'qualitative_signals' in db.metadata.tables


def test_context_graph_utils_import():
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        from utils import context_graph  # noqa: F401
        from utils import context_graph_invariants  # noqa: F401
        from utils import supersession  # noqa: F401
        from utils import provenance  # noqa: F401
        from utils import edge_factory  # noqa: F401


def test_vertical_registry_imports_and_finds_catalogs():
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        from utils import vertical_registry
        verticals = vertical_registry.list_verticals() if hasattr(vertical_registry, 'list_verticals') else None
        # Just prove the module loads and the catalog glob mechanism runs
        # without raising -- functional correctness is the next checkpoint.
        assert vertical_registry is not None


def test_vertical_health_imports():
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        from utils import vertical_health  # noqa: F401


def test_approval_queue_service_imports():
    """ApprovalQueueService (the actual business logic) must import even
    though auth_middleware is a placeholder -- the service class itself
    doesn't touch auth, only the Flask blueprint at the bottom of the file
    does, and that's expected to fail until real auth exists."""
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        from approval_queue import ApprovalQueueService
        assert ApprovalQueueService is not None


def test_llm_budget_controller_imports():
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        from utils import llm_budget_controller  # noqa: F401


def test_signal_engine_modules_import():
    app = _make_app()
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        import signal_engine.models  # noqa: F401
        from signal_engine import enrichment, urgency, pipeline, settings  # noqa: F401


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
