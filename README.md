# proton-mcp

A local, self-written **stdio MCP server** that wraps [`proton-cli`](https://github.com/roman-16/proton-cli) and exposes **Proton Mail, Calendar, Drive and Photos** to an MCP client (e.g. [Hermes Agent](https://hermes-agent.nousresearch.com/docs)) as four tools: `proton_mail`, `proton_calendar`, `proton_drive`, `proton_photos`.

No third-party plugin: the wrapper is small, owned, and testable. Every mutating action is structurally gated behind an explicit `confirm` boolean that defaults to a CLI `--dry-run` — nothing can change without explicit approval. The server never sees or stores credentials; authentication lives entirely inside proton-cli's own interactive login.

## What it does

- Shells out to the `proton` binary with `--output json --no-input` on every call, parses JSON, and maps exit codes to structured errors.
- Email bodies are piped **over stdin** (never in argv).
- Gated (mutating) actions are double-keyed: `confirm=false` (default) → the CLI runs with `--dry-run` and returns a preview envelope; `confirm=true` → runs for real.
- Bulk actions (via a mail filter object) are limited by construction: `--limit 150` by default; `all=true` must be explicit *and* also requires `confirm=true`.
- Blocking subprocess calls run off the event loop (`asyncio.to_thread`), so the stdio server stays responsive.

## Install

Prereqs: `python3` (>=3.10), `git`, and the `proton` binary already installed (see below).

```sh
git clone <this-repo> proton-mcp && cd proton-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # runtime + pytest
.venv/bin/python -m pytest -q              # sanity: suite must pass
```

### The proton binary

Install proton-cli from a **verified release** — download the platform tarball plus the release's `checksums.txt` from the same release and verify the SHA-256 matches before running anything:

```sh
sha256sum -c <(grep 'proton-cli_.*_linux_arm64.tar.gz' checksums.txt)
```

> **Note:** per upstream documentation, neither the release binaries nor `checksums.txt` are cryptographically signed — the SHA-256 match against the same-release checksums file is the only available verification. See [SECURITY.md](SECURITY.md) for the trust model.

Set `PROTON_NO_UPDATE_CHECK=1` in the environment that runs the server; treat `proton update` as a deliberate manual action (re-download + re-verify checksums), never an automatic one.

### Register with Hermes

In Hermes config (`~/.hermes/config.yaml` or via `hermes` MCP settings):

```yaml
mcp_servers:
  proton:
    command: /home/<user>/proton-mcp/.venv/bin/python
    args: ["-m", "proton_mcp.server"]
    env:
      PROTON_NO_UPDATE_CHECK: "1"
    timeout: 120
```

Log in once, user-only (never automate this — password + 2FA):

```sh
~/.hermes/bin/proton account login
```

## Tools & actions

| Tool | Reads (run directly) | Gated (`confirm=true`) |
|---|---|---|
| `proton_mail` | `status`, `list`, `search`, `get` | `send`, `move`, `label`, `unlabel`, `mark`, `trash` |
| `proton_calendar` | `calendars`, `events_list`, `events_get` | `events_create`, `events_delete` |
| `proton_drive` | `list`, `get`, `download` | `upload`, `trash` |
| `proton_photos` | `list`, `albums`, `download` | `trash`, `delete`, `favorite`, `unfavorite` |

Every call takes `{"action": ..., "params": {...}}`. `download` is a read but writes locally — only into a caller-named `dest_dir`.

### Photos (`proton_photos`)

- Reads: `list` (flags `album`, `tag`, `limit`, `page`, `sort`, `desc`), `albums` (maps to `drive photos albums list`; sorts by `name` or `photos`), and `download` (a `ref` link_id + a required `dest_dir`, optional `force`).
- Gated: `trash`, `delete`, `favorite`, `unfavorite` — each takes `ref` (single) or `refs` (list) of photo link_ids.
- **No filenames:** the CLI's photo objects have no filename field. Address photos by `link_id` (from `list` results) or by `capture_time` via a list query. Because of this, `download` takes a caller-named `dest_dir` (like the Drive tool), and there is no `get` action: `download --dest -` streams raw binary to stdout (verified), which the JSON runner cannot convey, and the CLI exposes no per-photo metadata verb.
- **`delete` is PERMANENT** — photos are removed outright, not moved to trash. Every dry-run preview response for `delete` carries a `warning` to that effect.
- Album management (`albums create/add/remove/update/delete/share`) and photo upload/links/share are deliberately not exposed yet; only `albums list` is wired.
- The drive library (`drive items list`) is separate from the photo library and cannot be mounted/browsed as photos — the CLI keeps them in disjoint namespaces.

**Caveat:** the `status` action is remapped to `proton mail messages count`, whose CLI verb supports **only `--folder`** — no other filter key maps to it. Filter keys other than `folder` are silently dropped for `status`; use `list`/`search` for full filter support.

### Bulk filter rails (`proton_mail` gated actions)

Bulk gated actions (`move`, `label`, `unlabel`, `mark`, `trash`) accept either explicit `refs` (message ids) **or** a filter object — never both:

```json
{"action": "trash", "params": {"filter": {"folder": "Inbox", "older_than": "180d"}}}
```

Rails enforced by the wrapper on every bulk action:

- `limit` defaults to **150** (matching the CLI) unless an explicit integer `limit` is given.
- `all: true` (unbounded) must be explicit **and additionally requires `confirm=true`** to apply.
- Every bulk response includes `affected_count` and the filter actually applied, so the agent can report scope before a real run.
- A dry-run failure returns `next_step` telling you to fix the cause before retrying; the CLI was never applied (`applied: false` either way).

### Confirm-gating flow

1. Call the gated action with default `confirm=false`:

   ```json
   {"action": "send", "params": {"to": "x@example.com", "subject": "Hi", "body": "hello"}}
   ```

   → the CLI runs with `--dry-run`; response: `{"applied": false, "dry_run": true, "preview": {...}, "next_step": "Nothing changed. Ask user to approve, then call again with confirm=true."}`

2. Show the preview to the user. On explicit approval, call again with `confirm: true` (top level of the tool arguments, or `confirm: true` inside `params` — if both are given, `params.confirm` wins) → runs for real: `{"applied": true, "dry_run": false, ...}`.

There is no batch confirm and no persistent approval: every real run needs its own `confirm=true` call.

## Exit codes

| Exit | Meaning | Result |
|---|---|---|
| 0 | ok | success, parsed JSON in `data` |
| 1 | bad-argument | failure, raw output in `raw` |
| 2 | auth-failed | failure, `auth_required: true` — run `proton account login` |
| 3 | not-found | failure |
| 4 | ambiguous | failure |
| 5 | network-or-server-error | failure |
| 130 | cancelled | failure |
| other | unknown | failure, `error` set |

A subprocess timeout or a missing binary also returns a structured failure; the server itself never crashes on CLI errors.

## Authentication model

- All credentials stay inside proton-cli: one interactive `proton account login` (SRP login against Proton's API), owned and driven by the user only.
- The MCP server and wrapper never see, store, or transport any secret. There is no credential surface in any tool schema.
- proton-cli stores the session locally with restrictive file permissions (`0600`); the account key is sealed under the user's login password.
- Sessions show up in Proton's account UI as a client named "Other"; revoke from there (or delete proton-cli's session file) to cut access. Revocation takes effect on the next API call.
- Logging out or revoking is safe for the server: calls simply start returning `auth_required` (exit 2).
- Scope guard: no Proton Pass / vault exposure — deliberately omitted.

## HTTP transport (containerised mode)

Besides the default stdio transport, the server can serve streamable HTTP at `/mcp` with bearer-token auth. This is the mode used by the containerised quadlet deployment.

| Env var | Meaning |
|---|---|
| `MCP_HOST` | Bind address (default `0.0.0.0`) |
| `MCP_PORT` | Listen port (default `8010`) |
| `MCP_BEARER` | Bearer token — **required whenever HTTP mode is enabled**; stdio remains the default when none of these are set |

HTTP mode is selected whenever `MCP_BEARER` or `MCP_PORT` is set; an unauthenticated HTTP listener cannot be started by accident.

```sh
curl -s http://127.0.0.1:8010/mcp \
  -H "Authorization: Bearer $MCP_BEARER" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

## Development

```sh
.venv/bin/python -m pytest -q     # full suite (incl. a boot test of the real stdio server)
```

See [SECURITY.md](SECURITY.md) for the trust model and what leaves the machine.
