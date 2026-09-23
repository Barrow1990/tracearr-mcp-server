"""
Tracearr MCP Server

Exposes Tracearr's Public API v2 as MCP tools, so an MCP-compatible AI assistant
(e.g. Claude Code / Claude Desktop) can see who is watching what right now, browse
watch history, look up per-title and per-user play statistics, see what was recently
added, and check library sizes.

The Public API v2 is read-only, so every tool built on it is too. Automations (Tracearr's
rules) are not on that API: they live on Tracearr's internal API, which needs a login
session rather than the API key. Those tools exist only when TRACEARR_USERNAME and
TRACEARR_PASSWORD (a dedicated owner account) and MCP_AUTH_TOKEN are all set; see the
automations section below and the README. Everything that can change or enable a rule is
gated by a human approval prompt.

Configuration is via environment variables:
  TRACEARR_URL        e.g. http://192.168.1.50:3000 (required)
  TRACEARR_API_KEY    Tracearr > Settings > General > API key, format
                      `trr_pub_<token>` (required). It must belong to an owner
                      account, otherwise Tracearr answers 403.
  TRACEARR_USERNAME   optional: a dedicated Tracearr OWNER account for the automations tools
  TRACEARR_PASSWORD   optional: its password
  MCP_HOST            interface to bind to (default 0.0.0.0)
  MCP_PORT            port to listen on (default 8942)
  MCP_AUTH_TOKEN      shared secret required as `Authorization: Bearer <token>`
                      on every request (optional — if unset, the server is open
                      to anyone who can reach it; see README for why that matters
                      more than usual here: this API returns people's viewing
                      history and email addresses)

Tracearr's Public API v2 needs Tracearr 2.0.0 or later (earlier versions serve v1
only). It lives at a fixed `/api/v2/public` prefix, so there is no API-version
setting to configure. Auth is a Bearer API key (`trr_pub_...`). Tracearr rate-limits
one budget per key across the whole v2 surface, so a 429 is reported as such rather
than as a generic failure.

Transport: streamable-http. This runs as a standing network service (bind
0.0.0.0 inside the container; publish the port only on your internal
network/VLAN — never forward it externally) rather than being spawned
per-client over stdio, so any MCP client on the LAN can connect to
http://<host>:<port>/mcp.

Auth here is a single shared bearer token checked by plain middleware, not
the SDK's built-in OAuth support (mcp.server.auth) — that machinery expects
a full OAuth authorization server (issuer/resource metadata, RFC 8414/8707/
9068 discovery), which is unwarranted complexity for a single internal
secret shared by trusted LAN clients.
"""

import asyncio
import hmac
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import uvicorn
from mcp.server.mcpserver import Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


TRACEARR_URL = _require_env("TRACEARR_URL").rstrip("/")
TRACEARR_API_KEY = _require_env("TRACEARR_API_KEY")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8942"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")
TRACEARR_USERNAME = os.environ.get("TRACEARR_USERNAME")
TRACEARR_PASSWORD = os.environ.get("TRACEARR_PASSWORD")

# How long startup waits for the first automations check, and how often an unreachable
# Tracearr is re-checked afterwards.
STARTUP_CHECK_WAIT_SECONDS = 10
AUTOMATIONS_RECHECK_SECONDS = 300

# Page-size ceilings the API documents: 100 for most cursor-paginated endpoints,
# 1000 for watched-media (its rows are far smaller).
MAX_PAGE_SIZE = 100
MAX_WATCHED_MEDIA_PAGE_SIZE = 1000

INSTRUCTIONS = (
    "Read-only tools for Tracearr, which tracks who watches what on the media server. The data is "
    "personal (viewing history, usernames, email addresses): share only what the user asked for. "
    "Paginated tools return meta.nextCursor; pass it back as `cursor` for the next page (null means "
    "no more). A media `ref` is a canonical media UUID or a provider ref like movie:tmdb:584 or "
    "show:tvdb:81189; seasons have no provider ref, so get their UUID from media_children of the show. "
    "Call automations_status if you are unsure whether the automation (rules) tools are available: it says "
    "why they are missing. Automations you create are always saved inactive; enabling one, changing an "
    "active one and deleting one ask the user for approval, and you must never try to work around a "
    "declined approval."
)

client = httpx.Client(
    base_url=f"{TRACEARR_URL}/api/v2/public",
    headers={"Authorization": f"Bearer {TRACEARR_API_KEY}"},
    timeout=30,
)

# Automation tools: Tracearr's internal /api/v1 routes, authenticated by a login session.
# The sign-in response carries a Bearer token (`set-auth-token`) that is sent on every call;
# httpx also keeps the session cookie in its jar as a fallback. Deliberately no static header.
session = httpx.Client(base_url=f"{TRACEARR_URL}/api/v1", timeout=30)
_session_lock = threading.Lock()
_session_token: str | None = None

