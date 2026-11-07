# garmin-mcp

A local, self-written **FastMCP HTTP server** that exposes **Garmin Connect** health and activity data to an MCP client (Hermes) as seven read-only tools, reading the **official Garmin Connect Personal API** live through Garmin's first-party Python client. No mirror and no sync job in v1 (decision recorded in the design doc, Rev 8).

Part of the `maciej-makowski/mcp-servers` monorepo. Design doc: `garmin-connect-integration-design-final.md` (Rev 8) in the hermes-designs repo; live-probe evidence lives in that doc's evidence dir.

## What it does

- Serves MCP over streamable HTTP at `/mcp` on port **8016**, published on host loopback only (in-container bind `0.0.0.0`, the quadlet publishes `127.0.0.1:8016`). Bearer-gated and fail closed: HTTP mode requires `MCP_BEARER`.
- Reads Garmin live on each uncached tool call. No SQLite mirror and no nightly sync (that shape is the deferred add-on if rate limits bite, design decision D5).
- Takes date ranges on the daily-family tools: one call fetches up to a month. The server splits longer ranges into windows at the per-endpoint caps.
- Keeps a small in-process TTL cache in front of repeat reads, with per-family exponential backoff on 429/403/5xx.
- Never lets one family sink a multi-family query: `garmin_daily` reports per-family `present` flags and an `errors` map instead of aborting.
- Writes a JSONL audit record per tool call under `$MCP_LOGS_DIR/garmin/`: tool name, redacted args, result counts. Never the key and never raw payloads.

## How it works

Two credentials, two directions. Both live in `/home/mcp/env/garmin.env` (0600, filled by the account owner; never committed):

| Env var | Direction | Purpose |
|---|---|---|
| `MCP_BEARER` | Hermes -> server | gate for every tool call |
| `GARMIN_API_KEY` | server -> Garmin | exchanged for a 24h bearer token |
| `MCP_HOST` / `MCP_PORT` | config | bind address (container sets `0.0.0.0`) / port (default 8016) |
| `MCP_LOGS_DIR` | config | audit log root |

The key-to-token exchange and renewal are handled by the official client (`PersonalApiAuthenticator`): the key derives a 24h bearer token, cached 0600, re-derived automatically when under one hour of validity remains. No password, no SSO login, no refresh token exists on the box. Key expiry is chosen at creation (365 days here); rotation is an attended step in Garmin Connect web plus an env-file edit and a unit restart.

## MCP API (all seven tools read-only)

| Tool | Args | Returns |
|---|---|---|
| `garmin_recent_activities` | `limit` (1-100, default 20), `since` (YYYY-MM-DD, optional) | activity list |
| `garmin_activity` | `activity_id` | full activity detail |
| `garmin_activity_file` | `activity_id` | ZIP summary only: size, sha256, type. Never attaches bytes. |
| `garmin_daily` | `start_date`, `end_date`, `families` (comma list or `all`), `body_battery_chunks` (optional) | per-family chunks over the window, `present` flags, `errors` |
| `garmin_trend` | `family` (a ranged family), `weeks` (1-52, default 8) | weekly buckets (Monday-anchored) with the mean of numeric fields |
| `garmin_workout_usage` | `activity_id` | structured workouts attached to the activity |
| `garmin_api_status` | none | key/token health, per-family last errors, cache stats |

Daily families:

- Ranged, one API call per chunk: `stress`, `steps`, `hydration`, `weight`, `sleep`, `body_battery` (chunked), `daily` (summaries), `hrv`
- Per-day, one call per day, fanout capped at 92 days per request: `spo2`, `fitness_age`, `heart_rate`

Range caps in days per chunk (steps 28, sleep 31, others 30): secondhand values from a third-party implementation's README, named constants in `garmin_mcp/ranges.py`, verified against the real API during the trial.

## Underlying API

Garmin's official [Connect Personal API](https://developer.garmin.com/gc-developer-program/personal/) (opened October 2026): a self-serve key created by the account owner in Garmin Connect web, read-only and pull-only by design, revocable at any time. The server calls it through Garmin's first-party client [`garmin-connect-personal-api-python`](https://github.com/garmin/connect-personal-api-python) (PyPI, Apache-2.0, `requests` only, Python >= 3.12; import name `garmin_personal_api`). That client exposes 26 GET endpoints covering activity search/detail/download, daily stress, steps, hydration and summaries, weight, sleep, body battery, HRV, SpO2, fitness age, heart rate, and nutrition, plus a generic `get()` for any other Personal API GET.

Known limits, recorded honestly in the design doc:

- No training status or readiness endpoints, confirmed against all 26 published GETs. The unofficial libraries expose them but require a password session; that path stays designed as the fallback (option B) if the trial shows the gap hurts.
- The per-endpoint range caps (~28 days steps, ~31 days sleep) and the FIT field-stripping report are secondhand; both are verified in the phase-5 trial.
- Garmin documents no rate limits. The TTL cache, month-per-call range fetches and per-family backoff bound the request volume, and the trial watches actual behavior.

## Development

From `garmin/`:

```sh
uv run pytest        # all network and binary call sites mocked (house convention)
```

Run locally (stdio by default; HTTP only with a bearer set):

```sh
uv run python server.py
MCP_BEARER=x MCP_PORT=8016 uv run python server.py
```

Deployment (quadlet, install.sh and update.sh wiring, key-rotation runbook) ships in the deploy PR; the house pattern is documented in `deploy/README.md`.

---

Status: Phase 1 skeleton (the `garmin/` package, its container build and CI wiring). Deploy wiring lands in the deploy PR. Underlying design: `garmin-connect-integration-design-final.md` Rev 8, adopted Nov 7 2026.
