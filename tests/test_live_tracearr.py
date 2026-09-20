"""Live contract tests against a *real* Tracearr instance.

These do not run by default — there's no Tracearr in CI, and we don't want to
fire real requests using the fake TRACEARR_URL/TRACEARR_API_KEY that conftest.py
sets for the rest of the suite. To run them:

    RUN_LIVE_TRACEARR_TESTS=1 TRACEARR_URL=http://192.168.1.50:3000 \\
    TRACEARR_API_KEY=trr_pub_<real key> pytest tests/test_live_tracearr.py -v

Point is to catch drift: if a Tracearr upgrade renames or removes a field these
tools rely on, these fail even though the mocked tests would still pass. They only
call read-only endpoints, like every tool in this server.
"""

import os

import httpx
import pytest

import server

RUN_LIVE = os.environ.get("RUN_LIVE_TRACEARR_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="opt-in only: set RUN_LIVE_TRACEARR_TESTS=1 with a real TRACEARR_URL/TRACEARR_API_KEY",
)


@pytest.fixture(scope="module")
def live_client():
    return httpx.Client(
        base_url=f"{server.TRACEARR_URL}/api/v2/public",
        headers={"Authorization": f"Bearer {server.TRACEARR_API_KEY}"},
        timeout=20,
    )


def test_key_is_accepted_and_streams_summary_has_totals(live_client):
    response = live_client.get("/streams", params={"summary": True})
    response.raise_for_status()
    assert "total" in response.json()["summary"]


def test_libraries_shape(live_client):
    response = live_client.get("/libraries")
    response.raise_for_status()
    assert isinstance(response.json()["data"], list)


def test_history_is_cursor_paginated(live_client):
    response = live_client.get("/history", params={"pageSize": 2})
    response.raise_for_status()
    body = response.json()
    assert isinstance(body["data"], list)
    assert "nextCursor" in body["meta"]


def test_users_carry_the_fields_the_tools_describe(live_client):
    response = live_client.get("/users", params={"pageSize": 1})
    response.raise_for_status()
    users = response.json()["data"]
    if not users:
        pytest.skip("no Tracearr identities yet")
    for field in ("id", "username", "email", "accounts"):
        assert field in users[0], field
