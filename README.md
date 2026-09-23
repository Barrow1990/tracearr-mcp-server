# tracearr-mcp-server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that
connects to Tracearr (who watches
what on your Plex/Jellyfin/Emby servers: live streams, watch history, per-user and
per-title stats), packaged for Docker. **The 14 API-key tools are read-only** —
Tracearr's Public API v2 has no write endpoints. The **automation (rules) tools**
can manage Tracearr's rules, but only when you give the server an owner login and
`MCP_AUTH_TOKEN`; everything that can enable, change or delete a live rule asks you first
(see [Automations](#automations-rules)).

It runs as a standing network service (streamable-http transport, not stdio),
so any MCP client on your internal network can connect to
`http://<host>:<port>/mcp` — the container isn't spawned per-client, and
container lifecycle/updates can be handed off to a tool like
[Dockhand](https://dockhand.pro).

## API assumptions

Built against Tracearr's **Public API v2** (`/api/v2/public`, OpenAPI 3.0,
version 2.0.0), which needs **Tracearr 2.0.0 or later** — earlier versions serve
API v1 only and `/ready` will say so (HTTP 404). Auth is a Bearer API key
(`trr_pub_<token>`) from **Tracearr > Settings > General**. The key must belong to
an owner account, otherwise Tracearr answers 403.

There's no server-reported API version to negotiate (the `/api/v2/public` prefix is
fixed), so there is no version environment variable. Tracearr rate-limits **one
budget per key across the whole v2 surface**; a 429 is reported to the AI as
exactly that, with a hint to wait.

## Tools

| Tool | Description |
|---|---|
| `active_streams` | Streams playing right now (user, title, player, transcode/direct), or just the totals with `summary_only` |
| `watch_history` | Watch history as plays, newest first; filter by user, server, media, provider ids, type, watched state, date range |
| `get_media` | Resolve a media ref to its canonical identity, merged ids and per-server availability |
| `media_children` | A show's seasons, or a season's episodes |
| `media_stats` | Play counts, watch time and distinct viewers over all-time / 30 days / 7 days |
| `media_watchers` | Who watched a title, ordered by watch time |
| `media_history` | Watch history for one title |
| `list_users` / `get_user` | Tracearr identities with their media-server accounts (and email, when set) |
| `user_stats` | An identity's plays and watch time over the same windows, plus top genres |
| `user_history` | An identity's watch history across all its accounts |
| `recently_added` | Recently added library items, newest first |
| `list_libraries` | Per-library counts, sizes and resolutions |
| `watched_media` | The set of watched (or started) media, for matching against an external library by tmdb/tvdb/imdb id |

A media `ref` is a canonical media UUID or a provider ref such as `movie:tmdb:584`
or `show:tvdb:81189`. Seasons have no provider ref: go show → `media_children` →
season UUID → `media_children`. Paginated tools return `meta.nextCursor`; pass it
back as `cursor` for the next page. Page sizes are clamped to what the API accepts
(100, or 1000 for `watched_media`).

## Automations (rules)

Tracearr calls its rules **automations** (a `policy` acts on violations, a `notification`
sends alerts). They are **not** on the Public API: they live on Tracearr's internal
`/api/v1/automations` routes, which accept a login session and reject the `trr_pub_` key.
So these tools need a Tracearr **username and password**, and they stay hidden unless
everything below is in place. Call `automations_status` to see exactly what is missing.

Requirements (all checked at startup):

1. `TRACEARR_USERNAME` / `TRACEARR_PASSWORD` set, for a **dedicated Tracearr owner account**
   (create/update/delete need the owner role; the account is checked at startup and
   anything else disables the group). A separate account keeps this server's sessions
   and audit trail apart from yours; give it a long random password.
2. `MCP_AUTH_TOKEN` set: this server then holds an owner login, so every MCP request
   must carry the shared secret.
3. Tracearr 2.0.0 or later, with local login enabled.

The login is a normal sign-in (`POST /api/v1/auth/sign-in/username`); the session lasts 30
days, and the server signs in again on a 401 (once, single-flight, never in a loop). If
Tracearr is unreachable at startup the check is retried every 5 minutes.

| Tool | Description |
|---|---|
| `automations_status` | Always listed. Whether the group is available and, if not, why |
| `list_automations` / `get_automation` | Browse and read rules (filters: kind, enabled, search, source, severity, server, trigger) |
| `automation_runs` / `automation_evaluations` | What a rule has done, and its near-misses |
| `export_automation` | A rule as a template envelope, a worked example of the definition shape |
| `dry_run_automation` | Test a definition against recent sessions; saves nothing |
| `create_automation` | Always saved **inactive**, after a dry run whose result is returned |
| `update_automation` | Change fields; **asks you first when the rule is active** |
| `set_automation_active` | Enable or disable; **enabling always asks you first** (a policy can act on streams) |
| `delete_automation` | **Always asks you first**; run history is deleted with it |

**Human approval** is an MCP elicitation prompt in your client. It fails closed: if the
client can't show a prompt, or you decline, cancel or leave the box unticked, nothing changes.
Disabling a rule needs no prompt (the safe direction), and `update_automation` cannot flip
`isActive`.

**Not exposed:** bulk update/delete, template detach/upgrade, users, servers, settings,
notification destinations. The account is an owner, so this is a choice of this server,
not a limit of the login.

## Privacy

This API returns **people's viewing history, usernames and email addresses**. Set
`MCP_AUTH_TOKEN` (below) so only clients you've given the secret to can read it,
and never expose the port outside your network. The server's own instructions tell
the AI to share only what you asked for, but that is guidance to the AI, not a
control — the token and the network boundary are the controls.

## Health endpoints

Two plain HTTP endpoints, reachable without `MCP_AUTH_TOKEN` (so Docker's
`HEALTHCHECK`, Dockhand, or any other monitor can poll them without the secret):

| Endpoint | Checks | Healthy | Unhealthy |
|---|---|---|---|
| `GET /health` | The process is up and serving HTTP. Does **not** call Tracearr. | `200 {"status": "ok"}` | (doesn't respond) |
| `GET /ready` | `TRACEARR_URL` is reachable and `TRACEARR_API_KEY` is accepted (via the streams summary, the cheapest authenticated call). | `200 {"status": "ok", "reachable": true, "authenticated": true, "tracearr": {...}}` | `503 {"status": "error", "reachable": ..., "authenticated": ..., "error": "..."}` |

`/ready` also reports the automation tools' state (`automations`) but a problem there never turns it unhealthy, since the read-only tools still work.

`/ready` says which of these it was: invalid or revoked key (401), key without an
owner account (403), Tracearr too old to have the v2 API (404), rate limited (429),
or unreachable.

## Authentication

Set `MCP_AUTH_TOKEN` (a random shared secret — `openssl rand -hex 32`) and
every request must carry `Authorization: Bearer <token>` or the server
returns `401`. This is checked by a small Starlette middleware in front of
the MCP app, **not** the `mcp` SDK's built-in OAuth support
(`mcp.server.auth`) — that machinery expects a full OAuth authorization
server, which is unnecessary complexity for one secret shared by trusted LAN
clients. This is a separate secret from `TRACEARR_API_KEY` — the latter
authenticates *this server* to Tracearr, the former authenticates *MCP
clients* to this server.

Leave `MCP_AUTH_TOKEN` unset and the server runs with **no auth** — anything that
can reach `http://<host>:<port>/mcp` can read everything above. The server logs a
warning on startup when it's running this way. Either way, the trust boundary is
still the network:

- **Do not** publish this port through any reverse proxy, port-forward, or
  anything else reachable from outside your LAN/VLAN.
- Bind the compose `ports:` mapping to a specific internal interface (e.g.
  `192.168.1.50:8942:8942`) if you want to be stricter.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRACEARR_URL` | yes | — | e.g. `http://192.168.1.50:3000` |
| `TRACEARR_API_KEY` | yes | — | Tracearr > Settings > General (`trr_pub_...`), owner account |
| `MCP_HOST` | no | `0.0.0.0` | Interface the server binds to inside the container |
| `MCP_PORT` | no | `8942` | Port the server listens on |
| `TRACEARR_USERNAME` | no | — | Dedicated Tracearr **owner** account, enables the automation tools (see above) |
| `TRACEARR_PASSWORD` | no | — | Its password. Also needs `MCP_AUTH_TOKEN` |
| `MCP_AUTH_TOKEN` | no, but strongly recommended (required for automations) | — | Shared secret required as `Authorization: Bearer <token>`. Unset = no auth (see above) |

**Compose and `$`.** Docker Compose interpolates `$` in `.env` / `.env.dockhand`
values, so a secret containing `$` is silently truncated (`abc$Xy1def` becomes
`abc`). Write `$$` for a literal `$`, or single-quote the value. Generated
`trr_pub_...` keys and `openssl rand -hex` tokens don't contain `$`.

## Image

Built and pushed to `ghcr.io/barrow1990/tracearr-mcp-server` by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml), after the tests pass:

| Branch | Tags published |
|---|---|
| `main` | `:latest` and `:<commit-sha>` |
| `dev` | `:dev` and `:dev-<commit-sha>` only — never `:latest`, so production can't pick up an unmerged build |

`docker-compose.yml` pulls `:latest` by default; swap in `build: .` there
instead if you'd rather build locally from the `Dockerfile`. The image is a
three-stage build that ends on a `scratch` base, the same shape as the other MCP
servers in this stack.

## Running with Docker Compose

```bash
cp .env.example .env   # fill in TRACEARR_URL / TRACEARR_API_KEY / MCP_AUTH_TOKEN
docker compose up -d --pull always
```

The server is then reachable at `http://<docker-host>:8942/mcp` from anything
on your internal network.

## Managing with Dockhand

Point Dockhand at `ghcr.io/barrow1990/tracearr-mcp-server` and let it track
new tags. **Make the GHCR package public**, or every pull will need `docker
login ghcr.io` with a PAT on each deploy host. Set a restart policy of
`unless-stopped` (already in `docker-compose.yml`). The `.env` / `.env.dockhand`
precedence works as in the other MCP-server repos.

## Connecting a client

### Claude Code

```bash
claude mcp add tracearr -s user --transport http http://<docker-host>:8942/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```
(Drop the `--header` flag if you're running with `MCP_AUTH_TOKEN` unset.)

### Claude Desktop

Claude Desktop's built-in config expects a locally-spawned `command`, so for
a network server like this you'll need an HTTP-to-stdio bridge such as
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "tracearr": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "http://<docker-host>:8942/mcp",
        "--header", "Authorization: Bearer <MCP_AUTH_TOKEN>"
      ]
    }
  }
}
```

## Running without Docker

```bash
pip install -r requirements.txt
TRACEARR_URL=http://192.168.1.50:3000 TRACEARR_API_KEY=trr_pub_your_key \
MCP_AUTH_TOKEN=your-shared-secret python server.py
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

