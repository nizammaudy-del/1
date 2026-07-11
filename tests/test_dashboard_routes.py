"""Route-level smoke tests for the dashboard-support endpoints added across
the dashboard UX work (headroom/proxy/routers/dashboard.py).

Covers the test-coverage gap flagged by the Copilot review on the initial
dashboard UX PR: these routes previously had no route-level tests at all.
Not exhaustive — just proves each route registers, responds with the
expected shape, and (where applicable) enforces the loopback guard — plus a
regression test for the `/mcp/usage` `limit` clamping bug fixed alongside it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from headroom.proxy.server import ProxyConfig, create_app


def _make_app():
    config = ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
    )
    return create_app(config)


@pytest.fixture
def client():
    app = _make_app()
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    ) as test_client:
        yield test_client


@pytest.fixture
def external_client():
    """TestClient that reports a non-loopback address (to exercise 404)."""
    app = _make_app()
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("10.0.0.1", 54321),
    ) as test_client:
        yield test_client


def test_config_get_is_reachable_without_loopback(external_client):
    # GET /config is intentionally unauthenticated (the dashboard Settings
    # menu reads it before knowing anything about the caller).
    resp = external_client.get("/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "pricing" in body and "settings" in body
    assert "config_path" not in body


@pytest.mark.parametrize(
    "path",
    [
        "/doctor",
        "/mcp/dashboards",
        "/mcp/usage",
        "/admin/deployments",
        "/learn/history",
        "/debug/memory/sync",
        "/stats/active_agents",
    ],
)
def test_dashboard_support_routes_require_loopback(external_client, path):
    resp = external_client.get(path)
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/doctor",
        "/mcp/dashboards",
        "/mcp/usage",
        "/admin/deployments",
        "/learn/history",
        "/debug/memory/sync",
        "/stats/active_agents",
    ],
)
def test_dashboard_support_routes_respond_over_loopback(client, path):
    resp = client.get(path)
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [-5, 0, 1, 100, 999])
def test_mcp_usage_limit_is_always_clamped_between_1_and_100(client, limit):
    # Regression test for the fix: limit was only capped on the high end,
    # so a negative value changed slicing semantics instead of erroring.
    resp = client.get("/mcp/usage", params={"limit": limit})
    assert resp.status_code == 200
