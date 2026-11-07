# strava-mcp

A local, self-written MCP server that wraps the official **Strava v3 REST API** and exposes **read-only** access to an MCP client (Hermes Agent) as three tools: `strava_athlete`, `strava_activities`, `strava_rate_status`. It serves streamable HTTP on host loopback 8016 and runs as the `mcp-strava` rootless-podman quadlet under the nologin `mcp` user, the same pattern as the other four servers in this repo.

No third-party connector is involved. Strava's official MCP connector (mcp.strava.com) only accepts Anthropic and Google clients: its dynamic client registration rejected every self-registration attempt (evidence record W20 in the design repo), and Strava's own FAQ names Claude and Gemini as the only supported assistants. The v3 REST API stays open to self-registered applications, so this wrapper talks to that API directly. Design and decision record: `ADOPTED/strava-design-final.md` in `maciej-makowski/hermes-designs` (Rev 8, adopted 2026-11-07).

## How it works

Every call takes one path through the same guard-and-send pipeline (`client.py`):

1. Hermes calls a tool over streamable HTTP at `/mcp`, carrying the `MCP_BEARER` header. The shared `require_bearer` middleware answers 401 to anything else, and the quadlet publishes the port on `127.0.0.1` only.
2. The tool layer validates parameters locally. An unknown action or a missing id fails as a structured validation error; no request reaches Strava.
3. The rate guard checks the two buckets (15 minutes and day) before any request. At or below its reserve it refuses with the wait, so no request is spent.
4. The client sends `GET https://www.strava.com/api/v3/...` with the stored access token as the Bearer credential. Every response's `X-RateLimit` and `X-ReadRateLimit` headers update the tracker, so the budgets follow Strava's real numbers.
5. A 401 triggers exactly one token refresh and one retry; a second 401 surfaces as an auth failure. A 429 becomes a structured `rate_limited` result carrying the wait-to-reset; the server never retries internally, the caller decides.
6. Any other upstream failure comes back as a structured dict with the upstream status, so Strava's own error fields stay visible to the agent.

### Token lifecycle

- The store lives at `/app/data/token.json` in the container. The quadlet mounts the mcp-owned host directory `/home/mcp/strava-token` there.
- Writes are atomic (temp file plus `os.replace`) and single-flight under `fcntl.flock`: Strava rotates the refresh token on every refresh and invalidates the previous one, so a mid-write crash must never destroy the store.
- An idle refresh fires when the access token is under one hour from expiry; access tokens rotate every six hours.
- Scopes: `read,profile:read_all,activity:read_all`. That covers profile, heart-rate zones, every activity including Only-You private ones, and streams. Adding `read_all` later costs one attended re-authorization.
- First authorization is the manual code-paste flow: the `strava_mcp.authorize` helper prints the authorization URL, exchanges the pasted code, writes the store, and prints success without echoing any token value. It is a plain code exchange with client id and client secret; the v3 application flow carries no PKCE.

### Audit

Every tool call goes through the house `log_call` decorator: redacted arguments, duration, outcome, upstream status. Per-tool JSONL files land under `/app/logs` in the container, which is the host directory `/home/mcp/mcp-logs/strava`.

## Tools and actions

| Tool | Actions | Underlying v3 call |
|---|---|---|
| `strava_athlete` | `get`, `zones`, `stats` | `GET /athlete`, `GET /athlete/zones`, `GET /athletes/{id}/stats` |
| `strava_activities` | `list`, `get`, `streams`, `zones`, `laps` | `GET /athlete/activities`, `GET /activities/{id}` plus `/streams`, `/zones`, `/laps` |
| `strava_rate_status` | `status` | none (reads the local tracker, makes no API call) |

Every call takes `{"action": ..., "params": {...}}`. All actions are reads; the server exposes no write endpoint of any kind.

### strava_athlete

- `get`: the authenticated athlete's full profile.
- `zones`: heart-rate and power zones.
- `stats`: rolling totals for an athlete id; the id defaults to the one stored with the token (the code-exchange payload carries the athlete object), so it usually needs no parameters.

