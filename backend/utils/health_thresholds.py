"""
Centralized health score thresholds.

Single source of truth for healthy/at-risk/critical boundaries.
Reads from config/health_thresholds.json so the UI Settings tab can update it.
All backend modules should use these functions instead of hardcoded values.
"""
import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config', 'health_thresholds.json')

# Cache loaded config
_cached = None


def _load():
    global _cached
    if _cached is None:
        with open(_CONFIG_PATH, 'r') as f:
            _cached = json.load(f)['thresholds']
    return _cached


def reload():
    """Force reload from disk (call after Settings UI saves changes)."""
    global _cached
    _cached = None
    return _load()


def healthy_min() -> int:
    """Minimum score to be considered Healthy (inclusive)."""
    return _load()['healthy']['min']


def at_risk_min() -> int:
    """Minimum score to be considered At-Risk (inclusive). Below this is Critical."""
    return _load()['at_risk']['min']


def classify(score: float) -> str:
    """Return 'healthy', 'at_risk', or 'critical' for a given score."""
    t = _load()
    if score >= t['healthy']['min']:
        return 'healthy'
    elif score >= t['at_risk']['min']:
        return 'at_risk'
    return 'critical'


def classify_label(score: float) -> str:
    """Return the display label: 'Healthy', 'At Risk', or 'Critical'."""
    return _load()[classify(score)]['label']


def classify_color(score: float) -> str:
    """Return the color name: 'green', 'yellow', or 'red'."""
    return _load()[classify(score)]['color']


def classify_hex(score: float) -> str:
    """Return the hex color code for the score."""
    return _load()[classify(score)]['hex']


def get_thresholds() -> dict:
    """Return full thresholds config (for API endpoint / frontend consumption)."""
    return _load()
