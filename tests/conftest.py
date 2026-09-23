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
os.environ.setdefault("TRACEARR_USERNAME", "mcp-owner")
os.environ.setdefault("TRACEARR_PASSWORD", "test-password")

import json  # noqa: E402

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


@pytest.fixture(autouse=True)
def _automations_start_disabled(monkeypatch):
    """Every test starts with the automation tools 'not checked', whatever ran before it."""
    monkeypatch.setattr(server, "automations_state", server.AutomationsState("disabled", "not checked in tests"))
    monkeypatch.setattr(server, "_session_token", None)
    yield
    # server.mcp is process-global: take back whatever tools a test registered on it
    with server._tools_lock:
        for name in list(server._registered_tools):
            server.mcp.remove_tool(name)
        server._registered_tools.clear()


@pytest.fixture
def automations_ready(monkeypatch, with_auth):
    """Automation tools enabled: owner check passed and MCP_AUTH_TOKEN set."""
    monkeypatch.setattr(server, "automations_state", server.AutomationsState("ready", None, "mcp-owner"))


class FakeTracearr:
    """A stand-in for Tracearr's login-session /api/v1 routes.

    Mimics what the server depends on: sign-in returns a signed bearer token in the
    `set-auth-token` header, every other route answers 401 without the current token,
    and validation failures come back as 400 with a `message`.
    """

    def __init__(self):
        self.automations: list[dict] = []
        self.requests: list[tuple[str, str, object]] = []  # (method, path, json body)
        self.logins = 0
        self.token: str | None = None
        self.role = "owner"
        self.login_status = 200
        self.list_status: int | None = None  # force GET /automations to answer this status
        self.always_unauthorized = False
        self.dry_run_status = 200
        self.dry_run_would_run = 2
        self._next_id = 1

    def add_automation(self, **overrides) -> dict:
        automation = {
            "id": f"00000000-0000-0000-0000-{self._next_id:012d}",
            "name": "Existing automation",
            "description": None,
            "kind": "policy",
            "severity": "warning",
            "isActive": False,
            "triggers": [{"id": "t1", "type": "stream.started", "enabled": True}],
            "conditions": {"groups": []},
            "actions": {"actions": [{"id": "a1", "type": "terminate"}]},
        }
        automation.update(overrides)
        self._next_id += 1
        self.automations.append(automation)
        return automation

    def expire_session(self):
        self.token = None

    def calls(self, method: str, path: str) -> list:
        return [body for (m, p, body) in self.requests if (m, p) == (method, path)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))

        if path == "/auth/sign-in/username":
            self.logins += 1
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"message": "nope"})
            self.token = f"session-{self.logins}"
            return httpx.Response(
                200,
                json={"token": "raw", "user": {"username": "mcp-owner", "role": self.role}},
                headers={"set-auth-token": self.token},
            )

        if self.always_unauthorized or self.token is None or request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Invalid or expired token"})

        if path == "/automations" and request.method == "GET":
            if self.list_status:
                return httpx.Response(self.list_status, json={"message": "forced"})
            return httpx.Response(
                200, json={"data": self.automations, "meta": {"page": 1, "pageSize": 25, "total": len(self.automations)}}
            )
        if path == "/automations/dry-run":
            if self.dry_run_status != 200:
                return httpx.Response(self.dry_run_status, json={"message": "triggers: At least one enabled trigger is required"})
            samples = [
                {"subject": {"user": {"id": "u", "name": f"user{n}"}}, "wouldRun": n < self.dry_run_would_run, "summary": "s"}
                for n in range(3)
            ]
            return httpx.Response(200, json={"samples": samples})
        if path == "/automations" and request.method == "POST":
            return httpx.Response(200, json=self.add_automation(**body))

        parts = path.split("/")  # ["", "automations", "<id>", ...]
        if len(parts) >= 3 and parts[1] == "automations":
            found = next((a for a in self.automations if a["id"] == parts[2]), None)
            if found is None:
                return httpx.Response(404, json={"message": "Automation not found"})
            if len(parts) == 3:
                if request.method == "GET":
                    return httpx.Response(200, json=found)
                if request.method == "PATCH":
                    found.update(body)
                    return httpx.Response(200, json=found)
                if request.method == "DELETE":
                    self.automations.remove(found)
                    return httpx.Response(200, json={"success": True})
            if len(parts) == 4 and parts[3] in ("runs", "evaluations", "export") and request.method == "GET":
                return httpx.Response(200, json={"data": [], "meta": {"page": 1, "pageSize": 25, "total": 0}})
        return httpx.Response(404, json={"message": "no such route"})


@pytest.fixture
def fake_tracearr(monkeypatch):
    """Point server.session at a FakeTracearr with a clean cookie jar; returns the fake."""
    fake = FakeTracearr()
    fake_session = httpx.Client(base_url=f"{server.TRACEARR_URL}/api/v1", transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(server, "session", fake_session)
    return fake
