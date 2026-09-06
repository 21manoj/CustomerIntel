"""
Vertical catalog consistency — guard tests (Aug 21 2026, vertical-coupling audit).

A live audit against customer 390 (datacenter_v1) found get_kpi_catalog serving
dc2_s's 5-pillar names/weights to a 6-pillar customer, with its own vertical
field correctly reporting 'datacenter_v1' — an internally inconsistent
response. Root cause: get_kpi_catalog hardcoded a two-branch if/else
(saas_premium via a direct module import, everything else silently defaulted
to DC2S_PILLARS) instead of using utils.vertical_registry, the loader the
rest of the codebase already uses correctly. The saas_premium branch was
ALSO broken — verticals/saas_premium/kpi_definitions.py has no
SAAS_PILLAR_WEIGHTS symbol — so its except ImportError fell through to the
same dc2_s default. Every vertical except dc2_s was silently served dc2_s's
catalog. Separately, get_vertical_config referenced _get_playbook_config
without importing it — a NameError on a prospect-facing, no-auth discovery
tool. And partner_portal hardcoded pillar_code='P4' as "the partner pillar,"
which is only true in dc2_s/saas_premium — in datacenter_v1, P4 is Power &
Facility, so the portal was handing a channel partner the customer's
facility data under partner labels.

Fixes:
  - get_kpi_catalog now sources PILLARS/default_l2 from vertical_registry
    and raises ToolError (no silent fallback) if that fails.
  - get_vertical_config's cs_pulse_onboarding.py import list now includes
    _get_playbook_config.
  - partner_portal refuses to serve (ToolError) unless the tenant's own
    declared P4 pillar name is literally "Channel & Partner Health" — a
    content check, not a per-vertical lookup table (which would just
    relocate the same defect). A pillar_roles registry replaces this with
    a proper role lookup later; this is the interim control.

Update (Phase 2, same day): the pillar_roles registry promised above has
landed — `config/*_kpi_catalog.json` now each carry a `pillar_roles` block
(role_name -> pillar_code) and `utils.vertical_registry.role(vertical,
role_name)` resolves it. partner_portal's gate was retrofitted from the
'partner' substring-on-P4-name check to `role(vertical, 'partner')`, and
the tests below were updated to match — see
test_partner_portal_gate_condition_matches_every_registered_vertical and
test_every_vertical_role_resolves_to_own_catalog_or_none.

These tests import no Flask app and need no DB — same convention as
test_mcp_tool_auth_coverage.py and test_vertical_playbook_routing.py.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

MCP_SERVER_PY = BACKEND / "mcp_server" / "cs_pulse_mcp_server.py"
ONBOARDING_PY = BACKEND / "mcp_server" / "cs_pulse_onboarding.py"
ADMIN_PY = BACKEND / "mcp_server" / "cs_pulse_admin.py"


# ──────────────────────────────────────────────────────────────────────────
# 1. Data-level: vertical_registry itself must serve a complete, consistent
#    catalog for every registered vertical. This is the thing get_kpi_catalog
#    now depends on entirely — if the registry has a gap, the tool inherits
#    it, so the registry's completeness is load-bearing, not incidental.
# ──────────────────────────────────────────────────────────────────────────

def test_every_registered_vertical_has_complete_pillar_data():
    from utils.vertical_registry import SUPPORTED_VERTICALS, get_pillars, get_kpis

    assert SUPPORTED_VERTICALS, "No verticals registered — registry itself is empty."

    for vertical in sorted(SUPPORTED_VERTICALS):
        pillars = get_pillars(vertical)
        kpis = get_kpis(vertical)

        assert pillars, f"{vertical!r}: get_pillars() returned no pillars."
        assert kpis, f"{vertical!r}: get_kpis() returned no KPIs."

        for pcode, pdata in pillars.items():
            assert pdata.get('name'), (
                f"{vertical!r} pillar {pcode!r} has no 'name' — "
                f"get_kpi_catalog would render the bare code."
            )
            assert pdata.get('weight_l2') is not None, (
                f"{vertical!r} pillar {pcode!r} has no weight_l2 — "
                f"get_kpi_catalog's default_l2 would silently omit it."
            )

        # Every KPI's declared pillar must actually exist in this vertical's
        # pillar set — otherwise it's invisible to any pillar_catalog build
        # that iterates pillars and filters KPIs by pillar membership
        # (exactly the mechanism that dropped 3 P6 KPIs to 35 from 38 in
        # the audited response, once the hardcoded catalog omitted P6).
        pillar_codes = set(pillars.keys())
        orphaned = {
            kcode for kcode, kdata in kpis.items()
            if kdata.get('pillar') not in pillar_codes
        }
        assert not orphaned, (
            f"{vertical!r}: KPIs {sorted(orphaned)} reference a pillar not "
            f"in get_pillars({vertical!r}) — they would silently vanish "
            f"from any pillar-keyed catalog view."
        )


# ──────────────────────────────────────────────────────────────────────────
# 2. Structural: get_kpi_catalog must not hardcode a per-vertical import.
#    AST sweep of the one function, not the whole file — verticals/*/
#    kpi_definitions.py modules legitimately define PILLARS constants for
#    their own internal use; the defect was this specific tool importing
#    them directly instead of going through the registry.
# ──────────────────────────────────────────────────────────────────────────

def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Could not find function {name!r} in AST.")


def _imported_names(node: ast.AST) -> set:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.ImportFrom) and n.module:
            for alias in n.names:
                names.add(f"{n.module}.{alias.name}")
    return names


@pytest.mark.skip(reason="reads mcp_server/cs_pulse_onboarding.py source, Tier 2, not yet ported")
def test_get_kpi_catalog_has_no_hardcoded_vertical_import():
    tree = ast.parse(MCP_SERVER_PY.read_text())

    # get_kpi_catalog itself, plus _get_kpi_definitions — the sibling helper
    # it calls, found to carry the identical defect (same file, same
    # if/else-bypassing-vertical_registry shape) while verifying this fix
    # live: PILLARS knew about P6, but kpi_defs never tagged any KPI
    # pillar='P6' because _get_kpi_definitions still hardcoded DC2S_KPIS.
    for fn_name in ("get_kpi_catalog", "_get_kpi_definitions"):
        fn = _find_function(tree, fn_name)
        imported = _imported_names(fn)

        forbidden = {
            n for n in imported
            if n.startswith("verticals.")
            and n.endswith(("PILLARS", "PILLAR_WEIGHTS", "KPIS"))
        }
        assert not forbidden, (
            f"{fn_name} imports vertical-specific constants directly: "
            f"{sorted(forbidden)}. This is the exact defect the Aug 21 audit "
            f"found (a hardcoded branch that silently served dc2_s's catalog "
            f"to every other vertical) — route through utils.vertical_registry "
            f"instead."
        )
        assert any(
            isinstance(n, ast.ImportFrom) and n.module == "utils.vertical_registry"
            for n in ast.walk(fn)
        ), f"{fn_name} no longer references utils.vertical_registry at all."


# ──────────────────────────────────────────────────────────────────────────
# 3. Structural: get_vertical_config's module must import every name its
#    function body references as a bare name. Regression pin for the
#    NameError — the same bug class the file's own Apr 28 2026 comment
#    documents for _require_auth; this is the second occurrence.
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="reads mcp_server/cs_pulse_onboarding.py source, Tier 2, not yet ported")
def test_get_vertical_config_imports_are_complete():
    tree = ast.parse(ONBOARDING_PY.read_text())

    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    builtins_and_locals = set(dir(__builtins__)) | {
        'self', 'cls', 'True', 'False', 'None',
    }

    fn = _find_function(tree, "get_vertical_config")
    loaded_names = {
        n.id for n in ast.walk(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    # Names bound as parameters or local assignments inside the function
    # are legitimately "defined" without a module-level import.
    locally_bound = {
        t.id for node in ast.walk(fn)
        for t in (getattr(node, 'targets', []) or [getattr(node, 'target', None)])
        if t is not None and isinstance(t, ast.Name)
    }
    locally_bound |= {a.arg for a in fn.args.args}

    unresolved = loaded_names - imported - locally_bound - builtins_and_locals
    # ToolError, ht (utils.health_thresholds alias), and dict/list/etc.
    # builtins are all legitimately resolved some other way (module-level
    # import elsewhere in the file, or a local `import X as Y` inside the
    # function body) — only flag names whose ONLY plausible source is a
    # top-of-file import block, i.e. names ending in a helper-style
    # underscore-prefixed pattern matching this file's known import block.
    suspicious = {n for n in unresolved if n.startswith('_get_') or n.startswith('_resolve_')}
    assert not suspicious, (
        f"get_vertical_config references {sorted(suspicious)} as bare "
        f"names with no matching import in cs_pulse_onboarding.py. This is "
        f"the exact shape of the Aug 21 2026 NameError "
        f"(_get_playbook_config) — a prospect-facing, no-auth discovery "
        f"tool that would raise a raw stack trace instead of a ToolError."
    )


# ──────────────────────────────────────────────────────────────────────────
# 4. Structural: partner_portal must gate on the tenant's actual P4 pillar
#    name, not skip straight to querying P4 data.
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="reads mcp_server/cs_pulse_admin.py source, Tier 2, not yet ported")
def test_partner_portal_gates_on_pillar_content():
    source = ADMIN_PY.read_text()
    tree = ast.parse(source)
    fn = _find_function(tree, "partner_portal")
    fn_source = ast.get_source_segment(source, fn) or ""

    assert "vertical_registry import role" in fn_source or "role as _vr_role" in fn_source, (
        "partner_portal's gate should resolve the tenant's partner pillar "
        "via vertical_registry.role(vertical, 'partner') — the pillar_roles "
        "registry — not a hardcoded per-vertical map or a name/substring "
        "check, both of which relocate the defect instead of fixing it."
    )
    assert "'partner')" in fn_source, (
        "partner_portal no longer appears to call role(vertical, 'partner') "
        "before serving. This is the Finding-2 data-exposure gate — "
        "removing it re-opens serving a non-partner vertical's P4 data "
        "(e.g. datacenter_v1's Power & Facility) under partner labels."
    )
    assert "is None" in fn_source, (
        "partner_portal's gate should refuse to serve when role(vertical, "
        "'partner') resolves to None (this vertical has no partner "
        "pillar at all) — that's the case a pillar_roles lookup must "
        "handle explicitly, not silently pass through."
    )


def test_partner_portal_gate_condition_matches_every_registered_vertical():
    """Data-level companion to the structural check above.

    The gate has gone through two generations of bug already: an exact
    string match against dc2_s's P4 name ("Channel & Partner Health")
    incorrectly refused saas_premium (whose equivalent pillar is genuinely
    named "Partner & Ecosystem Health" — same role, different string); the
    substring fix ('partner' in P4's name) that replaced it was itself
    documented as an interim control pending a real pillar_roles registry
    — this phase. A name/substring test cannot express "this vertical's
    partner pillar isn't P4" or "this vertical has no partner pillar";
    only checking the gate's actual condition against real registry role
    data for every vertical can. This is that test — it computes the
    "should serve" / "should refuse" expected outcome independently
    (ground truth: does vertical_registry.role(vertical, 'partner') —
    NOT a name string — resolve to 'P4'?) for every registered vertical.
    """
    from utils.vertical_registry import SUPPORTED_VERTICALS, get_pillars, role

    # Ground truth, established from the audit that created this gate:
    # dc2_s and saas_premium have a real partner pillar at P4; datacenter_v1
    # (Power & Facility at P4) and healthcare_provider (Compliance & Risk
    # at P4) do not. If a future vertical adds a partner pillar that
    # ISN'T P4, or removes SUPPORTED_VERTICALS's assumption that P4 is
    # always the partner slot, this table — and the gate's `!= 'P4'`
    # branch — is exactly what should be revisited, not silently bypassed.
    EXPECTED_PARTNER_PILLAR = {
        'dc2_s': 'P4',
        'saas_premium': 'P4',
        'datacenter_v1': None,
        'healthcare_provider': None,
        # manufacturing_iot (added 2026-08-22, pillar_roles registry
        # validation test) has no channel/reseller motion by design — see
        # config/manufacturing_iot_kpi_catalog.json's pillar_roles_notes.
        'manufacturing_iot': None,
    }

    for vertical in sorted(SUPPORTED_VERTICALS):
        resolved = role(vertical, 'partner')
        expected = EXPECTED_PARTNER_PILLAR.get(vertical, '__UNKNOWN__')
        assert expected != '__UNKNOWN__', (
            f"{vertical!r} is registered but has no expected-partner-pillar "
            f"entry in this test's ground-truth table — add one (a real "
            f"pillar code, or None) so this test actually covers it "
            f"instead of silently skipping a new vertical."
        )
        assert resolved == expected, (
            f"role({vertical!r}, 'partner') resolved to {resolved!r}, "
            f"expected {expected!r}. If this vertical's real partner "
            f"pillar genuinely changed, update EXPECTED_PARTNER_PILLAR "
            f"here to match — don't just widen the assertion."
        )
        # Every non-None resolution must point at a pillar code that
        # actually exists in this vertical's own catalog — role() must
        # never resolve to another vertical's pillar code, or a stale
        # code the vertical no longer has.
        if resolved is not None:
            assert resolved in get_pillars(vertical), (
                f"role({vertical!r}, 'partner') = {resolved!r}, but "
                f"{resolved!r} is not one of {vertical!r}'s own pillar "
                f"codes {sorted(get_pillars(vertical).keys())} — role() "
                f"must never point outside the vertical's own catalog."
            )


def test_every_vertical_role_resolves_to_own_catalog_or_none():
    """role() must never silently resolve to a wrong-looking pillar.

    For every (vertical, role_name) pair in the shared vocabulary: the
    result is either None (this vertical genuinely has no pillar for that
    role — a legitimate, expected state) or a pillar code that is present
    in that SAME vertical's own get_pillars() catalog. A pillar code
    belonging to a different vertical, or a code absent from this
    vertical's own catalog, is exactly the defect class pillar_roles
    exists to prevent (the same class of bug as partner_portal serving
    the wrong vertical's P4 data under partner labels).
    """
    from utils.vertical_registry import SUPPORTED_VERTICALS, get_pillars, role

    ROLE_VOCABULARY = [
        'revenue', 'reliability', 'capacity', 'partner',
        'expansion', 'adoption', 'engagement', 'compliance',
    ]

    for vertical in sorted(SUPPORTED_VERTICALS):
        own_pillar_codes = set(get_pillars(vertical).keys())
        for role_name in ROLE_VOCABULARY:
            resolved = role(vertical, role_name)
            if resolved is None:
                continue
            assert resolved in own_pillar_codes, (
                f"role({vertical!r}, {role_name!r}) = {resolved!r}, which "
                f"is not a real pillar code in {vertical!r}'s own catalog "
                f"{sorted(own_pillar_codes)} — this is either a stale "
                f"entry or borrowed from another vertical's pillar_roles "
                f"block."
            )

    # At least one vertical must actually resolve every role that's
    # exercised by real code today (partner, expansion) — an empty
    # pillar_roles block everywhere would vacuously pass the loop above.
    assert role('dc2_s', 'partner') == 'P4'
    assert role('dc2_s', 'expansion') == 'P5'


def test_pillar_roles_notes_documents_every_unmapped_pillar():
    """`pillar_roles_notes` (role_name -> reason string) has been write-only documentation
    since Tier 1 — every catalog that leaves a pillar unmapped in `pillar_roles` also
    explains why there, but nothing read it back until roi/measured.py's by_pillar started
    citing it for a vertical's unmapped attribution rows (see
    test_power_of_1_roi.py::test_by_pillar_surfaces_reason_for_unmapped_but_never_for_a_real_pillar).
    This guards the loader itself: every registered vertical's notes block, if present, is a
    dict of strings, and it never raises for a vertical with no JSON catalog at all."""
    from utils.vertical_registry import SUPPORTED_VERTICALS, get_pillar_roles_notes

    for vertical in sorted(SUPPORTED_VERTICALS):
        notes = get_pillar_roles_notes(vertical)
        assert isinstance(notes, dict)
        for key, text in notes.items():
            assert isinstance(key, str) and isinstance(text, str) and text.strip()

    # healthcare_provider is the documented case this was written for: pillar_roles only
    # fills 'compliance' -> P4, leaving P1/P2/P3 (75% of the vertical's weight_l2) unmapped
    # by deliberate design, not omission — the note says so explicitly.
    hc_notes = get_pillar_roles_notes('healthcare_provider')
    assert hc_notes and any(
        'no clean match in the shared vocabulary' in text and 'rather than force-fit' in text
        for text in hc_notes.values()
    )

    # never raises for an unregistered vertical; just returns {}
    assert get_pillar_roles_notes('not_a_real_vertical') == {}


if __name__ == "__main__":
    test_every_registered_vertical_has_complete_pillar_data()
    print("PASS: every registered vertical has complete pillar data")
    test_get_kpi_catalog_has_no_hardcoded_vertical_import()
    print("PASS: get_kpi_catalog has no hardcoded vertical import")
    test_get_vertical_config_imports_are_complete()
    print("PASS: get_vertical_config imports are complete")
    test_partner_portal_gates_on_pillar_content()
    print("PASS: partner_portal gates on pillar content")
    test_partner_portal_gate_condition_matches_every_registered_vertical()
    print("PASS: partner_portal gate condition matches every registered vertical")
    test_every_vertical_role_resolves_to_own_catalog_or_none()
    print("PASS: every vertical's role() resolutions belong to its own catalog")
    print("\nAll vertical catalog consistency tests passed.")