- `tests/test_tools.py` — every tool's request shape (path, parameters, dropped
  `None`s, page-size clamping, safe path segments) and error mapping, against a
  mocked Tracearr (`httpx.MockTransport`, no extra mocking library needed).
- `tests/test_http.py` — `/health`, `/ready`, and the bearer-auth middleware, via
  `server.build_app()` (the exact app `__main__` runs) through Starlette's
  `TestClient`.
- `tests/test_live_tracearr.py` — **opt-in** contract tests against a real
  Tracearr, to catch drift if an upgrade renames a field these tools rely on.
  Skipped by default; run with:
  ```bash
  RUN_LIVE_TRACEARR_TESTS=1 TRACEARR_URL=http://192.168.1.50:3000 \
  TRACEARR_API_KEY=<real trr_pub_... key> python -m pytest tests/test_live_tracearr.py -v
  ```

CI (`.github/workflows/ci.yml`) runs the mocked suite on every push/PR to `main`
and `dev`; the GHCR build only runs after it passes.

## Branch flow

`main` and `dev` are protected branches.

- **`dev`** is where changes land first. It can't be force-pushed or deleted. Every push
  to `dev` runs the tests and publishes `ghcr.io/barrow1990/tracearr-mcp-server:dev` (never `:latest`).
- **`main`** only changes through a pull request **from `dev`**. Direct pushes are
  blocked (for admins too), the `test` check must pass, and the `source-branch` check
  ([`enforce-dev-to-main.yml`](.github/workflows/enforce-dev-to-main.yml)) fails any
  pull request into `main` that comes from another branch or from a fork. Merging is
  what publishes `:latest`.
- Merge `dev` into `main` with a **merge commit**, not squash or rebase: squashing
  rewrites `dev`'s history, so `dev` and `main` diverge and every later pull
  request hits conflicts.

## License

MIT
