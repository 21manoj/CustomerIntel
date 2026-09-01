"""
id_generator vertical-prefix fallback — regression + new-vertical tests
(2026-08-22).

id_generator.py kept its own hardcoded VERTICAL_PREFIXES/VALID_VERTICALS,
completely separate from utils.vertical_registry.SUPPORTED_VERTICALS (which
auto-discovers verticals by globbing config/*_kpi_catalog.json). Confirmed
live on EC2: a genuinely new vertical (manufacturing_iot, added purely via
a JSON catalog, zero Python code, and already working end-to-end for KPI
catalog resolution) could NOT have a customer onboarded into it — both
create_customer (MCP) and POST /api/register (REST) rejected it with
"Unknown vertical 'manufacturing_iot'", tracing back to
id_generator.resolve_vertical_prefix's hardcoded allowlist. This
contradicted the "drop a catalog JSON, done" architecture principle:
dropping the JSON got you a vertical the *scoring engine* recognized, but
not one customers could actually be onboarded into.

Fix: resolve_vertical_prefix() now falls back to
utils.vertical_registry.SUPPORTED_VERTICALS for verticals with no
hand-picked VERTICAL_PREFIXES entry, deriving a short prefix from the
vertical's own name (first underscore-separated token, truncated to 4
chars) rather than rejecting. The 3 already-established verticals
(dc2_s, saas_premium, datacenter_v1) are untouched — they still resolve
through the hardcoded dict first, so existing IDs are not reinterpreted.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from id_generator import (
    resolve_vertical_prefix,
    generate_id,
    is_valid_prefixed_id,
    extract_vertical,
    VERTICAL_PREFIXES,
)


# ----------------------------------------------------------------
# Regression: the 3 established verticals must stay byte-identical
# ----------------------------------------------------------------

@pytest.mark.parametrize("vertical,expected_prefix", [
    ('dc2_s', 'dc'),
    ('datacenter_v1', 'dc'),
    ('saas_premium', 'saas'),
    # Older aliases that funnel into the same prefixes — also must not move.
    ('saas', 'saas'),
    ('datacenter', 'dc'),
    ('data_center', 'dc'),
    ('dc', 'dc'),
    ('msp', 'msp'),
])
def test_established_vertical_prefixes_unchanged(vertical, expected_prefix):
    assert resolve_vertical_prefix(vertical) == expected_prefix


def test_established_vertical_ids_unchanged_format():
    """Full generated IDs for established verticals keep their exact prefix."""
    assert generate_id('dc2_s', 'customer').startswith('dc_cust_')
    assert generate_id('datacenter_v1', 'account').startswith('dc_acct_')
    assert generate_id('saas_premium', 'user').startswith('saas_usr_')


# ----------------------------------------------------------------
# New vertical (no hand-picked entry) — must NOT be rejected
# ----------------------------------------------------------------

def test_new_vertical_registered_in_registry_gets_a_prefix():
    """
    manufacturing_iot has no entry in id_generator.VERTICAL_PREFIXES but
    IS registered in utils.vertical_registry.SUPPORTED_VERTICALS (it ships
    a config/manufacturing_iot_kpi_catalog.json). It must resolve to a
    valid, non-crashing prefix instead of raising.
    """
    assert 'manufacturing_iot' not in VERTICAL_PREFIXES  # precondition

    from utils.vertical_registry import SUPPORTED_VERTICALS
    assert 'manufacturing_iot' in SUPPORTED_VERTICALS  # precondition

    prefix = resolve_vertical_prefix('manufacturing_iot')
    assert prefix and prefix.isalnum()
    assert prefix == 'manu'  # first token, truncated to 4 chars


def test_new_vertical_generates_valid_id():
    cust_id = generate_id('manufacturing_iot', 'customer')
    assert cust_id.startswith('manu_cust_')
    assert is_valid_prefixed_id(cust_id)
    assert extract_vertical(cust_id) == 'manu'


def test_new_vertical_case_and_whitespace_insensitive():
    assert resolve_vertical_prefix(' Manufacturing_IOT ') == 'manu'


# ----------------------------------------------------------------
# A vertical unknown to BOTH the hardcoded map and the registry must
# still be rejected (fail closed, not silently accepted).
# ----------------------------------------------------------------

def test_truly_unknown_vertical_still_raises():
    with pytest.raises(ValueError):
        resolve_vertical_prefix('this_vertical_does_not_exist_anywhere')


def test_truly_unknown_vertical_id_generation_raises():
    with pytest.raises(ValueError):
        generate_id('this_vertical_does_not_exist_anywhere', 'customer')
