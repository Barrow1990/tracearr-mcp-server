"""Unit tests for each MCP tool, against a mocked Tracearr.

`@mcp.tool()` returns the original function unchanged, so these call the tools
directly as plain Python functions — no MCP protocol/session machinery involved
here (that's covered separately in test_http.py).
"""

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server

UUID = "11111111-2222-3333-4444-555555555555"


def params(request: httpx.Request) -> dict:
    return dict(request.url.params)


# --- request shape: every tool hits the right path -------------------------


@pytest.mark.parametrize(
    "call,path",
    [
        (lambda: server.active_streams(), "/api/v2/public/streams"),
        (lambda: server.watch_history(), "/api/v2/public/history"),
        (lambda: server.get_media("movie:tmdb:584"), "/api/v2/public/media/movie:tmdb:584"),
        (lambda: server.media_children("show:tvdb:81189"), "/api/v2/public/media/show:tvdb:81189/children"),
        (lambda: server.media_stats(UUID), f"/api/v2/public/media/{UUID}/stats"),
        (lambda: server.media_watchers(UUID), f"/api/v2/public/media/{UUID}/watchers"),
        (lambda: server.media_history(UUID), f"/api/v2/public/media/{UUID}/history"),
        (lambda: server.list_users(), "/api/v2/public/users"),
        (lambda: server.get_user(UUID), f"/api/v2/public/users/{UUID}"),
        (lambda: server.user_stats(UUID), f"/api/v2/public/users/{UUID}/stats"),
        (lambda: server.user_history(UUID), f"/api/v2/public/users/{UUID}/history"),
        (lambda: server.recently_added(), "/api/v2/public/recently-added"),
        (lambda: server.list_libraries(), "/api/v2/public/libraries"),
        (lambda: server.watched_media("movie"), "/api/v2/public/watched-media"),
    ],
)
def test_each_tool_hits_the_right_path_with_the_bearer_key(recorder, call, path):
    call()

    assert recorder.last.method == "GET"
    assert recorder.last.url.path == path
    assert recorder.last.headers["authorization"] == f"Bearer {server.TRACEARR_API_KEY}"


def test_a_tool_returns_the_api_json_unchanged(mock_tracearr):
    body = {"data": [{"id": "x", "media_title": "Chernobyl"}], "meta": {"nextCursor": "abc", "pageSize": 25}}
    mock_tracearr(lambda req: httpx.Response(200, json=body))

    assert server.watch_history() == body


# --- parameters -------------------------------------------------------------


def test_unset_optional_arguments_are_not_sent(recorder):
    server.watch_history()
    server.list_users()
    server.recently_added()

    assert all(dict(r.url.params) == {} for r in recorder.requests)


def test_watch_history_passes_every_filter_and_renames_page_size(recorder):
    server.watch_history(
        user_id=UUID,
        server_id="s1",
        media_id="m1",
        rating_key="rk",
        imdb_id="tt1",
        tmdb_id=584,
        tvdb_id=81189,
        media_type="episode",
        watched=False,
        since="2026-09-01",
        until="2026-09-20T12:00:00Z",
        cursor="cur",
        page_size=50,
    )

    assert params(recorder.last) == {
        "user_id": UUID,
        "server_id": "s1",
        "media_id": "m1",
        "rating_key": "rk",
        "imdb_id": "tt1",
        "tmdb_id": "584",
        "tvdb_id": "81189",
        "media_type": "episode",
        "watched": "false",  # False must be sent, not dropped
        "since": "2026-09-01",
        "until": "2026-09-20T12:00:00Z",
        "cursor": "cur",
        "pageSize": "50",
    }


def test_watched_true_is_sent(recorder):
    server.watch_history(watched=True)
    assert params(recorder.last) == {"watched": "true"}


@pytest.mark.parametrize("asked,sent", [(500, "100"), (0, "1"), (-5, "1"), (100, "100"), (7, "7")])
def test_page_size_is_clamped_to_what_the_api_accepts(recorder, asked, sent):
    server.user_history(UUID, page_size=asked)
    assert params(recorder.last)["pageSize"] == sent


