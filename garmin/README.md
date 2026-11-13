# garmin-mcp

A local, self-written **FastMCP HTTP server** that exposes **Garmin Connect** health and activity data to an MCP client (Hermes) as seven read-only tools, reading the **official Garmin Connect Personal API** live through Garmin's first-party Python client. No mirror and no sync job in v1 (decision recorded in the design doc, Rev 8).

Part of the `maciej-makowski/mcp-servers` monorepo. Design doc: `garmin-connect-integration-design-final.md` (Rev 8) in the hermes-designs repo; live-probe evidence lives in that doc's evidence dir.

## What it does

- Serves MCP over streamable HTTP at `/mcp` on port **8017**, published on host loopback only (in-container bind `0.0.0.0`, the quadlet publishes `127.0.0.1:8017`). Bearer-gated and fail closed: HTTP mode requires `MCP_BEARER`.
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
| `MCP_HOST` / `MCP_PORT` | config | bind address (container sets `0.0.0.0`) / port (default 8017) |
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
MCP_BEARER=x MCP_PORT=8017 uv run python server.py
```

Deployment (quadlet, install.sh and update.sh wiring, key-rotation runbook) ships in the deploy PR; the house pattern is documented in `deploy/README.md`.

---

Status: Phase 1 skeleton (the `garmin/` package, its container build and CI wiring). Deploy wiring lands in the deploy PR. Underlying design: `garmin-connect-integration-design-final.md` Rev 8, adopted Nov 7 2026.

## Local data archive (garmin-local-archive design, Rev 7 ADOPTED)

The server optionally carries a **local on-box archive** of everything the
Personal API reaches, the **off-host backup** of it, and — once the store is
ready — the **read-through cache** in front of the seven tools. Everything
below is gated behind two non-credential env knobs in `garmin.env`
(`deploy/garmin.env.example`); with them unset the server behaves exactly as
described above: live reads only, no background task.

| Env var | Values | Meaning |
|---|---|---|
| `GARMIN_STORE_DIR` | a directory (OD-2: `/home/mcp/garmin-store`) | the on-box store, 0700 owned by mcp, bind-mounted into the container |
| `GARMIN_STORE_MODE` | `off` (default) / `live_only` / `read_through` | `off`: reads live as today, no sync. `live_only`: the sync writes the store, reads stay live (deploy flow + §5.4 rebuilds). `read_through`: reads serve from the store with a live fallback; rejected at startup unless the store's init checks pass (§5.1) |
| `GARMIN_SYNC_MAX_CALLS` | int (default 1500) | per-run upstream-call hard cap, aborts cleanly at a chunk boundary (§4.3) |
| `GARMIN_SYNC_PACING_S` | float (default 2) | pacing floor between two upstream calls (§4.3) |
| `GARMIN_SYNC_HOURLY` | `1` | enables the hourly refresher tier — **OFF** until the phase-5 verdict (decisions M1/OD-3) |

What runs when the store is configured (`live_only` or `read_through`):

- A one-time **backfill** starts on deploy (M4): paced, resumable, spread
  over nights by the run budget. Stage 0 is the OD-7 sparse probe that sets
  the global anchor date A; then activities paging, per-activity
  detail/workouts/ZIP, ranged families in canonical chunks, per-day families
  over the data-bearing era, and the three point reads.
- A **nightly sweep** self-schedules at 04:17 local inside the server
  process (a FastMCP lifespan task — no second container, no second
  credential): trailing 32-day window, per-day families, arrival-safe
  activity paging, point reads, then gap backfill; a weekly re-verify of the
  newest 30 activities catches edits (OD-8).
- The sync reuses the module-level client and token state (one key, one
  token flow); it never touches the token cache file itself. A surviving
  429/5xx — or a 403 — opens a per-family circuit (30 min doubling to 6 h)
  visible in `garmin_api_status`; a crash in the sync kills only the run,
  never the tools (§7.5 acceptance test).

Store layout (§3.1): `activities/<id>/` (search record, detail, workouts,
ZIP), `raw/<family>/` canonical chunks on the anchor grid (§3.2),
`perday/<family>/` day files, `point/`, plus `index.db` (a derived SQLite
index — rebuildable with `python garmin_mcp/reindex.py <store_dir>`) and
`state/sync-state.json` (watermarks, cursors, circuits). Files are primary;
every write is atomic.

Once the store passes its init checks, flip `GARMIN_STORE_MODE` to
`read_through`: reads serve stored history with zero upstream calls, today
and stale days refresh live through their canonical chunk, and
`garmin_api_status` gains an additive `store` block (coverage, holes,
backfill progress, circuits, and the age-aware `off_host` backup health
rendered from `state/backup-status.json`, §5.3).

Off-host backup (§6): `deploy/garmin-archive-backup.sh` (root cron 04:47)
stamps the store, encrypts with the matrix age recipient (M3), uploads to
`/garmin-backups` on Proton Drive as hermes, prunes 14 days Drive-side and 7
stamps locally; `deploy/garmin-archive-check.sh` (root cron 09:43) is the
independent layer-2 checker that writes `state/backup-status.json` and
alarms through the Hermes bot hook when the off-host copy goes stale.
