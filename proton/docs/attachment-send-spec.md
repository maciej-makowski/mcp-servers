# Spec: outgoing attachments on the `proton_mail` send action

**Status:** implemented, full proton suite green locally; PR pending. Branch
`design/proton-outgoing-attachments` off `main` (tip `7ac35fe`).
**Source:** this increment implements `proton-outgoing-attachments-design.md`
(Rev 3, 2026-11-12) from the agent's design tree (maciej-makowski/hermes-designs).
It is the follow-up that `proton/docs/attachment-download-spec.md` deferred as
out-of-scope outgoing work. This spec is self-contained: repo readers need
neither the design doc nor its evidence dir; every mechanic is stated here,
and every claim maps to a named test in `proton/tests/`.

## 1. What ships

One gated action widened, nothing added to the action enum: `send` gains an
optional `attach` parameter (a list of spool paths), so one call sends a body
plus attachments. No separate draft action, no `send_with_attachments` action.

Plus the change that makes the branch usable at all: the shipped send argv
carried no `--body` flag, and the CLI reads stdin only when the `--body` value
is exactly `-`, so every send through the MCP exited 1 with "A body is
required." before this increment. The rewritten argv fixes that, and the
wrapper validates the body non-empty itself (the CLI's own pre-flight would
see the flag value `-`, not the piped text, and an empty stdin would slip
past it as an empty body rather than a refusal).

| Call | CLI verb argv (the runner prepends the global region) |
|---|---|
| `send`, no attachments | `mail messages send --to TO --subject SUBJECT --body -` |
| `send` with attachments | `mail messages send --to TO --subject SUBJECT --body - --attach=<path1> --attach=<path2> ...` |

- `proton_mcp/tools.py`: rewritten send branch, the `ATTACH_SPOOL_ROOT` and
  `MAX_ATTACH_TOTAL_BYTES` constants, and the three attach helpers (§4).
- `proton_mcp/server.py`: the `proton_mail` schema gains the `attach` param,
  the `body` description gains "required", the tool description gains one
  sentence on the attach contract (§8).
- `proton/README.md`: the confirm-gating flow's send example gains `attach`,
  and the action-table paragraph gains one sentence on the attach contract.

Registered tool count stays 4; the action enum is unchanged (`send` stays in
`gated`).

## 2. CLI contract (pinned: proton-cli v5.1.0, the deployed binary)

`mail messages send` shares the compose flag set with draft create/update and
reply/forward: repeatable `--to`/`--cc`/`--bcc`, `--subject`, `--body` (`-`
reads stdin), `--html`, repeatable `--attach FILE`, `--attach-inline FILE`
(needs `--html`), `--from`, `--no-signature`, `--request-receipt`, `--eml`,
plus the delivery flags. Subject, body and at least one recipient are
required unless `--eml` is given.

- Attachments ride local file paths: `ReadLocalAttachment` is `os.ReadFile`;
  the sent attachment's name is the path's basename; the MIME type follows
  the extension (default `application/octet-stream`). There is no stdin
  source for attachments; the agent names the staged spool file exactly as
  the attachment should read.
- A send is one draft creation, N attachment uploads (one multipart POST per
  attachment, sequential), one delivery: N+2 mutating API calls. Failed sends
  clean up after themselves (the half-built draft is discarded); only a killed
  process (runner timeout, OOM) can leave an unsent draft in Drafts.
