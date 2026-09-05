"""
Session defaults for the suite.

MCP_TRANSPORT: in-process tool calls in fixtures/tests are the local,
trusted path (stdio semantics). The deploy container exports
MCP_TRANSPORT=http for the server; that must not leak into tests that call
tools directly — since keyed tools over HTTP are denied without a key.
Tests that exercise the HTTP surface set 'http' themselves
(server.build_asgi_app does it) and reset it on teardown.
"""
import os

import pytest


@pytest.fixture(scope='session', autouse=True)
def _local_transport_for_in_process_tools():
    prev = os.environ.get('MCP_TRANSPORT')
    os.environ['MCP_TRANSPORT'] = 'stdio'
    yield
    if prev is None:
        os.environ.pop('MCP_TRANSPORT', None)
    else:
        os.environ['MCP_TRANSPORT'] = prev
