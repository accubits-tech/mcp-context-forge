"""Smoke test for the sample server. Replace with real tool tests."""

from sample_mcp_server.server import ping


def test_ping_returns_pong():
    assert ping() == "pong"