mcp = MCPServer("tracearr", instructions=INSTRUCTIONS)


def _error_detail(response: httpx.Response) -> str:
    """Tracearr's own explanation for an error response, whatever shape it uses."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or "no detail given"
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if value:
                return str(value)[:300]
    return str(body)[:300]


def _get(path: str, **params: Any) -> Any:
    """GET a Public API v2 path and return its JSON.

    Parameters whose value is None are dropped, so tools can pass every optional
    argument straight through. Failures become readable tool errors.
    """
    query = {name: value for name, value in params.items() if value is not None}
    try:
        response = client.get(path, params=query)
    except httpx.RequestError as error:
        raise ToolError(f"Cannot reach Tracearr at {TRACEARR_URL}: {error}") from error

    status = response.status_code
    if status == 401:
        raise ToolError("Tracearr rejected the API key (HTTP 401): it is invalid, revoked or missing.")
    if status == 403:
        raise ToolError(
            "Tracearr refused the request (HTTP 403): the API key is not associated with an owner account."
        )
    if status == 404:
        raise ToolError(f"Not found in Tracearr (HTTP 404): {_error_detail(response)}")
    if status == 429:
        raise ToolError(
            "Tracearr's rate limit was hit (HTTP 429). Its budget is shared across the whole v2 API for this "
            "key, so wait a little before trying again."
        )
    if status >= 400:
        raise ToolError(f"Tracearr returned HTTP {status}: {_error_detail(response)}")
    return response.json()


def _ref(ref: str) -> str:
    """A media ref as a safe URL path segment (keeps the `:` of provider refs)."""
    if not ref or not ref.strip():
        raise ToolError("ref is required: a media UUID or a provider ref like movie:tmdb:584")
    return quote(ref.strip(), safe=":")


def _page_size(page_size: int | None, cap: int = MAX_PAGE_SIZE) -> int | None:
    """Clamp to what the API accepts, so an over-large request still returns a page."""
    return None if page_size is None else min(max(page_size, 1), cap)


@mcp.tool()
def active_streams(server_id: str | None = None, summary_only: bool = False) -> dict:
    """List the streams playing right now, each with user, title, player, and whether it is
    transcoding, direct streaming or direct playing.

    server_id: only this media server (UUID). summary_only: return just the totals (streams,
    transcodes, direct streams/plays, total bitrate, per-server breakdown) without the stream list.
    """
    return _get("/streams", server_id=server_id, summary=True if summary_only else None)


@mcp.tool()
def watch_history(
    user_id: str | None = None,
    server_id: str | None = None,
    media_id: str | None = None,
    rating_key: str | None = None,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    media_type: str | None = None,
    watched: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    page_size: int | None = None,
) -> dict:
    """Watch history as plays, newest first. One record per play (a resume chain); plays where no
    part reached 2 minutes are excluded.

    Filters (all optional): user_id (Tracearr identity UUID), server_id, media_id (canonical media
    UUID; a show id matches all its episodes), rating_key, imdb_id, tmdb_id, tvdb_id, media_type
    (movie, episode, track, live, photo, unknown), watched (true/false: past the completion
    threshold, 85% by default), since / until (a date like 2026-09-01, or a full ISO datetime;
    until must not be before since).
    Paging: default 25 per page, max 100; pass meta.nextCursor back as `cursor` for the next page.
    """
    return _get(
        "/history",
        user_id=user_id,
        server_id=server_id,
        media_id=media_id,
        rating_key=rating_key,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        media_type=media_type,
        watched=watched,
        since=since,
        until=until,
        cursor=cursor,
        pageSize=_page_size(page_size),
    )


@mcp.tool()
def get_media(ref: str) -> dict:
    """Resolve a media ref to its canonical identity, the ids merged into it, and per-server
    availability (including removed copies). Shows also carry season and episode counts.

    ref: a canonical media UUID, or {movie|show|episode}:{imdb|tmdb|tvdb}:{id} such as
    movie:tmdb:584 or show:tvdb:81189. Seasons have no provider ref (see media_children).
    """
    return _get(f"/media/{_ref(ref)}")


@mcp.tool()
def media_children(ref: str) -> dict:
    """List a show's seasons (with episode counts) or a season's episodes. To reach a season, go
    show ref -> media_children -> season UUID -> media_children. Movies and episodes have no
    children (404). ref as in get_media."""
    return _get(f"/media/{_ref(ref)}/children")


@mcp.tool()
def media_stats(ref: str) -> dict:
    """Play counts, watch time and distinct viewers for a title across all_time, last_30 and
    last_7 UTC-day windows, each with a total and a per-server breakdown. Shows roll up their
    episodes. Cached by Tracearr for 60 seconds. ref as in get_media."""
    return _get(f"/media/{_ref(ref)}/stats")


@mcp.tool()
def media_watchers(ref: str, window: str = "all_time", server_id: str | None = None) -> dict:
    """Who watched a title: one entry per server account, ordered by watch time.

    window: all_time (default), last_30 or last_7 (UTC calendar days). server_id: only this server.
    ref as in get_media.
    """
    return _get(f"/media/{_ref(ref)}/watchers", window=window, server_id=server_id)


@mcp.tool()
def media_history(ref: str, cursor: str | None = None, page_size: int | None = None) -> dict:
    """Watch history for one title, newest first, one record per play. A show includes every
    episode and a season its episodes. Default 25 per page, max 100; pass meta.nextCursor back as
    `cursor`. ref as in get_media."""
    return _get(f"/media/{_ref(ref)}/history", cursor=cursor, pageSize=_page_size(page_size))


@mcp.tool()
def list_users(
    include_removed: bool = False, cursor: str | None = None, page_size: int | None = None
) -> dict:
    """List Tracearr identities, newest first, each with the media-server accounts it owns.
    Includes the email held on the identity (null when unset) and external_user_id (the media
    server's own user id, the stable key to correlate on).

    include_removed: also list identities whose every account has been removed.
    Default 25 per page, max 100; pass meta.nextCursor back as `cursor`.
    """
    return _get(
        "/users",
        include_removed=True if include_removed else None,
        cursor=cursor,
        pageSize=_page_size(page_size),
    )


@mcp.tool()
def get_user(user_id: str) -> dict:
    """One Tracearr identity by its id (UUID), with the accounts it owns."""
    return _get(f"/users/{_ref(user_id)}")


@mcp.tool()
def user_stats(user_id: str) -> dict:
    """Plays and watch time for an identity, summed across all its accounts, over all_time,
    last_30 and last_7 UTC-day windows, plus its top genres by play count. Cached 60 seconds."""
    return _get(f"/users/{_ref(user_id)}/stats")


@mcp.tool()
def user_history(user_id: str, cursor: str | None = None, page_size: int | None = None) -> dict:
    """Watch history for an identity across every account it owns, newest first, one record per
    play. Default 25 per page, max 100; pass meta.nextCursor back as `cursor`."""
    return _get(f"/users/{_ref(user_id)}/history", cursor=cursor, pageSize=_page_size(page_size))


@mcp.tool()
def recently_added(
    server_id: str | None = None,
    library_id: str | None = None,
    media_type: str | None = None,
    include_removed: bool = False,
    cursor: str | None = None,
    page_size: int | None = None,
) -> dict:
    """Library items ordered by the server-reported added date, newest first, each with its media
    identity.

    Filters (optional): server_id, library_id (a server's library id), media_type (movie, episode,
    season, show, artist, album, track, photo). include_removed also returns items since removed
    from the server. Default 25 per page, max 100; pass meta.nextCursor back as `cursor`.
    """
    return _get(
        "/recently-added",
        server_id=server_id,
        library_id=library_id,
        media_type=media_type,
        include_removed=True if include_removed else None,
        cursor=cursor,
        pageSize=_page_size(page_size),
    )


@mcp.tool()
def list_libraries() -> dict:
    """Per-library rollups for each server: item, movie, episode, show and track counts, total
    file size and per-resolution counts. Removed items are excluded. Cached 60 seconds."""
    return _get("/libraries")


@mcp.tool()
def watched_media(
    media_type: str,
    user_id: str | None = None,
    server_id: str | None = None,
    min_state: str | None = None,
    cursor: str | None = None,
    page_size: int | None = None,
) -> dict:
    """The distinct set of media with recorded watching, newest activity first, for matching
    against an external library by tmdb/tvdb/imdb id. Absence means unwatched.

    media_type (required): movie, show or episode. A movie/episode counts once a play passed the
    completion threshold (85% by default); a show once every episode present on the server has.
    user_id: scope to one identity (omit for the whole install). min_state: watched (default) or
    partial (also titles started but not finished).
    Default 100 per page, max 1000; pass meta.nextCursor back as `cursor`.
    """
    return _get(
        "/watched-media",
        media_type=media_type,
        user_id=user_id,
        server_id=server_id,
        min_state=min_state,
        cursor=cursor,
        pageSize=_page_size(page_size, MAX_WATCHED_MEDIA_PAGE_SIZE),
    )


# ---------------------------------------------------------------------------
# Automations (Tracearr's rules): availability, login, and the startup check
# ---------------------------------------------------------------------------
#
# Tracearr calls its rules "automations". They are not on the Public API: they live on
# the internal /api/v1/automations routes, which accept a login session only (the API
# key is rejected). Reads work for any logged-in user; create/update/delete need an
# owner account, so the tools are only offered for an owner.


@dataclass(frozen=True)
class AutomationsState:
    """Whether the automation tools are usable, and if not, why.

    status: "disabled" (won't change until restart with fixed settings),
            "pending" (Tracearr was unreachable; being re-checked), or "ready".
    """

    status: str
    reason: str | None = None
    account: str | None = None


automations_state = AutomationsState("disabled", "not checked yet")


class LoginError(Exception):
    """Login to Tracearr failed. `transient` means retrying later can help."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def _automations_config_problem() -> str | None:
    """Why the automation tools can't be enabled from configuration alone, or None."""
    if not (TRACEARR_USERNAME and TRACEARR_PASSWORD):
        return "TRACEARR_USERNAME and TRACEARR_PASSWORD are not set"
    if not MCP_AUTH_TOKEN:
        return (
            "MCP_AUTH_TOKEN is not set. The automation tools hold an owner login to Tracearr, "
            "so they only run when every MCP request must carry the shared secret"
        )
    return None


def _login() -> dict[str, Any]:
    """Sign in and remember the session token. Hold `_session_lock`.

    Returns the signed-in user's info (including `role`). Raises LoginError.
    """
    global _session_token
    _session_token = None
    session.cookies.clear()
    try:
        response = session.post(
            "/auth/sign-in/username", json={"username": TRACEARR_USERNAME, "password": TRACEARR_PASSWORD}
        )
    except httpx.RequestError as error:
        raise LoginError(f"cannot reach Tracearr at {TRACEARR_URL}: {error}", transient=True) from error

    status_code = response.status_code
    if status_code == 200:
        token = response.headers.get("set-auth-token")
        if not token and not len(session.cookies):
            raise LoginError(
                "login succeeded but Tracearr sent neither a bearer token nor a session cookie: "
                "a proxy may be stripping headers"
            )
        _session_token = token
        try:
            body = response.json()
        except ValueError:
            body = None
        return body.get("user", {}) if isinstance(body, dict) else {}
    if status_code == 401:
        raise LoginError("Tracearr rejected the username/password (HTTP 401)")
    if status_code == 403:
        raise LoginError(f"Tracearr refused the login (HTTP 403): {_error_detail(response)}")
    if status_code == 422:
        raise LoginError(f"Tracearr rejected the username/password format (HTTP 422): {_error_detail(response)}")
    if status_code == 429:
        raise LoginError("Tracearr's login rate limit was hit (HTTP 429)", transient=True)
    if status_code >= 500:
        raise LoginError(f"Tracearr returned HTTP {status_code} on login", transient=True)
    raise LoginError(f"unexpected HTTP {status_code} from Tracearr on login: {_error_detail(response)}")


def _session_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Send a request as the logged-in account, signing in first / again as needed.

    A 401 means the session expired or was revoked: sign in once more and retry once.
    Never loops, because Tracearr rate-limits sign-ins. Raises LoginError or
    httpx.RequestError; callers turn those into messages.
    """
    if _session_token is None and not len(session.cookies):
        with _session_lock:
            if _session_token is None and not len(session.cookies):
                _login()

    sent_token = _session_token
    headers = {"Authorization": f"Bearer {sent_token}"} if sent_token else {}
    response = session.request(method, path, headers=headers, **kwargs)
    if response.status_code != 401:
        return response

    with _session_lock:
        # If another thread already signed in again since this request was sent, don't
        # burn another sign-in.
        if _session_token == sent_token:
            _login()
    headers = {"Authorization": f"Bearer {_session_token}"} if _session_token else {}
    response = session.request(method, path, headers=headers, **kwargs)
    if response.status_code == 401:
        raise LoginError("Tracearr still rejects the session right after signing in again")
    return response


def check_automations_access() -> AutomationsState:
    """Sign in and confirm this account can manage automations; record the outcome.

    Called once at startup, and again on a cooldown while Tracearr is unreachable.
    """
    global automations_state

    problem = _automations_config_problem()
    if problem:
        automations_state = AutomationsState("disabled", problem)
        return automations_state

    try:
        with _session_lock:
            user = _login()
        account = user.get("username") or user.get("name") or TRACEARR_USERNAME
        role = user.get("role")
        if role != "owner":
            automations_state = AutomationsState(
                "disabled",
                f"account '{account}' has role '{role}', but the automation tools need an owner account",
                account,
            )
            return automations_state

        response = _session_request("GET", "/automations", params={"pageSize": 1})
        if response.status_code == 200:
            automations_state = AutomationsState("ready", None, account)
        elif response.status_code == 403:
            automations_state = AutomationsState(
                "disabled", f"account '{account}' is not allowed to read automations (HTTP 403)", account
            )
        elif response.status_code == 404:
            automations_state = AutomationsState(
                "disabled", "Tracearr has no /api/v1/automations route (unsupported Tracearr version?)", account
            )
        elif response.status_code >= 500:
            automations_state = AutomationsState(
                "pending", f"Tracearr returned HTTP {response.status_code} for /automations", account
            )
        else:
            automations_state = AutomationsState(
                "disabled", f"unexpected HTTP {response.status_code} from /automations", account
            )
    except LoginError as error:
        automations_state = AutomationsState("pending" if error.transient else "disabled", str(error))
    except httpx.RequestError as error:
        automations_state = AutomationsState("pending", f"cannot reach Tracearr at {TRACEARR_URL}: {error}")
    return automations_state


def _automations_report() -> dict[str, Any]:
    state = automations_state
    return {
        "enabled": state.status == "ready",
        "status": state.status,
        "reason": state.reason,
        "account": state.account,
    }


# ---------------------------------------------------------------------------
# Automation tools (Tracearr /api/v1/automations, owner login session)
# ---------------------------------------------------------------------------


class Approval(BaseModel):
    """The approval prompt shown to the human. Unticked (the default) means reject."""

    approve: bool = Field(default=False, description="Tick to approve this change; leave unticked to reject it")


# Returned by an approval resolver when no prompt is needed; the tool body still
# runs the same `_is_approved` check, so there is one code path for both cases.
_NO_APPROVAL_NEEDED = Approval(approve=True)


def _is_approved(outcome: Any) -> bool:
    """True only for an accepted prompt whose box was ticked (or no prompt was needed).

    Anything else (declined, cancelled, unticked, or a missing outcome) is a refusal.
    """
    return (
        getattr(outcome, "action", None) == "accept"
        and getattr(getattr(outcome, "data", None), "approve", False) is True
    )


def _short(value: Any, limit: int = 400) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _automations_call(method: str, path: str, **kwargs: Any) -> Any:
    """Call an automations route and return its JSON, turning failures into readable tool errors."""
    try:
        response = _session_request(method, f"/automations{path}", **kwargs)
    except LoginError as error:
        raise ToolError(f"Cannot use Tracearr's automations: {error}") from error
    except httpx.RequestError as error:
        raise ToolError(f"Cannot reach Tracearr at {TRACEARR_URL}: {error}") from error

    status = response.status_code
    if status == 400:
        raise ToolError(f"Tracearr rejected the request as invalid (HTTP 400): {_error_detail(response)}")
    if status == 403:
        raise ToolError(
            f"Tracearr refused the request (HTTP 403): {_error_detail(response)}. "
            "Creating, changing and deleting automations needs an owner account."
        )
    if status == 404:
        raise ToolError(f"Not found in Tracearr (HTTP 404): {_error_detail(response)}")
    if status == 409:
        raise ToolError(f"Tracearr reports a conflict (HTTP 409): {_error_detail(response)}")
    if status == 429:
        raise ToolError("Tracearr's rate limit was hit (HTTP 429). Wait a little before trying again.")
    if status >= 400:
        raise ToolError(f"Tracearr returned HTTP {status}: {_error_detail(response)}")
    if status == 204 or not response.content:
        return None
    return response.json()


def _find_automation(automation_id: str) -> dict:
    return _automations_call("GET", f"/{quote(automation_id.strip(), safe='')}")


def _outline(automation: dict) -> str:
    """One-paragraph description of what an automation does, for approval prompts."""
    triggers = ", ".join(str(t.get("type")) for t in automation.get("triggers") or []) or "none"
    actions = automation.get("actions") or {}
    kinds = ", ".join(str(a.get("type")) for a in actions.get("actions") or []) or "none"
    return f"kind {automation.get('kind')}, triggers: {triggers}; actions: {kinds}"


def automations_status() -> dict:
    """Say whether the automation (rules) tools are available, and if not exactly why.

    Call this when unsure which tools exist. Reasons include: TRACEARR_USERNAME/TRACEARR_PASSWORD
    or MCP_AUTH_TOKEN not set on the server, the account not being an owner, or Tracearr being
    unreachable (re-checked every few minutes).
    """
    return _automations_report()


def list_automations(
    kind: str | None = None,
    enabled: bool | None = None,
    search: str | None = None,
    source: str | None = None,
    severity: str | None = None,
    server_id: str | None = None,
    trigger: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    """List Tracearr automations (its rules), with pagination.

    kind: policy (violations, may act on users) or notification. enabled: only active or inactive.
    source: builtin, import, local or own. severity: low/warning/high. trigger: a trigger group name.
    Returns {data, meta:{page,pageSize,total}}.
    """
    query = {
        "kind": kind,
        "enabled": None if enabled is None else str(enabled).lower(),
        "search": search,
        "source": source,
        "severity": severity,
        "serverId": server_id,
        "trigger": trigger,
        "page": page,
        "pageSize": _page_size(page_size),
    }
    return _automations_call("GET", "", params={k: v for k, v in query.items() if v is not None})


def get_automation(automation_id: str) -> dict:
    """One automation in full: triggers, conditions, actions, scope, severity, whether it is active."""
    return _find_automation(automation_id)


def automation_runs(
    automation_id: str, outcome: str | None = None, page: int | None = None, page_size: int | None = None
) -> dict:
    """The recorded runs of one automation, newest first. outcome: completed, stopped_by_condition or error."""
    query = {"outcome": outcome, "page": page, "pageSize": _page_size(page_size)}
    return _automations_call(
        "GET",
        f"/{quote(automation_id.strip(), safe='')}/runs",
        params={k: v for k, v in query.items() if v is not None},
    )


def automation_evaluations(automation_id: str) -> dict:
    """The capped ring of near-misses: sessions that matched a trigger but recorded no run, and why."""
    return _automations_call("GET", f"/{quote(automation_id.strip(), safe='')}/evaluations")


def export_automation(automation_id: str) -> Any:
    """An automation as a shareable template envelope, handy as a worked example of the definition shape."""
    return _automations_call("GET", f"/{quote(automation_id.strip(), safe='')}/export")


def _dry_run(definition: dict, session_id: str | None = None) -> dict:
    body: dict[str, Any] = {"definition": definition}
    if session_id:
        body["sample"] = {"sessionId": session_id}
    return _automations_call("POST", "/dry-run", json=body)


def _dry_run_summary(result: Any) -> dict:
    """A compact view of a dry-run: how many recent sessions it would act on, with a few examples."""
    samples = result.get("samples", []) if isinstance(result, dict) else []
    return {
        "samples_checked": len(samples),
        "would_run": sum(1 for sample in samples if sample.get("wouldRun")),
        "examples": [
            {
                "user": (sample.get("subject") or {}).get("user", {}).get("name"),
                "would_run": sample.get("wouldRun"),
                "summary": _short(sample.get("summary"), 300),
            }
            for sample in samples[:5]
        ],
    }


def dry_run_automation(definition: dict, session_id: str | None = None) -> dict:
    """Test an automation definition against recent sessions without saving anything.

    `definition` has the create_automation shape. Optionally pin it to one session with session_id.
    Returns Tracearr's own samples (which conditions passed, which actions would run) plus a summary.
    """
    result = _dry_run(definition, session_id)
    return {"summary": _dry_run_summary(result), "result": result}


def create_automation(definition: dict) -> dict:
    """Create an automation from a full definition. It is ALWAYS saved inactive, and a dry-run is done first.

    `definition` keys: name, description?, kind (policy|notification), severity (low|warning|high, or null
    for a notification), triggers [{...}], conditions {groups:[...]}, actions {actions:[...]}, and optional
    serverId/serverUserId/userId scope, cooldownMinutes, retentionDays. Look at export_automation of an
    existing one for the exact node shapes; Tracearr validates and explains any mistake. Tell the user the
    dry-run result; they review it and can then ask you to enable it (set_automation_active, which asks for
    their approval) or enable it in the Tracearr UI.
    """
    payload = {key: value for key, value in definition.items() if key != "isActive"}
    payload["isActive"] = False
    dry_run = _dry_run_summary(_dry_run(payload))
    created = _automations_call("POST", "", json=payload)
    return {"created": True, "active": False, "automation": created, "dry_run": dry_run}


def _approve_update(automation_id: str, changes: dict) -> Elicit[Approval] | Approval:
    """Ask the human before changing an automation that is currently ACTIVE; inactive ones need no prompt.

    Resolvers can re-run between prompt rounds, so this only does idempotent reads.
    """
    automation = _find_automation(automation_id)
    if not automation.get("isActive"):
        return _NO_APPROVAL_NEEDED
    lines = "\n".join(
        f"- {field}: {_short(automation.get(field), 200)} -> {_short(value)}" for field, value in sorted(changes.items())
    )
    return Elicit(
        f"Update automation '{automation.get('name')}' ({automation.get('id')}), which is currently ACTIVE "
        f"({_outline(automation)}), so the change affects live behaviour.\n\nChanges:\n{lines}\n\nApprove this change?",
        Approval,
    )


def update_automation(
    automation_id: str,
    changes: dict,
    approval: Annotated[ElicitationResult[Approval], Resolve(_approve_update)] = None,  # type: ignore[assignment]
) -> dict:
    """Change fields of an existing automation; fields left out of `changes` are unchanged.

    `changes` may hold any create_automation field (name, description, severity, triggers, conditions,
    actions, cooldownMinutes, ...) except isActive: use set_automation_active for that. If the automation
    is currently ACTIVE the user is asked to approve first (a prompt in their client); inactive ones
    update without a prompt. If they decline, nothing changes: tell them and don't retry. Automations
    installed from a template refuse changes to their definition until detached, in the Tracearr UI.
    """
    if "isActive" in changes:
        raise ToolError("Use set_automation_active to enable or disable an automation, not update_automation.")
    if not _is_approved(approval):
        return {
            "updated": False,
            "reason": "The user declined the change (or the client could not show the approval prompt). Nothing was changed.",
        }
    return {
        "updated": True,
        "automation": _automations_call("PATCH", f"/{quote(automation_id.strip(), safe='')}", json=changes),
    }


def _approve_active(automation_id: str, active: bool) -> Elicit[Approval] | Approval:
    """Ask the human before an automation is turned ON; turning one off is the safe direction."""
    automation = _find_automation(automation_id)
    if not active or automation.get("isActive"):
        return _NO_APPROVAL_NEEDED
    consequence = (
        "It is a POLICY: once active, it can act on users' streams automatically (for example terminate "
        "them or change trust)."
        if automation.get("kind") == "policy"
        else "Once active, it sends notifications when its triggers fire."
    )
    return Elicit(
        f"Enable automation '{automation.get('name')}' ({automation.get('id')})? {_outline(automation)}. "
        f"{consequence}\n\nApprove enabling it?",
        Approval,
    )


def set_automation_active(
    automation_id: str,
    active: bool,
    approval: Annotated[ElicitationResult[Approval], Resolve(_approve_active)] = None,  # type: ignore[assignment]
) -> dict:
    """Enable (active=true) or disable (active=false) an automation.

    Enabling always asks the user to approve (a prompt in their client): a policy automation can act on
    users' streams once live. Disabling needs no prompt. If they decline, nothing changes: tell them and
    don't retry. Run dry_run_automation or check get_automation first and tell the user what it will do.
    """
    if not _is_approved(approval):
        return {
            "updated": False,
            "reason": "The user declined enabling it (or the client could not show the approval prompt). It is unchanged.",
        }
    return {
        "updated": True,
        "automation": _automations_call("PATCH", f"/{quote(automation_id.strip(), safe='')}", json={"isActive": active}),
    }


def _approve_delete(automation_id: str) -> Elicit[Approval]:
    """Always ask the human before an automation is deleted."""
    automation = _find_automation(automation_id)
    state = "ACTIVE" if automation.get("isActive") else "inactive"
    return Elicit(
        f"Delete automation '{automation.get('name')}' ({automation.get('id')}, currently {state})? "
        "Its run history is deleted with it and this cannot be undone.",
        Approval,
    )


def delete_automation(
    automation_id: str,
    approval: Annotated[ElicitationResult[Approval], Resolve(_approve_delete)] = None,  # type: ignore[assignment]
) -> dict:
    """Permanently delete an automation and its run history. The user is always asked to approve first
    (a prompt in their client). If they decline, nothing is deleted: tell them and don't retry."""
    if not _is_approved(approval):
        return {
            "deleted": False,
            "reason": "The user declined the deletion (or the client could not show the approval prompt). Nothing was deleted.",
        }
    automation = _find_automation(automation_id)
    _automations_call("DELETE", f"/{quote(automation_id.strip(), safe='')}")
    return {"deleted": True, "automation": {"id": automation.get("id"), "name": automation.get("name")}}


# ---------------------------------------------------------------------------
# Tool registration: only what is actually usable is listed
# ---------------------------------------------------------------------------

AUTOMATION_TOOLS = (
    list_automations,
    get_automation,
    automation_runs,
    automation_evaluations,
    export_automation,
    dry_run_automation,
    create_automation,
    update_automation,
    set_automation_active,
    delete_automation,
)

_registered_tools: set[str] = set()
# sync_tools runs from the startup thread and from build_app, so serialise it.
_tools_lock = threading.Lock()


def sync_tools() -> None:
    """Make the listed automation tools match the configuration and the check outcome.

    `automations_status` is always listed so the AI can find out why the rest are missing.
    """
    with _tools_lock:
        wanted: dict[str, Any] = {automations_status.__name__: automations_status}
        if automations_state.status == "ready":
            wanted.update({tool.__name__: tool for tool in AUTOMATION_TOOLS})

        before = set(_registered_tools)
        for name in sorted(_registered_tools - wanted.keys()):
            mcp.remove_tool(name)
            _registered_tools.discard(name)
        for name, tool in wanted.items():
            if name not in _registered_tools:
                mcp.add_tool(tool)
                _registered_tools.add(name)
        if _registered_tools != before:
            _log_tools()


def _log_tools() -> None:
    """Log every listed tool, and why the automation group is missing if it is. Call with `_tools_lock` held."""
    automation = [tool.__name__ for tool in AUTOMATION_TOOLS if tool.__name__ in _registered_tools]
    line = ", ".join(automation) if automation else f"none — {automations_state.reason or automations_state.status}"
    hidden = {tool.__name__ for tool in AUTOMATION_TOOLS} | {automations_status.__name__}
    public = [name for name in _public_tool_names() if name not in hidden]
    print(
        f"Tools available ({len(public) + len(_registered_tools)}):\n"
        f"  public (read-only): {', '.join(public)}\n"
        f"  always            : {automations_status.__name__}\n"
        f"  automations       : {line}",
        file=sys.stderr,
    )


def _public_tool_names() -> list[str]:
    return [tool.name for tool in asyncio.run(mcp.list_tools())]


def _log_state(prefix: str) -> None:
    reason = f" — {automations_state.reason}" if automations_state.reason else ""
    print(f"{prefix}: {automations_state.status}{reason}", file=sys.stderr)


def _check_and_retry(first_check_done: threading.Event) -> None:
    """Background worker: the startup automations check, then retries while Tracearr is unreachable."""
    check_automations_access()
    sync_tools()
    _log_state("Automation tools")
    first_check_done.set()
    while automations_state.status == "pending":
        time.sleep(AUTOMATIONS_RECHECK_SECONDS)
        check_automations_access()
        sync_tools()
        _log_state("Automation tools re-check")


def initialize() -> None:
    """Start the automations check without letting it hold up the server.

    It runs in a background thread so a Tracearr that hangs can't stop the read-only
    tools coming up. Startup waits up to STARTUP_CHECK_WAIT_SECONDS for the first result,
    so normally the tools are already listed when the first client connects; otherwise
    the state is "pending" and they appear when the check finishes (on a client's next connect).
    """
    global automations_state
    automations_state = AutomationsState("pending", "startup check still running")
    first_check_done = threading.Event()
    threading.Thread(target=_check_and_retry, args=(first_check_done,), name="automations-check", daemon=True).start()
    if not first_check_done.wait(STARTUP_CHECK_WAIT_SECONDS):
        print(
            f"Automations check still running after {STARTUP_CHECK_WAIT_SECONDS}s; starting the server without waiting",
            file=sys.stderr,
        )
        sync_tools()


# Paths that must stay reachable without MCP_AUTH_TOKEN, so Docker's own
# HEALTHCHECK, Dockhand's health probe, etc. don't need the secret.
UNAUTHENTICATED_PATHS = {"/health", "/ready"}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness check: the process is up and serving HTTP. Does not call Tracearr."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> Response:
    """Readiness check: TRACEARR_URL is reachable and TRACEARR_API_KEY is accepted.

    Uses the streams summary, which is the cheapest authenticated call the API offers.
    """
    try:
        response = client.get("/streams", params={"summary": True}, timeout=5)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        if status_code == 401:
            reason = "invalid or revoked Tracearr API key"
        elif status_code == 403:
            reason = "Tracearr API key is not associated with an owner account"
        elif status_code == 404:
            reason = "Tracearr has no /api/v2/public API (needs Tracearr 2.0.0 or later)"
        elif status_code == 429:
            reason = "Tracearr rate limit hit"
        else:
            reason = f"Tracearr returned HTTP {status_code}"
        return JSONResponse(
            {"status": "error", "reachable": True, "authenticated": status_code not in (401, 403), "error": reason},
            status_code=503,
        )
    except httpx.RequestError as error:
        return JSONResponse(
            {
                "status": "error",
                "reachable": False,
                "authenticated": False,
                "error": f"cannot reach Tracearr at {TRACEARR_URL}: {error}",
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "status": "ok",
            "reachable": True,
            "authenticated": True,
            "tracearr": {"url": TRACEARR_URL, "apiVersion": "v2"},
            "automations": _automations_report(),
        }
    )


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <MCP_AUTH_TOKEN>` on every request except
    the health/readiness endpoints, which are meant to be publicly pollable."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in UNAUTHENTICATED_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, MCP_AUTH_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    """Build the ASGI app (routes + auth middleware). Split out from __main__ so
    tests can exercise the real, fully-wired app without going through uvicorn."""
    sync_tools()
    app = mcp.streamable_http_app(host=MCP_HOST)

    if MCP_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware)
        print("Auth enabled: Authorization: Bearer <token> required", file=sys.stderr)
    else:
        print("WARNING: MCP_AUTH_TOKEN not set — server is open to anyone who can reach it", file=sys.stderr)

    return app


if __name__ == "__main__":
    initialize()
    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT)