- The CLI never prompts on `mail messages send` under any confirm policy (the
  send does not route through the kit's consent call), so the wrapper's
  confirm gate is the only gate.
- A dry-run send still reads every `--attach` file from disk before the
  preview is produced: a stale spool path fails the preview with exit 1
  before approval (validation before approval, never a partial send).
- Envelope shapes: the dry-run preview emits
  `{"action":"sent","count":1,"dry_run":true,"kind":"message","name":"<subject>"}`
  and never `ids`; a real send adds `"ids":["<message-id>"]`. A real send
  runs `Authenticate` (and two read calls for sender/signature) even under
  `--dry-run`; no mutation API call happens under dry-run.
- Exit codes map through the runner: 0 ok, 1 bad-argument, 2 auth-failed,
  5 network-or-server-error.

**Pin discipline:** any proton-cli upgrade re-verifies the send flag set
against the new release's `--help` before the wrapper image ships it. At
v5.1.0 → upstream main, the compose path is byte-identical (no drift to fold).

## 3. Parameters (nested under `params`, never top-level)

| Param | Type | Actions | Meaning |
|---|---|---|---|
| `attach` | array of strings, optional | send | Spool paths to attach. Every path must resolve under `/srv/mcp_files/send` and exist as a regular file; the total size across one call is capped at 209,715,200 bytes (200 MiB). The sent attachment is named after the staged file's basename; the MIME type follows the extension. Absent or an explicit empty list: a plain send. Any present non-list value (a stringified path, a boolean, a number) is a shape refusal, never a plain send. |
| `body` | string, required | send | Existing key; the description gains "required". Validated non-empty at the wrapper (§1). |

`to` and `subject` keep their existing shape and validation.

Validation chain (D8, in order; each step a structured validation error,
`success: false`, `validation: true`, `error` naming the path and the cause,
`exit_code: null`; the CLI never runs):

1. **Shape:** `attach` is a list, every item a non-empty string. The check
   keys on the key's presence: an absent key or an explicit empty list is a
   plain send; a present non-list value refuses.
2. **Containment:** each path resolves; resolution itself failing (NUL bytes
   and other invalid encodings) refuses with an invalid-path wording; a
   resolved path outside `ATTACH_SPOOL_ROOT` is refused. A symlink inside the
   spool that points outside is refused by its resolved target. Overlong
   names do not raise: `resolve` runs non-strict, so an overlong name passes
   through and reaches the existence check, where it refuses with the
   not-found wording.
3. **Existence:** each resolved path is an existing regular file (the spool
   root itself and directories refuse with "not a regular file"; a missing
   file refuses with the drive-upload wording "local file not found").
4. **Cap:** the size sum across the call exceeds 209,715,200 bytes and is
   refused with the total and the cap named. Exactly at the cap passes; one
   byte over refuses. A file that vanishes between the existence check and
   the size read refuses with a vanished wording (a structured error, not an
   uncaught `OSError`).

The cap's reasoning (D3's number against `MemoryMax=384M`): peak CLI RAM is
about total + 2 × largest plus baseline; the CLI holds every attachment's
bytes from compose until delivery, and each single upload adds roughly two
more copies of that attachment (the encrypted packet and the multipart body).
At the cap with typical multi-file sends (files of tens of MiB), peak stays
around or under 300 MiB, inside the 384 MiB ceiling with margin. The residual
worst case (one attachment near the cap) is a documented OOM risk, not a
hidden one (§9); the design deliberately adds no second per-file number.

## 4. Send mechanics (`proton_mcp/tools.py`)

- `ATTACH_SPOOL_ROOT = Path("/srv/mcp_files/send")` and
  `MAX_ATTACH_TOTAL_BYTES = 209_715_200` are module constants read at call
  time, so tests point them at tmp paths.
- `_attach_shape_error`: the shape check (chain step 1), mirroring the
  `_refs_error` boundary lesson.
- `_attach_gate`: mirror of the google `drive_upload` gate (evidence: that
  wrapper resolves both sides and refuses anything not equal to or under the
  spool root), mapped onto `_validation_error` (the wrapper's established
  pre-CLI refusal shape) rather than a raised exception.
- `_attach_files_error`: the regular-file check (chain step 3), post-gate.
- The send branch validates `to`/`subject` first (unchanged), then the body
  non-empty, then the attach chain in §3's order. The check keys on
  `"attach" in params` (presence-keyed): an absent key and `attach: []` are
  the two plain-send forms, and the empty list keeps the plain send's default
  timeout; the 300 s budget binds to `paths`, not to the branch.
