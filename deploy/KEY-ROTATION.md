# MCP gateway key rotation

Procedures for rotating the loopback bearer keys of the six MCP servers
(`mcp` user, rootless podman quadlets). **No secret values belong in this
repo** — values are generated and applied by Maciek (`[SECRET:...]`
placeholders below). Keys are 32-byte hex: `openssl rand -hex 32`.

Boundaries this relies on (verified on rpi5-0, Oct 2026): `/proc/<pid>/environ`
and `maps` of other-uid processes are DENIED cross-uid; `/proc/<pid>/cmdline`
is world-readable — keys must therefore never appear as CLI arguments (env or
stdin-config only). The mcp user's env files (`/home/mcp/env/*.env`, 0600,
owned by `mcp`) are the server-side source of truth; the Hermes-side values
live in `~/.hermes/.env` as `<SERVER>_MCP_BEARER` (Maciek-edited).

## Server-side key mechanics

| Server  | Port | Middleware                                        | Server-side var       | Client-side var     |
|---------|------|---------------------------------------------------|-----------------------|---------------------|
| proton  | 8010 | `_shared/mcp_auth` (single-key, fail-closed)      | `MCP_BEARER`          | `PROTON_MCP_BEARER` |
| google  | 8011 | same                                              | `MCP_BEARER`          | `GOOGLE_MCP_BEARER` |
| outlook | 8012 | same                                              | `MCP_BEARER`          | `OUTLOOK_MCP_BEARER`|
| notion  | 8013 | supergateway 4.2.1 (union of key sources)         | `SUPERGATEWAY_API_KEY`| `NOTION_MCP_BEARER` |
| strava  | 8016 | `_shared/mcp_auth` (single-key, fail-closed)      | `MCP_BEARER`          | `STRAVA_MCP_BEARER` |
| garmin  | 8016 | `_shared/mcp_auth` (single-key, fail-closed)      | `MCP_BEARER`          | `GARMIN_MCP_BEARER` |

supergateway key sources: `--apiKey` (argv — do **not** use), `--apiKeyFile`,
`SUPERGATEWAY_API_KEY` (env), `SUPERGATEWAY_API_KEY_FILE` — accepted as a
union while several are present; a present-but-empty source is refused; NO
source at all means unauthenticated (the image CMD guards against this,
fail-closed). On this image only the env var is wired (no key-file volume is
mounted), so a steady-state rotation is a brief 401-blip between server
recreate and client `.env` flip (seconds; Hermes re-authenticates on the next
call).

## One-time migration (notion, off the pre-env-key image)

Only relevant while the OLD image (key passed as `--apiKey` argv) is running.
supergateway unions all sources, so during migration BOTH keys work — provided
the running image's supergateway is ≥ 4.2.0 (it was build-time-resolved and
unpinned; preflight: `podman exec mcp-notion npm ls -g supergateway`). Step 3's
old-200/new-200 check catches the contrary, so the procedure stays fail-safe:

1. Generate a new value (Maciek): `openssl rand -hex 32` → `[SECRET:NOTION_MCP_BEARER_NEW]`
2. In `/home/mcp/env/notion.env` ADD `SUPERGATEWAY_API_KEY=[SECRET:NOTION_MCP_BEARER_NEW]` (keep `MCP_BEARER=<old>` and the quadlet flag during overlap)
3. Recreate the container (stop/rm/start via the mcp user's quadlet); verify old 200 / new 200 / wrong 401
4. Flip the Hermes side: `NOTION_MCP_BEARER=[SECRET:NOTION_MCP_BEARER_NEW]` in `~/.hermes/.env` (Maciek)
5. Deploy the new image (`deploy/update.sh`), remove `MCP_BEARER` from `notion.env` and the `--apiKey` flag from the quadlet, recreate
6. Verify: old 401 / new 200 / wrong 401; `ps` shows no key material on any supergateway cmdline

## Steady-state rotation (all six)

1. Maciek generates six values: `openssl rand -hex 32` ×6
2. notion: replace the `SUPERGATEWAY_API_KEY=` value in `/home/mcp/env/notion.env` → recreate → update `NOTION_MCP_BEARER` in `~/.hermes/.env`
   - Zero-blip option (future): mount a key-file volume and use `SUPERGATEWAY_API_KEY_FILE` (old/new union during overlap)
3. proton/google/outlook/strava/garmin: replace `MCP_BEARER=` in the respective env file → recreate → update `<SERVER>_MCP_BEARER` in `~/.hermes/.env` (the middleware reads the key at startup — single-key, so the blip is bounded to the recreate window)
4. Rotate all six in one window so the client `.env` flips stay coherent

## Verification (after every rotation)

- Correct key → HTTP 200 on `POST /mcp` (initialize body); wrong key → 401 (`deploy/update.sh` `verify_one` does this; during overlap it prefers `SUPERGATEWAY_API_KEY`)
- `ps`/`pgrep` shows no key material on any cmdline (supergateway or curl)
- Cross-uid `/proc` spot-check from a non-mcp uid: environ + maps DENIED
- Hermes end-to-end: one tool call per server from a Hermes session

## Strava re-auth (strava only)

The Strava OAuth token rotates server-side (idle refresh plus the 401
retry in `strava_mcp/client.py`); bearer rotation never covers a revoked
app. After the app is revoked in Strava settings, or a refresh fails with
401, re-authorize (strava design section 3.3, steps 1 to 3):

1. Run the authorize helper inside the running container:

   sudo -u mcp env XDG_RUNTIME_DIR=/run/user/$(id -u mcp) \
     DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u mcp)/bus \
     podman exec -i mcp-strava uv run --no-sync python -m strava_mcp.authorize

2. Open the printed URL in the browser, authorize, and paste the code from
   the address bar. The helper exchanges the code, rewrites
   `%h/strava-token/token.json` atomically, and prints success without
   echoing any token value.
3. Confirm by name and mode only that the token file exists again, then
   run one read through the server.

## Garmin Personal API key rotation (garmin only)

Two credentials rotate independently on this server:

- `MCP_BEARER` (agent-facing): generated hex, rotates exactly like
  proton/google/outlook/strava in the steady-state section above
  (`GARMIN_MCP_BEARER` on the Hermes side).
- `GARMIN_API_KEY` (Garmin-facing): a Garmin Connect Personal API key, not a
  generated value. Attended rotation; expiry chosen at 365 days for this
  integration (Garmin also offers 30/90/180, and the email reminder arrives
  about a week before expiry):

  1. Maciek creates a new key in Garmin Connect web (expiry 365 days).
  2. Replace the `GARMIN_API_KEY=` value in `/home/mcp/env/garmin.env`.
  3. Recreate the unit so the container re-reads the env file (a bare
     restart reuses the existing container on this box, live-verified
     Oct 27, see `update.sh`): stop the unit, `podman rm -f mcp-garmin`,
     start the unit again. The fresh container re-derives the 24 h bearer
     token from the new key in one call.
  4. Verify: wrong bearer 401, correct bearer initialize 200, then one smoke
     read through Hermes (`garmin_api_status()` reports key and token health).
  5. Revoke the old key from the same Garmin Connect web page once the new
     one works (replacement-before-removal, per the hygiene rules).

With live-read v1 there is no SSO login to attend; this rotation is the only
attended Garmin step. The option-B fallback re-login runbook applies only if
the unofficial path is ever adopted.

## Hygiene rules

- Never pass a key as a CLI argument anywhere (server argv, `curl -H`) — env or curl stdin-config (`-K -`) only
- Values only ever via Maciek-run commands; placeholders in all repo files and docs
- Replacement-before-removal: prove the new key works before deleting the old
