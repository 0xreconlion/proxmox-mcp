from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nodeops_mcp import __version__
from nodeops_mcp.server import build_http_app


@pytest.mark.anyio
async def test_health_is_minimal_and_does_not_query_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEOPS_MODE", "simulated")
    monkeypatch.setenv("NODEOPS_AUTH_MODE", "disabled-loopback")
    transport = ASGITransport(app=build_http_app(host="127.0.0.1"))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "nodeops-mcp",
        "version": __version__,
    }
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"status", "service", "version"}


def test_unauthenticated_http_remains_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEOPS_MODE", "simulated")
    monkeypatch.setenv("NODEOPS_AUTH_MODE", "disabled-loopback")
    with pytest.raises(ValueError, match="127.0.0.1"):
        build_http_app(host="0.0.0.0")