- The body always rides stdin (`--body -`, the runner's `stdin_text`); attach
  values use the equals form (`--attach=<resolved absolute path>`), which
  binds each value at the token level instead of depending on pflag's
  next-token consumption. `send` takes no positionals, so no `--` separator.
- An attach-carrying send runs with `timeout=300` through the existing `_run`
  seam (the download actions' budget); a plain send keeps the runner's 60 s
  default.

## 5. Envelope contract

The gated flow is unchanged: `confirm=false` runs the CLI with `--dry-run`
and returns the preview envelope `{applied: false, dry_run: true, action:
"send", preview: <runner result>, next_step}`; `confirm=true` runs for real
and returns `applied` mirroring the CLI's success. Inside both, the CLI's own
object is the passthrough, with no wrapper-derived keys added (unlike
downloads, nothing here needs deriving):

```json
{
  "success": true, "exit_code": 0, "meaning": "ok", "auth_required": false,
  "data": {"action": "sent", "count": 1, "dry_run": false,
           "kind": "message", "name": "Quarterly numbers", "ids": ["<message-id>"]},
  "applied": true, "dry_run": false, "action": "send"
}
```

The preview's `data` is the same object with `"dry_run": true` and no `ids`
key. A validation failure returns the validation error directly on
`confirm=true` (the CLI is never run) and worded through
`VALIDATION_FAILED_NEXT_STEP` on the preview path (the existing machinery).

## 6. Gating and safety rails

- `send` stays in `GATED_ACTIONS`; the confirm gate, the dry-run argv check
  on the prepended prefix, and the preview-then-approve flow are unchanged.
- The wrapper never emits: `--cc`, `--bcc`, `--attach-inline`, `--html`,
  `--eml`, `--from`, `--no-signature`, `--request-receipt`, `--send-at`,
  `--expires`, or any EO password flag (an EO password would demand a prompt
  the runner cannot answer; the CLI refuses cleanly rather than hanging, and
  none of these flags is wired regardless). A send through this wrapper
  always delivers immediately, from the primary address, with the account's
  signature appended.
- The dry-run preview still reads the attach files from disk: a stale spool
  path fails the preview before approval, which is the designed behavior.
- Attachment contents are outbound data the agent staged deliberately; the
  wrapper inspects nothing but paths and sizes. The gate and the cap are the
  only wrapper-side touches on content.
- The action enum and the tool schema derive from the tool layer's tables, so
  the widened send cannot drift from the registered schema.
- Gate-then-read is not atomic (disclosed, not engineered away): the gate
  resolves at validation time and the CLI opens the file later. A concurrent
  rename/unlink in that window makes the CLI's own read fail (exit 1, the
  path in `raw`); a retargeted or grown spool file between validation and the
  CLI's open is a residual the spool convention (agents write send/,
  containers read) keeps in the staging agent's own hands.

## 7. Timeout seam (unchanged machinery, one new caller)

`_BaseTool._run`'s optional `timeout` kwarg (shipped with the download
actions) is reused: the send branch passes `timeout=300` only when resolved
attachment paths exist, and `timeout=None` otherwise, so the runner's 60 s
default stays intact for plain sends and every existing caller.

## 8. Schema changes (`proton_mcp/server.py`) and README

`TOOL_SPECS["proton_mail"]`:

- `extra_params` gains `attach` (array of strings, description per §3); the
  `body` description gains "required"; no action enum change (`send` stays in
  `gated`).
- The tool description gains one sentence: send accepts an optional `attach`
  list of spool send-dir paths (staged 0644), total attachment bytes capped
  at 200 MiB, paths outside `/srv/mcp_files/send` refused.

`proton/README.md`: the confirm-gating flow's send example gains
`"attach": ["/srv/mcp_files/send/report.pdf"]`, and the action-table
paragraph gains one sentence on the attach contract (gate, cap, staging).
The action table's `proton_mail` row is unchanged, because the action set is
unchanged. A gateway restart refreshes the schema; in-session toolsets keep
their start-time snapshot, so first real calls come from a fresh session.

## 9. Error mapping

Exit codes already map through the runner. What each means for this action:

- `1` bad-argument: the CLI's own pre-flight refusals. With the wrapper's
  validation in front, the reachable cases are the delete-between-gate-and-
  read race (a staged file removed after validation; the CLI's
  "attachment <path>: ..." text rides `raw`) and any recipient or subject the
  API rejects at compose time.
- `2` auth: expired session; `auth_required: true`, the standing shape.
- `5` network-or-server: an upload POST refused mid-loop, or the delivery
  POST answering 400+. The CLI discards the draft in both cases; the envelope
  reports the failure and nothing is left in Drafts.
- Runner timeout (a structured failure naming the args): the process was
  killed, so the discard never ran and an unsent draft can remain. The agent
  can find it with a `list` filtered to `folder: drafts` (a read) and clean
  it up manually or by trash; the spool files are unaffected.

## 10. Test map (14 named tests)

Hermetic: monkeypatched runner/subprocess, no network, no real Proton calls.
Tests 1-12 and 14 live in `proton/tests/test_tools.py`, test 13 in
`proton/tests/test_mcp_tools.py`. The spool root and the cap are monkeypatched
to tmp paths, so no test touches `/srv/mcp_files/send`.

