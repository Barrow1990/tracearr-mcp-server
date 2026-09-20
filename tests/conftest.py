"""Shared pytest fixtures.

Sets a fake TRACEARR_URL/TRACEARR_API_KEY *before* importing server.py, since
the module requires both at import time (see server._require_env). Individual
tests then swap server.client's transport to control what "Tracearr" returns,
via httpx.MockTransport — no extra mocking library needed, it ships in httpx
(already a runtime dependency).
"""

import os

os.environ.setdefault("TRACEARR_URL", "http://test-tracearr:3000")
os.environ.setdefault("TRACEARR_API_KEY", "trr_pub_faketoken")

import httpx  # noqa: E402
import pytest  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def mock_tracearr(monkeypatch):
    """Point server.client at a fake Tracearr.

    Usage:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})
        mock_tracearr(handler)
    """

    def _install(handler):
        fake_client = httpx.Client(
            base_url=f"{server.TRACEARR_URL}/api/v2/public",
            headers={"Authorization": f"Bearer {server.TRACEARR_API_KEY}"},
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(server, "client", fake_client)
        return fake_client

    return _install


@pytest.fixture
def recorder(mock_tracearr):
    """A fake Tracearr that records every request and answers 200 {"data": []}."""

    class Recorder:
        requests: list[httpx.Request] = []

        @property
        def last(self) -> httpx.Request:
            return self.requests[-1]

    rec = Recorder()
    rec.requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return httpx.Response(200, json={"data": [], "meta": {"nextCursor": None, "pageSize": 25}})

    mock_tracearr(handler)
    return rec


@pytest.fixture
def no_auth(monkeypatch):
    """Run with MCP_AUTH_TOKEN unset (the default, open-server mode)."""
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", None)


@pytest.fixture
def with_auth(monkeypatch):
    """Run with a known MCP_AUTH_TOKEN, and return it."""
    token = "test-shared-secret"
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", token)
    return token
