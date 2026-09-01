#!/usr/bin/env python3
"""
Feature Toggle System for Safe MVP Deployment
Allows enabling/disabling new features without affecting existing functionality
"""

import os
from typing import Dict, Any
from dataclasses import dataclass
from enum import Enum

class FeatureToggle(Enum):
    """Available feature toggles"""
    FORMAT_DETECTION = "format_detection"
    EVENT_DRIVEN_RAG = "event_driven_rag"
    CONTINUOUS_LEARNING = "continuous_learning"
    REAL_TIME_INGESTION = "real_time_ingestion"
    ENHANCED_UPLOAD = "enhanced_upload"
    TEMPORAL_ANALYSIS = "temporal_analysis"
    MULTI_FORMAT_SUPPORT = "multi_format_support"
    REVENUE_INTELLIGENCE = "revenue_intelligence"
    CONTEXT_GRAPH = "context_graph"
    MCP_SERVER = "mcp_server"
    SIGNAL_ENGINE = "signal_engine"
    ASK_AI_V2 = "ask_ai_v2"
    WITH_LLM = "with_llm"

@dataclass
class FeatureConfig:
    """Feature configuration"""
    enabled: bool
    description: str
    version: str
    dependencies: list = None
    environment_required: str = None

class FeatureToggleManager:
    """Manages feature toggles for safe deployment"""
    
    def __init__(self):
        self.features = {
            # Enterprise Edition: all features enabled by default.
            # Individual features can still be disabled via FEATURE_{NAME}=false env var.
            FeatureToggle.FORMAT_DETECTION: FeatureConfig(
                enabled=True,
                description="Auto-detect and validate file formats",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.EVENT_DRIVEN_RAG: FeatureConfig(
                enabled=True,
                description="Automatic RAG rebuilds on data changes",
                version="1.0.0",
                dependencies=[FeatureToggle.REAL_TIME_INGESTION],
                environment_required=None
            ),
            FeatureToggle.CONTINUOUS_LEARNING: FeatureConfig(
                enabled=True,
                description="Continuous learning and model updates",
                version="1.0.0",
                dependencies=[FeatureToggle.EVENT_DRIVEN_RAG],
                environment_required=None
            ),
            FeatureToggle.REAL_TIME_INGESTION: FeatureConfig(
                enabled=True,
                description="Real-time data ingestion APIs",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.ENHANCED_UPLOAD: FeatureConfig(
                enabled=True,
                description="Enhanced upload with format detection",
                version="1.0.0",
                dependencies=[FeatureToggle.FORMAT_DETECTION],
                environment_required=None
            ),
            FeatureToggle.TEMPORAL_ANALYSIS: FeatureConfig(
                enabled=True,
                description="Temporal analysis and historical trends",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.MULTI_FORMAT_SUPPORT: FeatureConfig(
                enabled=True,
                description="Support for multiple file formats",
                version="1.0.0",
                dependencies=[FeatureToggle.FORMAT_DETECTION],
                environment_required=None
            ),
            FeatureToggle.REVENUE_INTELLIGENCE: FeatureConfig(
                enabled=True,
                description="Power of 1 revenue intelligence: action economics, ROI tracking, capacity planning",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.CONTEXT_GRAPH: FeatureConfig(
                enabled=True,
                description="Context graph intelligence: causal signal edges, stakeholder tracking, "
                            "decision lifecycle, outcome economics, story arcs",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.MCP_SERVER: FeatureConfig(
                enabled=True,
                description="Expose CS Pulse as MCP tool provider for external LLMs (Claude, Copilot, ChatGPT)",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.SIGNAL_ENGINE: FeatureConfig(
                enabled=True,
                description="QSIM Signal Engine: qualitative signal ingestion, LLM enrichment, "
                            "structural urgency classification, CG collision, composite scoring",
                version="1.0.0",
                dependencies=[FeatureToggle.CONTEXT_GRAPH],
                environment_required=None
            ),
            FeatureToggle.ASK_AI_V2: FeatureConfig(
                enabled=True,
                description="Ask AI v2: Claude-powered intelligent assistant with tool_use, "
                            "rich artifact rendering (Mermaid, charts, tables), persona-aware",
                version="1.0.0",
                dependencies=[],
                environment_required=None
            ),
            FeatureToggle.WITH_LLM: FeatureConfig(
                enabled=False,
                description="LLM reasoning layer: Tier 1 KPI inference, causal reasoning, "
                            "action plan generation. Requires ANTHROPIC_API_KEY.",
                version="0.9.0",
                dependencies=[],
                environment_required="ANTHROPIC_API_KEY"
            ),
        }
        
        # Load from environment variables
        self._load_from_environment()
    
    def _load_from_environment(self):
        """Load feature toggles from environment variables"""
        for feature in FeatureToggle:
            env_var = f"FEATURE_{feature.value.upper()}"
            if env_var in os.environ:
                self.features[feature].enabled = os.environ[env_var].lower() == 'true'
    
    def is_enabled(self, feature: FeatureToggle) -> bool:
        """Check if a feature is enabled"""
        if feature not in self.features:
            return False
        
        config = self.features[feature]
        
        # Check if dependencies are enabled
        if config.dependencies:
            for dep in config.dependencies:
                if not self.is_enabled(dep):
                    return False
        
        return config.enabled
    
    def enable_feature(self, feature: FeatureToggle):
        """Enable a feature"""
        if feature in self.features:
            self.features[feature].enabled = True
            print(f"✅ Feature enabled: {feature.value}")
    
    def disable_feature(self, feature: FeatureToggle):
        """Disable a feature"""
        if feature in self.features:
            self.features[feature].enabled = False
            print(f"❌ Feature disabled: {feature.value}")
    
    def get_feature_status(self) -> Dict[str, Any]:
        """Get status of all features"""
        status = {}
        for feature, config in self.features.items():
            status[feature.value] = {
                'enabled': self.is_enabled(feature),
                'description': config.description,
                'version': config.version,
                'dependencies': [dep.value for dep in config.dependencies] if config.dependencies else [],
                'environment_required': config.environment_required
            }
        return status
    
    def validate_dependencies(self) -> Dict[str, Any]:
        """Validate feature dependencies"""
        issues = []
        warnings = []
        
        for feature, config in self.features.items():
            if config.enabled and config.dependencies:
                for dep in config.dependencies:
                    if not self.is_enabled(dep):
                        issues.append(f"Feature '{feature.value}' requires '{dep.value}' to be enabled")
        
        return {
            'issues': issues,
            'warnings': warnings,
            'valid': len(issues) == 0
        }

# Global feature toggle manager
feature_toggles = FeatureToggleManager()

def is_feature_enabled(feature: FeatureToggle) -> bool:
    """Convenience function to check if feature is enabled"""
    return feature_toggles.is_enabled(feature)

def require_feature(feature: FeatureToggle):
    """Decorator to require a feature to be enabled"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            if not is_feature_enabled(feature):
                raise FeatureNotEnabledException(f"Feature '{feature.value}' is not enabled")
            return func(*args, **kwargs)
        return wrapper
    return decorator

class FeatureNotEnabledException(Exception):
    """Exception raised when a required feature is not enabled"""
    pass


# ============================================================
# Context Graph Toggle — Per-Customer DB-Backed
# ============================================================
# The global FeatureToggle.CONTEXT_GRAPH acts as the platform
# master switch (FEATURE_CONTEXT_GRAPH env, or POST /api/feature-toggle).
# It is NOT tied to "enterprise" customer tier — enterprise only affects
# billing/plan metadata unless you wire tier → toggles separately.
# The per-customer toggle in the DB controls whether a specific customer
# has context graph enabled. Both global AND per-customer must be ON.
#
# Sub-toggles allow incremental rollout:
#   story_arcs, signal_edges, stakeholder_tracking,
#   decision_lifecycle, outcome_economics, industry_benchmarks
# ============================================================

# Default sub-toggle config — Enterprise Edition: all ON
CONTEXT_GRAPH_DEFAULT_CONFIG = {
    "story_arcs": True,
    "signal_edges": True,
    "stakeholder_tracking": True,
    "decision_lifecycle": True,
    "outcome_economics": True,
    "industry_benchmarks": True,
}


def is_context_graph_enabled(customer_id: int) -> bool:
    """
    Check if context graph is enabled for a specific customer.
    Requires BOTH the global platform toggle AND the per-customer
    DB toggle to be ON.

    Usage:
        from feature_toggles import is_context_graph_enabled
        if is_context_graph_enabled(customer_id):
            # context graph path
        else:
            # flat signal path (existing behavior)
    """
    # Check global platform toggle first (fast, in-memory)
    if not feature_toggles.is_enabled(FeatureToggle.CONTEXT_GRAPH):
        return False

    # Check per-customer DB toggle
    try:
        from models import FeatureToggle as FTModel
        toggle = FTModel.query.filter_by(
            customer_id=customer_id,
            feature_name='context_graph'
        ).first()
        # Enterprise Edition: default to enabled if no per-customer toggle exists
        return toggle.enabled if toggle else True
    except Exception:
        return True


def get_context_graph_config(customer_id: int) -> dict:
    """
    Get context graph sub-toggle config for a customer.
    Returns the sub-toggle dict with defaults for any missing keys.

    Usage:
        cfg = get_context_graph_config(customer_id)
        if cfg.get('signal_edges'):
            edges = get_signal_edges(account_id)
    """
    if not is_context_graph_enabled(customer_id):
        return {k: False for k in CONTEXT_GRAPH_DEFAULT_CONFIG}

    try:
        from models import FeatureToggle as FTModel
        toggle = FTModel.query.filter_by(
            customer_id=customer_id,
            feature_name='context_graph'
        ).first()
        if toggle and toggle.config:
            # Merge with defaults so new sub-toggles get False
            merged = dict(CONTEXT_GRAPH_DEFAULT_CONFIG)
            merged.update(toggle.config)
            return merged
    except Exception:
        pass

    return dict(CONTEXT_GRAPH_DEFAULT_CONFIG)


def is_context_graph_sub_enabled(customer_id: int, sub_toggle: str) -> bool:
    """
    Check if a specific context graph sub-toggle is enabled.

    Usage:
        from feature_toggles import is_context_graph_sub_enabled
        if is_context_graph_sub_enabled(customer_id, 'signal_edges'):
            build_signal_edges(account_id)
    """
    cfg = get_context_graph_config(customer_id)
    return cfg.get(sub_toggle, False)

# Example usage
if __name__ == "__main__":
    print("🔧 Feature Toggle System")
    print("=" * 50)
    
    # Show current status
    status = feature_toggles.get_feature_status()
    for feature, config in status.items():
        status_icon = "✅" if config['enabled'] else "❌"
        print(f"{status_icon} {feature}: {config['description']}")
    
    # Validate dependencies
    validation = feature_toggles.validate_dependencies()
    if validation['valid']:
        print("\n✅ All feature dependencies are valid")
    else:
        print("\n❌ Feature dependency issues:")
        for issue in validation['issues']:
            print(f"  - {issue}")