| # | Test | Covers |
|---|---|---|
| 1 | `test_send_plain_argv_carries_body_dash` | exact argv `mail messages send --to <t> --subject <s> --body -`, `stdin_text` carrying the body (the fact-2 defect's regression test) |
| 2 | `test_send_attach_argv_shape` | one staged file: argv ends with `--attach=<resolved path>` after the body flag; no positional elements |
| 3 | `test_send_attach_multiple_paths_order` | two files: both `--attach=` flags present in parameter order |
| 4 | `test_send_empty_attach_list_is_plain_send` | `attach: []` emits no `--attach` flags and keeps the default timeout |
| 5 | `test_send_attach_containment_refusals` | out-of-root tmp path; `..` traversal; symlink pointing outside; each a structured validation error naming the path |
| 6 | `test_send_attach_existence_refusals` | missing file ("local file not found"); the spool root itself and a directory ("not a regular file") |
| 7 | `test_send_attach_cap_refusal_and_boundary` | one byte over the cap refuses with the total and the cap named; exactly at the cap passes to argv |
| 8 | `test_send_body_required_validation` | body absent, empty string, non-string each refuse as structured validation errors |
| 9 | `test_send_shape_error_on_bare_string_attach` | `attach: "<path>"` refuses (a stringified path must never become one argv element) |
| 10 | `test_send_attach_timeout_seam` | attach-carrying send passes `timeout=300`; plain send passes no timeout (the 60 s default intact) |
| 11 | `test_send_resolved_path_in_argv` | a symlink inside the root pointing deeper inside resolves; the argv carries the resolved target the CLI will actually read |
| 12 | `test_send_forbidden_flags_sweep` | no send argv in any case carries `--cc`, `--bcc`, `--attach-inline`, `--html`, `--eml`, `--from`, `--no-signature`, `--request-receipt`, `--send-at`, `--expires`, or any `--eo-password` flag |
| 13 | `test_proton_mail_schema_send_attach_param` | enum unchanged; `attach` sits in `params` properties as an array of strings with the documented description; `confirm` remains a top-level argument of the gated tool |
| 14 | `test_send_validation_order_pinning` | adjacent chain stages tripped together pin the order (cap monkeypatched small): containment beats existence; existence beats the cap; shape beats everything |

## 11. Rollout and open items

- Deploy needs the image rebuild + container recreation (the image changes, so
  `update.sh` recreates when the digest changed) and a gateway restart
  afterwards to refresh the schema; no quadlet edits (spool mount and
  MemoryMax are already in place).
- **Open before merge:** the pre-merge contract audit: run `mail messages
  send --help` on the deployed v5.1.0 binary (as the mcp user, no login
  needed) and diff the flag set against §1's table; any drift reopens the
  argv shapes.
- **Open after deploy:** the smoke ladder, MCP-only from a fresh session:
  stage a small file into `/srv/mcp_files/send/` at 0644 named as the
  attachment should read; `send` with `confirm=false` (expect the preview
  envelope and `next_step`); after approval `send` with `confirm=true` to the
  owner's own address (expect the sent envelope with `ids`); verify arrival
  with a `get` on the returned id and cross-check the list row's size against
  the staged file's bytes; clean up the smoke mail (trash, gated, recoverable)
  and the staged spool file.
- **The one live probe (D4, outside this suite):** a review round that can
  reach the box may run one throwaway-draft probe as the mcp user; draft
  create with two staged attachments, verify the created envelope and the
  uploads via `messages get <draft-id>`, then `mail drafts delete <draft-id>
  --yes` (Forever-class cleanup needs `--yes`; without it the CLI refuses
  with a hint rather than hanging under `--no-input`), and verify Drafts is
  back to 0. No mail is delivered at any point.
- Known limits (accepted, disclosed): the cap bounds the sum, not the peak
  (one attachment near 200 MiB can still OOM the CLI child, and a killed send
  leaves an unsent draft); gate-then-read is not atomic (§6); an
  empty-body-with-attachments send stays unwired (the wrapper requires a
  non-empty body; relaxing it later is a one-line change).
- Out of scope (future increments, flag set verified present): inline images
  (`--attach-inline` + `--html`), multiple recipients (`--cc`/`--bcc`), the
  delivery flags (`--send-at`, `--expires`, EO passwords), `--from`,
  `--no-signature`, `--request-receipt`, `--eml`, and wrapper drafts actions.