### strava_activities

- `list`: the authenticated athlete's activities, newest first. Params: `after` and `before` (epoch seconds), `page` (default 1), `per_page` (default 30, clamped to 100), and `pages` (consecutive pages per call, default 1, clamped to 4, stops early at a short page so no request is wasted). Returns compact summary dicts (`id`, `name`, `sport_type`, `start_date`, `distance`, `moving_time`, `total_elevation_gain`, `average_speed`, `max_speed`, `private`) so one call does not spend the budget on full payloads.
- `get`: one activity's full detail; `include_all_efforts: true` adds segment efforts.
- `streams`: the raw measurement series for one activity. Requests the full key set (`time`, `latlng`, `distance`, `altitude`, `velocity_smooth`, `hearthrate`, `cadence`, `watts`, `temp`, `moving`, `grade_smooth`) with `key_by_type`. Series are downsampled by uniform stride to `max_points` (default 1,000); `raw: true` returns full resolution and warns past 50,000 total points.
- `zones`: the activity's heart-rate and power zone summaries.
- `laps`: the activity's lap splits.

### strava_rate_status

- `status`: the tracker's current view of both buckets (remaining reads and reset times) plus the configured reserves. Makes no API call and spends no budget.

### Error and rate shapes

- Guard refusal or upstream 429: `{"success": false, "rate_limited": true, "error": "...", "wait": {...}}`.
- Upstream HTTP error: `{"success": false, "status": <code>, "error": "strava api returned <code>: <body snippet>"}`.
- Local validation failure: `{"success": false, "validation": true, "error": "..."}`; no request was sent.

## The underlying API

- Strava REST API v3, base `https://www.strava.com/api/v3`. Reference: [developers.strava.com/docs/reference](https://developers.strava.com/docs/reference/). The endpoints used are exactly the ones in the table above; nothing else is called.
- OAuth 2.0: authorize at `https://www.strava.com/oauth/authorize`, exchange and refresh at `https://www.strava.com/oauth/token` (form-encoded, `grant_type` of `authorization_code` or `refresh_token`). The exchange response carries the athlete object, which supplies the `stats` default id.
- Rate limits: Strava documents 100 reads per 15 minutes and 1,000 per day, with exact remaining values in the `X-RateLimit` headers. The tracker reads those headers on every response and refuses at the reserves (10 of the 100-per-15-minute bucket, 100 of the 1,000-per-day bucket); a future bulk job would cap at 60 requests per 15-minute window.
- The v3 API also has write endpoints (kudos, comments, manual activity logging). They are deliberately not exposed here; they are future extension rows in the design doc.

## Deploy and registration

- Image: `ghcr.io/maciej-makowski/mcp-strava:latest` plus a per-commit sha tag (amd64 and arm64). Quadlet: `deploy/mcp-strava.container`, published on `127.0.0.1:8016` only.
- Env: copy `deploy/strava.env.example` to `/home/mcp/env/strava.env` (0600, owned by `mcp`) and fill `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` and `MCP_BEARER` (plus `MCP_HOST` and `MCP_PORT`). Secret values never enter this repo. Rotation and re-auth runbooks live in [`deploy/KEY-ROTATION.md`](../deploy/KEY-ROTATION.md).
- First authorization, once the container runs: the "Strava re-auth" runbook in `deploy/KEY-ROTATION.md` runs `uv run --no-sync python -m strava_mcp.authorize` inside the container via `podman exec`. Authorize in the browser, paste the code.
- Hermes registration: `mcp_servers.strava.url` of `http://127.0.0.1:8016/mcp`, an `Authorization` header carrying the same bearer value that Hermes's `.env` stores as `STRAVA_MCP_BEARER`, timeout 120. The full rollout ladder lives in the design doc.
- Quick check against a running container:

```sh
curl -s http://127.0.0.1:8016/mcp \
  -H "Authorization: Bearer $MCP_BEARER" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

## Development

```sh
uv run --extra dev pytest    # 76 tests; every upstream HTTP interaction is mocked, no network in tests
```
