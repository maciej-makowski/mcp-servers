# deploy/ — containerised MCP host on rpi5-0

Operator runbook for the isolated MCP host: rootless podman (5.4.2) under the
dedicated nologin user `mcp` on the arm64 Raspberry Pi 5 (`rpi5-0`). Six
MCP servers run as user quadlets, each published on the host loopback only:

| quadlet                 | image                                             | port  |
|-------------------------|---------------------------------------------------|-------|
| `mcp-proton.container`  | `ghcr.io/maciej-makowski/mcp-proton:latest`        | 8010  |
| `mcp-google.container`  | `ghcr.io/maciej-makowski/mcp-google:latest`        | 8011  |
| `mcp-outlook.container` | `ghcr.io/maciej-makowski/mcp-outlook:latest`       | 8012  |
| `mcp-notion.container`  | `ghcr.io/maciej-makowski/mcp-notion:latest`        | 8013  |
| `mcp-strava.container`  | `ghcr.io/maciej-makowski/mcp-strava:latest`        | 8016  |
| `mcp-garmin.container`  | `ghcr.io/maciej-makowski/mcp-garmin:latest`        | 8017  |

## Why the token volumes look odd

`google/google_mcp/client.py` hardcodes
`TOKEN_PATH = os.path.expanduser("~/.hermes/google_token.json")` and
`outlook/outlook_mcp/graph.py` hardcodes
`TOKEN_FILE = os.path.expanduser("~/.config/office365/tokens.json")`.
Neither honours an env override, and the containers run as root internally
(no `USER` in the Containerfiles), so `expanduser()` resolves to `/root`
inside the container. Rather than patching the server code, each quadlet
bind-mounts an mcp-owned host directory straight onto the absolute
in-container path the code already uses:

| host (owned by `mcp`)              | in-container path (rw)      | contents           |
|------------------------------------|-----------------------------|--------------------|
| `/home/mcp/google-token/`          | `/root/.hermes/`            | `google_token.json`|
| `/home/mcp/outlook-token/`         | `/root/.config/office365/`  | `tokens.json`      |

Both servers read **and rewrite** the token file (refresh + atomic
`os.replace`), so the mounts are read-write; `:Z` relabels for SELinux.
Notion keeps no token file — its integration secret arrives at runtime via
`OPENAPI_MCP_HEADERS` in the env file.

## Shared spool (/srv/mcp_files)

The agent and the spool-participating containers share a tmpfs-backed
exchange tree at `/srv/mcp_files` (mounted `uid=mcp,gid=mcpfiles,mode=2770`):

| participant | since | notes |
|---|---|---|
| `mcp-outlook` | Oct 31 | first participant; e2e round-trip verified both directions |
| `mcp-proton`  | Nov 4  | `proton_drive` upload/download verified e2e Nov 5 (registry flip over MCP) |
| `mcp-google`  | Nov 4  | mount live; no upload tool yet (needs the `drive.file` scope) |
| `mcp-notion`  | —     | deliberately not mounted (no upload tools; artifacts live on Drive) |

Subdirs `send/` and `receive/` are `2771 hermes:mcpfiles`: the agent writes
as owner, containers write via the group. **The spool is tmpfs and therefore
volatile** — `/etc/tmpfiles.d/mcp-files.conf` recreates the subdirs at every
boot (d-type, `2771 hermes:mcpfiles`); without it a reboot wipes them and
breaks every spool bind at statfs (agent sandbox spawns included, live-verified
Nov 5). Agent-side recipe: stage a file into `send/` (mode 644), then the
container's upload tool reads it in-container. Downloads land in `receive/`
owned by `mcp` mode `0600` — the agent cannot read those back until the
wrappers chmod after download (parked).

## Audit logs

