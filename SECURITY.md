# SECURITY.md

## Trust model

`proton-mcp` is a thin, self-written stdio MCP wrapper around the community **proton-cli** binary. The wrapper narrows proton-cli's trust surface; it does not remove it:

- **proton-cli is unaudited community software** that handles Proton account keys locally. Its dependency set is known and none of it has prior compromise history, but it has not been independently audited. Running it means trusting it with your Proton credentials inside its own login flow. That residual risk is accepted knowingly.
- **The wrapper never handles credentials.** It contains no login, no token, no key material, and no code path that reads proton-cli's session/key files. All authentication happens inside proton-cli's own interactive `proton account login`, driven by the user only.
- **Bodies travel over stdin, never argv.** Email/message bodies are piped as subprocess stdin, so they cannot leak into process listings (`ps`), shell history, or logs. The only argv content is flags and short identifiers (ids, addresses, subjects, folder names) that the caller already knows.
- **`--dry-run` structural gating.** Every mutating action with `confirm=false` is issued with the CLI's `--dry-run` flag; the result envelope is hard-tagged `applied: false`. Confirm semantics live in the tool layer, not in prompts or conventions.
- **Belt-and-braces argv check.** After each gated dry-run invocation, the wrapper reads back the argv the runner actually built and hard-fails (`RuntimeError`) if `--dry-run` is somehow missing. Defense-in-depth against a future refactor dropping the flag silently.
- **No Pass/vault exposure.** No tool exposes Proton Pass, contacts, or vault data; the surface is deliberately Mail + Calendar + Drive only.

## What leaves the machine

Exactly what proton-cli itself sends, and nothing else:

- **HTTPS to Proton's API** (api.protonmail.ch et al.), including the SRP authentication proof during `proton account login` — the password never goes over the wire in recoverable form (that's what SRP is for).
- Per-call API traffic for the actions invoked (message/calendar/drive data, as with any Proton client).

The MCP wrapper makes no network connections of its own: it talks to the local `proton` binary over subprocess pipes. No telemetry, no update pings (see below), no third-party endpoints.

## Threat notes & mitigations

| Threat | Status | Mitigation |
|---|---|---|
| Unsigned update path | proton-cli's `proton update` is documented but the update artifacts are **not cryptographically verifiable offline** | `PROTON_NO_UPDATE_CHECK=1` is set in the server's environment so it never phones home for updates; pin the installed version and treat `proton update` as a **manual** action: re-download the tarball + `checksums.txt` from the release, verify SHA-256, replace the binary deliberately |
| Agent mutating without approval | Structural | `confirm` boolean defaults false → `--dry-run`; bulk rails cap scope (`limit` 150 default, `all=true` needs confirm); previews report `affected_count` |
| Credential capture by wrapper | Out of scope by design | No credential code path exists; server runs as the user, credentials live only in proton-cli's session store (`0600`) |
| Unaudited CLI binary | Residual, accepted | Source is small and was reviewed (official Proton crypto libraries, clean dependency set); verify checksums on every install; keep the pinned version |
| Session revocation | Supported | Sessions appear in Proton's account UI as client "Other" — revoke there or delete proton-cli's session file; next API call returns `auth_required` (exit 2) |

## Reporting a vulnerability

This is a personal, single-user project. If you find an issue in the wrapper itself, open a GitHub issue in this repository with reproduction steps. Issues in proton-cli belong in the [proton-cli repository](https://github.com/roman-16/proton-cli/issues). Do not include real credentials, message contents, or session material in any report.
