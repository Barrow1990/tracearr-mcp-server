"""Tests for the wired-together ASGI app: /health, /ready, and the bearer-auth
middleware — via server.build_app(), the same function __main__ uses."""

import httpx
from starlette.testclient import TestClient

import server


def test_health_does_not_call_tracearr(mock_tracearr, no_auth):
    def handler(request):
        raise AssertionError("/health must not call Tracearr")

    mock_tracearr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_success_uses_the_cheap_streams_summary(mock_tracearr, no_auth):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"summary": {"total": 0}})

    mock_tracearr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "reachable": True,
        "authenticated": True,
        "tracearr": {"url": server.TRACEARR_URL, "apiVersion": "v2"},
    }
    assert seen[0].url.path == "/api/v2/public/streams"
    assert dict(seen[0].url.params) == {"summary": "true"}


def test_ready_invalid_key(mock_tracearr, no_auth):
    mock_tracearr(lambda req: httpx.Response(401, json={"message": "Invalid API key"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is False
    assert "invalid or revoked" in body["error"]


def test_ready_key_without_owner_account(mock_tracearr, no_auth):
    mock_tracearr(lambda req: httpx.Response(403, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["authenticated"] is False
    assert "owner account" in body["error"]


def test_ready_old_tracearr_without_v2(mock_tracearr, no_auth):
    mock_tracearr(lambda req: httpx.Response(404, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert "2.0.0" in response.json()["error"]


def test_ready_rate_limited(mock_tracearr, no_auth):
    mock_tracearr(lambda req: httpx.Response(429, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert "rate limit" in response.json()["error"]


def test_ready_unreachable_host(mock_tracearr, no_auth):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    mock_tracearr(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is False
    assert body["authenticated"] is False


def test_ready_other_tracearr_error(mock_tracearr, no_auth):
    mock_tracearr(lambda req: httpx.Response(500, json={"message": "boom"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert "HTTP 500" in body["error"]


def test_no_auth_token_leaves_mcp_open(mock_tracearr, no_auth):
    mock_tracearr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    # No 401 — auth is off. The exact protocol response doesn't matter here,
    # only that the request wasn't rejected by the auth layer.
    assert response.status_code != 401


def test_auth_token_blocks_mcp_without_header(mock_tracearr, with_auth):
    mock_tracearr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/mcp", headers={"Accept": "application/json, text/event-stream"})

    assert response.status_code == 401


def test_auth_token_blocks_mcp_with_wrong_token(mock_tracearr, with_auth):
    mock_tracearr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer wrong-token"},
        )

    assert response.status_code == 401


def test_auth_token_allows_mcp_with_correct_token(mock_tracearr, with_auth):
    mock_tracearr(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {with_auth}"},
        )

    # Past auth — the normal MCP protocol response for a bare GET with no
    # prior session (400 missing-session), not a 401.
    assert response.status_code != 401


def test_auth_token_does_not_block_health_or_ready(mock_tracearr, with_auth):
    mock_tracearr(lambda req: httpx.Response(200, json={"summary": {}}))

    with TestClient(server.build_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