def test_watched_media_allows_a_much_larger_page(recorder):
    server.watched_media("movie", page_size=5000)
    assert params(recorder.last)["pageSize"] == "1000"


def test_watched_media_sends_its_required_and_optional_params(recorder):
    server.watched_media("show", user_id=UUID, server_id="s1", min_state="partial", cursor="c")
    assert params(recorder.last) == {
        "media_type": "show",
        "user_id": UUID,
        "server_id": "s1",
        "min_state": "partial",
        "cursor": "c",
    }


def test_active_streams_summary_only(recorder):
    server.active_streams(summary_only=True)
    assert params(recorder.last) == {"summary": "true"}


def test_include_removed_only_sent_when_true(recorder):
    server.list_users(include_removed=True)
    server.recently_added(include_removed=True, library_id="L", media_type="movie", server_id="s1")

    assert params(recorder.requests[0]) == {"include_removed": "true"}
    assert params(recorder.requests[1]) == {
        "include_removed": "true",
        "library_id": "L",
        "media_type": "movie",
        "server_id": "s1",
    }


def test_media_watchers_window_and_server(recorder):
    server.media_watchers("movie:imdb:tt0111161", window="last_7", server_id="s1")
    assert params(recorder.last) == {"window": "last_7", "server_id": "s1"}


# --- refs are safe path segments ---------------------------------------------


def test_provider_refs_keep_their_colons(recorder):
    server.get_media("show:tvdb:81189")
    assert recorder.last.url.raw_path.decode() == "/api/v2/public/media/show:tvdb:81189"


def test_a_ref_cannot_escape_its_path_segment(recorder):
    server.get_media("../users?x=1#frag")

    path = recorder.last.url.raw_path.decode()
    assert path.startswith("/api/v2/public/media/")
    assert "/../" not in path and "?" not in path.split("?")[0]
    assert params(recorder.last) == {}  # the injected query string was encoded, not sent


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_ref_is_rejected_before_any_request(recorder, bad):
    with pytest.raises(ToolError, match="ref is required"):
        server.get_media(bad)
    assert recorder.requests == []


# --- errors become readable tool errors ---------------------------------------


@pytest.mark.parametrize(
    "status,expect",
    [
        (401, "invalid, revoked or missing"),
        (403, "not associated with an owner account"),
        (404, "Not found"),
        (429, "shared across the whole v2 API"),
        (400, "since is after until"),
        (500, "HTTP 500"),
    ],
)
def test_http_errors_are_explained(mock_tracearr, status, expect):
    mock_tracearr(lambda req: httpx.Response(status, json={"message": "since is after until"}))

    with pytest.raises(ToolError, match=expect):
        server.watch_history()


def test_error_detail_is_read_from_common_shapes(mock_tracearr):
    for body in ({"error": "boom-a"}, {"detail": "boom-a"}, {"error": {"message": "boom-a"}}, {"message": "boom-a"}):
        mock_tracearr(lambda req, b=body: httpx.Response(418, json=b))
        with pytest.raises(ToolError, match="boom-a"):
            server.list_libraries()


def test_a_non_json_error_body_is_still_reported(mock_tracearr):
    mock_tracearr(lambda req: httpx.Response(502, text="<html>Bad gateway</html>"))
    with pytest.raises(ToolError, match="Bad gateway"):
        server.list_libraries()


def test_unreachable_tracearr_is_a_readable_error(mock_tracearr):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    mock_tracearr(handler)
    with pytest.raises(ToolError, match="Cannot reach Tracearr"):
        server.list_libraries()


# --- the tool set -----------------------------------------------------------


def test_every_public_tool_is_registered_and_read_only():
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    # automations_status is always listed; the rest of the automation group only when the check passed
    names = {t.name for t in tools} - {"automations_status"}

    assert len(names) == 14
    assert {"active_streams", "watch_history", "watched_media", "list_libraries"} <= names
    # nothing that could write: the API has no write endpoints, and no tool name suggests one
    assert not any(n.startswith(("create", "delete", "update", "set", "kill", "terminate")) for n in names)
