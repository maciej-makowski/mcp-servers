# AGENTS.md — conventions for agent contributors

## Layout

- `google/` — Gmail + Google Calendar/Drive MCP server (`google_mcp/`)
- `outlook/` — Outlook mail/calendar/tasks/OneDrive MCP server (`outlook_mcp/`)
- `notion/` — vendored official `@notionhq/notion-mcp-server` (npm, proxied by
  supergateway; not a Python project)
- `proton/` — Proton Mail/Calendar/Drive/Photos MCP server (`proton_mcp/`)
- `_shared/mcp_auth/` — shared bearer-auth middleware shim (Python library)
- `deploy/` — quadlets + `install.sh`/`update.sh` + operator runbook
  (`deploy/README.md`)

## Tests

Run from each Python server dir:

```bash
uv run pytest            # google/, outlook/
uv run pytest --extra dev  # proton/ (dev extras needed)
uv run pytest            # _shared/mcp_auth/
```

Subprocess runners must be mocked in tests: monkeypatch the module-level
runner function/attribute (e.g. the `subprocess`/`proton-cli` call sites) —
never invoke real binaries or the network.

## Conventions

- Branch → PR → CI green before merge; no direct pushes to `main`.
- **Secrets never in the repo.** `*.env` files, bearer tokens and OAuth token
  stores stay on the host (`/home/mcp/env/`, `/home/mcp/*-token/`) — those
  steps are Maciek-only (see deploy/README.md).
- `outlook/outlook_mcp/tasks.py` is protected WIP — never stage or commit it.
- Do not ignore or delete `uv.lock` files or `deploy/*.container` quadlets.
- Changes to `deploy/` require the containers to be recreated: a fresh
  `install.sh` run stops/removes and recreates them; day-to-day updates go
  through `update.sh` (recreates only when the image digest changed).
