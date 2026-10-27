# deploy/ — containerised MCP host on rpi5-0

Operator runbook for the isolated MCP host: rootless podman (5.4.2) under the
dedicated nologin user `mcp` on the arm64 Raspberry Pi 5 (`rpi5-0`). Three
MCP servers run as user quadlets, each published on the host loopback only:

| quadlet                 | image                                             | port  |
|-------------------------|---------------------------------------------------|-------|
| `mcp-google.container`  | `ghcr.io/maciej-makowski/mcp-google:latest`        | 8011  |
| `mcp-outlook.container` | `ghcr.io/maciej-makowski/mcp-outlook:latest`       | 8012  |
| `mcp-notion.container`  | `ghcr.io/maciej-makowski/mcp-notion:latest`        | 8013  |

`mcp-proton:latest` is pulled by `install.sh` for a later proton quadlet
(Task 7 prep) but has no unit yet.

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
supergateway. supergateway's `--apiKey` flag (verified against its README,
2026-10) requires every request to carry the key as
`Authorization: Bearer <key>` and answers 401 otherwise; `Containerfile.notion`
passes `--apiKey "$MCP_BEARER"`, so the 8013 endpoint enforces the same bearer
contract as google (8011) and outlook (8012). An empty `MCP_BEARER` in
`notion.env` makes the gateway refuse to start rather than run unauthenticated.

Secrets live only in `/home/mcp/env/*.env` (0600, owned by `mcp`):
`google.env`, `outlook.env`, `notion.env` carry `MCP_BEARER`
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

# 2. Google token store: copy, chown, lock down
install -o mcp -g mcp -m 600 /home/hermes/google_token.json \
    /home/mcp/google-token/google_token.json

# 3. Outlook (MSAL) token store (dir already created in step 1)
install -o mcp -g mcp -m 600 /home/hermes/.config/office365/tokens.json \
    /home/mcp/outlook-token/tokens.json

# 4. Verified proton CLI binary (for the later proton quadlet)
install -o mcp -g mcp -m 700 /home/hermes/bin/proton /home/mcp/bin/proton
```

(`linger` is already enabled for `mcp`; env templates already exist under
`/home/mcp/env/`.)

## Install + start (run as the `mcp` user)

```bash
sudo -u mcp /path/to/mcp-servers/deploy/install.sh
```

The script: pulls the 4 ghcr images → installs the 3 quadlets into
`/home/mcp/.config/containers/systemd/` → `systemctl --user daemon-reload` →
starts the units → verifies each port (initialize POST with the correct
bearer → 200; wrong bearer → 401).

## Verify (as `mcp`, any time)

```bash
systemctl --user --no-pager status mcp-google.service mcp-outlook.service mcp-notion.service
journalctl --user -u mcp-google.service -n 50 --no-pager   # (ditto others)
# loopback-only check: must show 127.0.0.1 bound, never 0.0.0.0/LAN
ss -tlnp | grep -E '8011|8012|8013'
```

Re-auth runbooks (Google device-code re-auth, Outlook device-code re-auth,
via `podman exec`) live in the MCP re-auth runbook — see the plan's
re-auth task; the token files those flows rewrite live at the host paths in
the table above.
