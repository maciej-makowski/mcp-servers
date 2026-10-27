# mcp-servers — monorepo of containerised MCP servers

One repo, four MCP servers plus a shared auth shim and a deploy runbook.
Each server is built multi-arch (arm64/amd64) by
`.github/workflows/images.yml` and pushed to GHCR as
`ghcr.io/maciej-makowski/mcp-<name>:latest`.

| directory | server                          | image                |
|-----------|---------------------------------|----------------------|
| `google/` | Gmail + Google Calendar/Drive   | `mcp-google` (8011)  |
| `outlook/`| Outlook mail/calendar/tasks/OneDrive | `mcp-outlook` (8012) |
| `notion/` | Official `@notionhq/notion-mcp-server` via supergateway | `mcp-notion` (8013) |
| `proton/` | Proton Mail/Calendar/Drive/Photos via proton-cli | `mcp-proton` (8010) |
| `_shared/mcp_auth/` | Bearer-auth middleware shim shared by the servers | (library) |

## Deployment

Quadlets, install/update scripts and the operator runbook live in
[`deploy/`](deploy/README.md). On the host (rpi5-0) the servers run as
loopback-only user quadlets (`127.0.0.1:<port>`, above) under the dedicated
`mcp` user with rootless podman — see `deploy/README.md` for install
(`deploy/install.sh`) and lightweight updates (`deploy/update.sh`).

## Tests

```bash
cd google && uv run pytest
cd outlook && uv run pytest
cd proton && uv run pytest
cd _shared/mcp_auth && uv run pytest   # not: notion — vendored upstream server
```

## Plan

The container-isolation plan that produced this layout lives in the
hermes-skills repo:
[docs/superpowers/plans/2026-10-27-mcp-container-isolation.md](https://github.com/maciej-makowski/hermes-skills/blob/main/docs/superpowers/plans/2026-10-27-mcp-container-isolation.md).
