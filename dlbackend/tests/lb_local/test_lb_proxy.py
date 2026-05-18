"""Tests for the load balancer HTTP proxy.

Uses monkeypatching to mock the httpx client so no real servers are needed.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient


def _mock_response(request: httpx.Request) -> httpx.Response:
    """Mock backend that echoes request info."""
    # Extract backend identity from the URL host:port
    backend = f"{request.url.host}:{request.url.port}"
    return httpx.Response(
        200,
        json={
            "backend": backend,
            "path": str(request.url.path),
            "method": request.method,
            "api_key": request.headers.get("x-api-key", ""),
        },
    )


@pytest.fixture()
def lb_client(monkeypatch):
    """Create a TestClient for the LB with mocked backends."""
    from lbserver.utils import RoundRobin

    backends = ["http://gpu1:8001", "http://gpu2:8001"]
    monkeypatch.setattr("lbserver.app.BACKENDS", backends)
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "/_internal")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(backends))
    monkeypatch.setattr(
        "lbserver.app._client",
        httpx.AsyncClient(transport=httpx.MockTransport(_mock_response)),
    )

    from lbserver.app import app
    return TestClient(app)


class TestHTTPProxy:
    def test_round_robin_alternates(self, lb_client):
        resp1 = lb_client.get("/some/path")
        resp2 = lb_client.get("/some/path")
        assert resp1.status_code == 200
        assert resp2.status_code == 200

        b1 = resp1.json()["backend"]
        b2 = resp2.json()["backend"]
        assert b1 != b2

    def test_internal_prefix_prepended(self, lb_client):
        resp = lb_client.get("/lelamp/api/dl/health")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/_internal/lelamp/api/dl/health"

    def test_method_forwarded(self, lb_client):
        resp = lb_client.post("/api/dl/detect", content=b"test")
        assert resp.status_code == 200
        assert resp.json()["method"] == "POST"

    def test_headers_forwarded(self, lb_client):
        resp = lb_client.get("/test", headers={"X-API-Key": "secret123"})
        assert resp.status_code == 200
        assert resp.json()["api_key"] == "secret123"

    def test_full_cycle(self, lb_client):
        backends = []
        for _ in range(4):
            resp = lb_client.get("/ping")
            backends.append(resp.json()["backend"])
        assert backends[0] == backends[2]
        assert backends[1] == backends[3]
        assert backends[0] != backends[1]


class TestHTTPProxyErrors:
    def test_unreachable_backend_returns_502(self, monkeypatch):
        from lbserver.utils import RoundRobin

        monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(["http://127.0.0.1:19999"]))
        monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
        # Use real client — will fail to connect
        monkeypatch.setattr("lbserver.app._client", httpx.AsyncClient(timeout=2.0))

        from lbserver.app import app
        client = TestClient(app)
        resp = client.get("/anything")
        assert resp.status_code == 502