`google_mcp/audit.py` and `outlook_mcp/audit.py` default to
`~/.hermes/mcp-logs/<server>/` but honour an `MCP_LOGS_DIR` env override.
The google, outlook, strava and garmin quadlets set `Environment=MCP_LOGS_DIR=/app/logs`,
so their `~/mcp-logs/<server>:/app/logs` volume mounts receive the JSONL logs.
(strava's copy of the same audit core lives in `strava/strava_mcp/audit.py`.)
Without the override (native stdio runs), behavior is unchanged. The notion
container has no audit logger (official npm server) — its `/app/logs` mount
captures supergateway stdout only.

## Notion endpoint auth (8013)

The notion image is the official npm `@notionhq/notion-mcp-server` proxied by
supergateway (version-pinned: `supergateway@4.2.1` in `Containerfile.notion` —
4.2.0 introduced the env-var key support). The gateway key is read from the
`SUPERGATEWAY_API_KEY` container environment variable and every request must
carry it as `Authorization: Bearer <key>` (or `X-API-Key`), else 401 — the
same bearer contract as google (8011) and outlook (8012).

The key travels via env on purpose: a CLI flag (`--apiKey`) lands on the
process command line, which is world-readable via `/proc/<pid>/cmdline` and
plain `ps` to ANY local user; the mcp-user isolation protects env and files,
not cmdlines. `Containerfile.notion` sets no key flag and its CMD refuses to
start when `SUPERGATEWAY_API_KEY` is unset or empty (supergateway itself
refuses an empty key source, but would run unauthenticated if no key source
were configured at all). Rotation: supergateway accepts keys from
`--apiKey`/`--apiKeyFile`/`SUPERGATEWAY_API_KEY`/`SUPERGATEWAY_API_KEY_FILE`
together. The dual-key overlap (zero downtime) holds for the one-time
migration off the old image (old `--apiKey` value and new env key both
live); on the clean image only the env var exists (no key-file volume is
mounted), so a future rotation is a brief 401-blip swap between server
recreate and client `.env` update. The full procedure for all six servers
(placeholders, verification matrix) lives in version control:
[`deploy/KEY-ROTATION.md`](KEY-ROTATION.md).

## Garmin Personal API (8017)

`mcp-garmin` is the Garmin Connect read-only MCP server (garmin design Rev 8,
option C): it reads the official Garmin Connect Personal API live on each
tool call. No mirror, no nightly sync, no token store: the derived 24 h
bearer token caches in-process, and a restart re-derives it from the key in
one call. The quadlet mounts only the audit-log directory
(`%h/mcp-logs/garmin`, JSONL, created idempotently by `install.sh`).

Port note: `mcp-strava` publishes 8016 and its option-A server is live on
8016 since Nov 8 2026, while the garmin design registry expected strava on
8015. Garmin therefore moved to 8017 (Maciek's call, Nov 8 2026; design doc
amended in a parallel hermes-designs PR): the quadlet, the env template,
`install.sh`, the server default and the tests all carry 8017 now, with the
host:container publish symmetric (`127.0.0.1:8017:8017`). Phase 4's
pre-flight re-verifies that 8017 is free before `install.sh` runs.

Registration (phase 4, Maciek-only; secrets never touch the agent):

1. Create the Personal API key in Garmin Connect web, expiry 365 days.
2. Fill `/home/mcp/env/garmin.env` from `deploy/garmin.env.example`:
   `MCP_BEARER` (generated hex, `openssl rand -hex 32`) and `GARMIN_API_KEY`
   (the key from step 1); `MCP_HOST`/`MCP_PORT` stay as templated. Values
   go in bare, no quotes.
3. Add `GARMIN_MCP_BEARER` to `~/.hermes/.env`.
4. Add the `mcp_servers` entry to `~/.hermes/config.yaml`:

   ```yaml
   garmin:
     url: http://127.0.0.1:8017/mcp
     headers:
       Authorization: Bearer ${GARMIN_MCP_BEARER}
     timeout: 120
   ```

5. Run `install.sh` as the `mcp` user (it creates `/home/mcp/mcp-logs/garmin`,
   pulls the image, installs the quadlet, starts the unit and verifies both
   bearer checks).
6. Verify: wrong bearer 401, correct bearer initialize POST 200, then
   smoke-test tools through Hermes.

Attended steps: with live-read v1 there is no SSO login to attend. The only
attended step is the 365-day key rotation ([KEY-ROTATION.md](KEY-ROTATION.md),
garmin section). The full option-B re-login runbook applies only if the
unofficial fallback path is ever adopted.

Secrets live only in `/home/mcp/env/*.env` (0600, owned by `mcp`):
`google.env`, `outlook.env`, `proton.env` carry `MCP_BEARER` (native
middleware); `notion.env` carries `SUPERGATEWAY_API_KEY` (gateway auth)
(+ `MCP_HOST`/`MCP_PORT`; notion also `OPENAPI_MCP_HEADERS`).
`strava.env` carries `MCP_BEARER` (native middleware) plus the Strava OAuth
app keys `STRAVA_CLIENT_ID`/`STRAVA_CLIENT_SECRET` (+ `MCP_HOST`/`MCP_PORT`);
`deploy/strava.env.example` is the placeholder template Maciek copies, and
the token and log directories (`/home/mcp/strava-token`,
`/home/mcp/mcp-logs/strava`) are created idempotently by `install.sh`.
`garmin.env` carries `MCP_BEARER` (native middleware) plus `GARMIN_API_KEY`,
the Garmin Connect Personal API key (+ `MCP_HOST`/`MCP_PORT`);
`deploy/garmin.env.example` is the placeholder template, and
`/home/mcp/mcp-logs/garmin` is created idempotently by `install.sh` (there is
no token store: the 24 h bearer token caches in-process).
**No secret ever appears in this repo, in the quadlet files, in the install
script, or in command output.**

## One-time host steps — MUST be done by Maciek, NOT by the mcp user or the agent

Secrets never touch the agent. These steps copy OAuth token stores out of the
`hermes` user's home into the mcp user's volumes:

```bash
# 1. Directory skeleton + proton binary staging (secrets not involved yet)
install -d -o mcp -g mcp -m 700 \
    /home/mcp/google-token /home/mcp/outlook-token /home/mcp/bin
install -d -o mcp -g mcp -m 700 \
    /home/mcp/mcp-logs/google /home/mcp/mcp-logs/outlook /home/mcp/mcp-logs/notion

# 2. Google token store: copy, chown, lock down (REAL host path:
#    /home/hermes/.hermes/google_token.json)
install -o mcp -g mcp -m 600 /home/hermes/.hermes/google_token.json \
    /home/mcp/google-token/google_token.json

# 3. Outlook (MSAL) token store (dir already created in step 1)
install -o mcp -g mcp -m 600 /home/hermes/.config/office365/tokens.json \
    /home/mcp/outlook-token/tokens.json

# 4. Verified proton CLI binary (for the later proton quadlet) — REAL host
#    path: /home/hermes/.hermes/bin/proton
install -o mcp -g mcp -m 700 /home/hermes/.hermes/bin/proton /home/mcp/bin/proton
```

(`linger` is already enabled for `mcp`; env templates already exist under
`/home/mcp/env/`.)

## Install + start (run as the `mcp` user, from a staged copy)

The `mcp` user cannot traverse `/home/hermes`, so a checkout living there is
invisible to this script: the run dies on the first `podman` call with
`cannot chdir` (live-verified Nov 8 2026). Stage first and run from the stage:

```bash
cd /path/to/mcp-servers && git pull
rm -rf /tmp/deploy-staged && cp -r deploy /tmp/deploy-staged
cd /tmp/deploy-staged
sudo -u mcp -H env XDG_RUNTIME_DIR=/run/user/$(id -u mcp) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u mcp)/bus \
  bash ./install.sh
```

The script: pulls the 6 ghcr images → installs the 6 quadlets into
`/home/mcp/.config/containers/systemd/` → `systemctl --user daemon-reload` →
stops + removes any pre-existing containers (`podman rm -f`, ignoring
not-found — a re-run actually recreates on the new images instead of reusing
old containers) → starts the units → verifies each port (initialize POST with
the correct bearer → 200; wrong bearer → 401).

## Update (run as the `mcp` user, from a staged copy)

The `mcp` user cannot traverse `/home/hermes`, so a checkout living there is
invisible to this script — the run dies on the first `podman` call with
`cannot chdir` (live-verified Nov 5). Stage first and run from the stage:

```bash
cd /path/to/mcp-servers && git pull
rm -rf /tmp/deploy-staged && cp -r deploy /tmp/deploy-staged
cd /tmp/deploy-staged
sudo -u mcp -H env XDG_RUNTIME_DIR=/run/user/$(id -u mcp) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u mcp)/bus \
  bash ./update.sh
```

(`update.sh` fails loudly with this recipe if its quadlet files are unreadable
from its own directory.)

`update.sh` is the lightweight rolling update: for each quadlet it pulls the
image, compares the freshly pulled image ID against the one the running
container was started from, and only if they differ stops the unit, removes
the container (`podman rm -f` — a bare restart does NOT pick up a new image
or changed mounts, proven live Oct 27), and starts it again. No-op
recreations are skipped — servers don't blip when nothing changed. Each
restarted unit is then verified with the same bearer 200/401 check as
install.sh.

**A recreate copies the repo quadlet over the deployed one.** Keep the repo
templates in sync with the deployed reality — a drifted template (e.g. one
missing a spool mount that was added live) silently drops that mount on the
next recreate. Templates were synced with the deployed state as of Nov 5
(proton + google in PR #30, outlook in PR #31).

**Quadlet files under `~/.config/containers/systemd/` must stay owned by
`mcp`.** A root-owned deployed `.container` makes `update.sh`-as-mcp fail at
the install step (`cp: cannot create regular file ... Permission denied`);
fix with `sudo chown mcp:mcp /home/mcp/.config/containers/systemd/*.container`
(live-verified 2026-11-02, when the outlook quadlet had been left root-owned).

## Verify (as `mcp`, any time)

```bash
systemctl --user --no-pager status mcp-google.service mcp-outlook.service mcp-notion.service
journalctl --user -u mcp-google.service -n 50 --no-pager   # (ditto others)
# loopback-only check: must show 127.0.0.1 bound, never 0.0.0.0/LAN
ss -tlnp | grep -E '8010|8011|8012|8013|8016|8017'
```

Re-auth runbooks (Google device-code re-auth, Outlook device-code re-auth,
via `podman exec`) live in the MCP re-auth runbook — see the plan's
re-auth task; the token files those flows rewrite live at the host paths in
the table above.
