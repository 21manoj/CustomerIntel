"""
get_evidence_graph — the MCP read-only tool wrapping utils/context_graph.py's
get_nodes/get_edges (backend/mcp_server/cs_pulse_onboarding.py, next to
get_evidence). Nodes come back in the same evidence_view shape get_evidence
already returns (journeys/read.py); edges carry from_node_id, to_node_id,
edge_type and any confidence/derivation/evidence_tier properties.

Two things this suite proves:
  * it returns real nodes (and the edge between them) for an account with evidence
  * a bogus or another customer's account_id never leaks that other
    customer's data — get_nodes() itself only filters by account_id (no
    customer_id parameter), so the tenant check has to happen above it
"""
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from extensions import db                                 # noqa: E402

TEST_DB = os.environ.get('DATABASE_URL', 'postgresql://manojgupta@localhost:5432/customerintel_test')
if 'test' not in TEST_DB.rsplit('/', 1)[-1].lower():
    raise RuntimeError('refusing non-test database')

from mcp_server.common import get_flask_app               # noqa: E402
app = get_flask_app()

from models import Account, ContextEdge, ContextNode, Customer  # noqa: E402


def _mk_customer(tag: str) -> int:
    c = Customer(customer_name=f'EvGraph {tag}', email=f'evgraph_{tag}@t.test', domain=f'evgraph-{tag}.test')
    db.session.add(c)
    db.session.flush()
    return c.customer_id


def _mk_account(customer_id: int, name: str) -> int:
    a = Account(customer_id=customer_id, account_name=name, revenue=250_000, external_account_id=name)
    db.session.add(a)
    db.session.flush()
    return a.account_id


def _mk_node(customer_id: int, account_id: int, **kwargs) -> ContextNode:
    defaults = {
        'customer_id': customer_id, 'account_id': account_id, 'node_type': 'SIGNAL', 'node_subtype': 'vendor_system_outage',
        'source': 'observed', 'source_platform': 'test', 'tier': 1, 'title': 'Test evidence node',
        'occurred_at': datetime(2026, 1, 1), 'properties': {'quote': 'a thing happened', 'role': 'infra_incident'},
    }
    defaults.update(kwargs)
    n = ContextNode(**defaults)
    db.session.add(n)
    db.session.flush()
    return n


@pytest.fixture()
def two_tenants():
    """Customer A (with a SIGNAL->SIGNAL LED_TO edge) and an unrelated Customer B, each with their own account."""
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        cid_a = _mk_customer(f'a_{tag}')
        aid_a = _mk_account(cid_a, f'Account A {tag}')
        n1 = _mk_node(cid_a, aid_a, occurred_at=datetime(2026, 1, 5), title='Outage at the vendor',
                     properties={'quote': 'prod outage for 3 hours', 'role': 'infra_incident'})
        n2 = _mk_node(cid_a, aid_a, node_subtype='risk_mitigation_action', occurred_at=datetime(2026, 1, 20),
                     title='Corrective action plan issued', properties={'quote': 'CAP issued', 'role': 'intervention'})
        from utils.edge_factory import create_inferred_edge, AUTO_TRIGGER_DERIVATION
        edge, created = create_inferred_edge(
            n1.node_id, n2.node_id, edge_type='LED_TO', source_platform='test.edge_factory',
            derivation=AUTO_TRIGGER_DERIVATION, customer_id=cid_a, label='outage to mitigation',
        )
        assert created and edge is not None

        cid_b = _mk_customer(f'b_{tag}')
        aid_b = _mk_account(cid_b, f'Account B {tag}')
        _mk_node(cid_b, aid_b, title="B's own evidence", properties={'quote': "customer B's own secret", 'role': 'infra_incident'})

        db.session.commit()
        yield {
            'cid_a': cid_a, 'aid_a': aid_a, 'n1': n1.node_id, 'n2': n2.node_id, 'edge_id': edge.edge_id,
            'cid_b': cid_b, 'aid_b': aid_b,
        }
        db.session.remove()
        db.drop_all()


def test_returns_real_nodes_and_the_edge_between_them(two_tenants):
    from mcp_server.cs_pulse_onboarding import get_evidence_graph
    t = two_tenants
    with app.app_context():
        g = get_evidence_graph(t['cid_a'], t['aid_a'])

    assert g['customer_id'] == t['cid_a']
    assert g['account_id'] == t['aid_a']

    node_ids = {n['node_id'] for n in g['nodes']}
    assert node_ids == {t['n1'], t['n2']}
    # nodes are the same evidence_view shape get_evidence returns
    n1_view = next(n for n in g['nodes'] if n['node_id'] == t['n1'])
    assert n1_view['quote'] == 'prod outage for 3 hours'
    assert n1_view['role'] == 'infra_incident'
    assert n1_view['account_id'] == t['aid_a']
    assert 'provenance' in n1_view and n1_view['provenance']['source_platform'] == 'test'

    assert len(g['edges']) == 1
    e = g['edges'][0]
    assert e['from_node_id'] == t['n1']
    assert e['to_node_id'] == t['n2']
    assert e['edge_type'] == 'LED_TO'
    assert e['confidence'] is None                 # edge_factory: inferred edges carry no calibrated estimate
    from utils.edge_factory import AUTO_TRIGGER_DERIVATION
    assert e['derivation'] == AUTO_TRIGGER_DERIVATION
    assert e['evidence_tier'] == 'inferred'


def test_foreign_account_id_returns_empty_not_customer_bs_data(two_tenants):
    """account_id from a DIFFERENT customer must not leak that customer's nodes,
    even though utils.context_graph.get_nodes() itself has no customer_id
    filter at all — it only filters by account_id."""
    from mcp_server.cs_pulse_onboarding import get_evidence_graph
    t = two_tenants
    with app.app_context():
        g = get_evidence_graph(t['cid_a'], t['aid_b'])     # customer A asking for customer B's account

    assert g['nodes'] == []
    assert g['edges'] == []


def test_bogus_account_id_returns_empty(two_tenants):
    from mcp_server.cs_pulse_onboarding import get_evidence_graph
    t = two_tenants
    with app.app_context():
        g = get_evidence_graph(t['cid_a'], 999_999_999)

    assert g['nodes'] == []
    assert g['edges'] == []


def test_registered_and_keyed_over_http():
    """get_evidence_graph must be classified in onboarding_tool_registry so the
    HTTP auth gate (mcp_server/auth.py) actually requires a key for it —
    an unlisted tool is a real security gap the registry's own tests catch."""
    from mcp_server.onboarding_tool_registry import KEYED_TOOLS, ONBOARDING_TOOLS
    assert 'get_evidence_graph' in KEYED_TOOLS
    assert 'get_evidence_graph' not in ONBOARDING_TOOLS
