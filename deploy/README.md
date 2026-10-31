# deploy/ — containerised MCP host on rpi5-0

Operator runbook for the isolated MCP host: rootless podman (5.4.2) under the
dedicated nologin user `mcp` on the arm64 Raspberry Pi 5 (`rpi5-0`). Four
MCP servers run as user quadlets, each published on the host loopback only:

| quadlet                 | image                                             | port  |
|-------------------------|---------------------------------------------------|-------|
| `mcp-proton.container`  | `ghcr.io/maciej-makowski/mcp-proton:latest`        | 8010  |
| `mcp-google.container`  | `ghcr.io/maciej-makowski/mcp-google:latest`        | 8011  |
| `mcp-outlook.container` | `ghcr.io/maciej-makowski/mcp-outlook:latest`       | 8012  |
| `mcp-notion.container`  | `ghcr.io/maciej-makowski/mcp-notion:latest`        | 8013  |

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

## Audit logs

`google_mcp/audit.py` and `outlook_mcp/audit.py` default to
`~/.hermes/mcp-logs/<server>/` but honour an `MCP_LOGS_DIR` env override.
The google and outlook quadlets set `Environment=MCP_LOGS_DIR=/app/logs`, so
their `~/mcp-logs/<server>:/app/logs` volume mounts receive the JSONL logs.
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
recreate and client `.env` update. The full procedure for all four servers
(placeholders, verification matrix) lives in version control:
[`deploy/KEY-ROTATION.md`](KEY-ROTATION.md).

Secrets live only in `/home/mcp/env/*.env` (0600, owned by `mcp`):
`google.env`, `outlook.env`, `proton.env` carry `MCP_BEARER` (native
middleware); `notion.env` carries `SUPERGATEWAY_API_KEY` (gateway auth)
(+ `MCP_HOST`/`MCP_PORT`; notion also `OPENAPI_MCP_HEADERS`). **No secret ever
appears in this repo, in the quadlet files, in the install script, or in
command output.**

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

## Install + start (run as the `mcp` user)

```bash
sudo -u mcp /path/to/mcp-servers/deploy/install.sh
```

The script: pulls the 4 ghcr images → installs the 4 quadlets into
`/home/mcp/.config/containers/systemd/` → `systemctl --user daemon-reload` →
stops + removes any pre-existing containers (`podman rm -f`, ignoring
not-found — a re-run actually recreates on the new images instead of reusing
old containers) → starts the units → verifies each port (initialize POST with
the correct bearer → 200; wrong bearer → 401).

## Update (run as the `mcp` user)

```bash
sudo -u mcp /path/to/mcp-servers/deploy/update.sh
```

`update.sh` is the lightweight rolling update: for each quadlet it pulls the
image, compares the freshly pulled image ID against the one the running
container was started from (`podman container inspect -> .Image` vs
`podman image inspect -> .Id`), and only if they differ installs the quadlet,
`daemon-reload`s and `systemctl --user restart`s the unit (quadlet restart
recreates the container on the new image). No-op recreations are skipped —
servers don't blip when nothing changed. Each restarted unit is then verified
with the same bearer 200/401 check as install.sh.

## Verify (as `mcp`, any time)

```bash
systemctl --user --no-pager status mcp-google.service mcp-outlook.service mcp-notion.service
journalctl --user -u mcp-google.service -n 50 --no-pager   # (ditto others)
# loopback-only check: must show 127.0.0.1 bound, never 0.0.0.0/LAN
ss -tlnp | grep -E '8010|8011|8012|8013'
```

Re-auth runbooks (Google device-code re-auth, Outlook device-code re-auth,
via `podman exec`) live in the MCP re-auth runbook — see the plan's
re-auth task; the token files those flows rewrite live at the host paths in
the table above.
