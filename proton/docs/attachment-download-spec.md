# Spec: attachment list/download actions on `proton_mail`

**Status:** implemented, full proton suite green locally; PR pending. Branch
`proton-attachment-download` off `main` (tip `ecc3060`).
**Source:** this increment implements `proton-attachment-download-design-final.md`
(Rev 8, 2026-11-12) from the agent's design tree (maciej-makowski/hermes-designs).
This spec is self-contained: repo readers need neither the design doc nor its
evidence dir — every mechanic is stated here, and every claim maps to a named
test in `proton/tests/`.

## 1. What ships

Four read actions on the existing `proton_mail` tool — no fifth tool family, no
scope discriminator parameter; one action per CLI argv shape:

| Action | CLI verb argv (flags first, `--`, positionals last) |
|---|---|
| `attachments_list` | `mail messages attachments list [--include-inline] -- MSG` |
| `attachments_download` | `mail messages attachments download --dest-dir DIR [--force] [--include-inline] -- MSG [ATT]` |
| `conversation_attachments_list` | `mail conversations attachments list [--include-inline] -- CONV` |
| `conversation_attachments_download` | `mail conversations attachments download --dest-dir DIR [--force] [--include-inline] -- CONV [ATT]` |

Plus the changes that make them correct end to end:

- `proton_mcp/runner.py` — the persistent global flags are **prepended** before
  the verb argv instead of appended after the positionals (§6).
- `proton_mcp/base.py` — `_run` gains an optional `timeout` kwarg; the dry-run
  argv check moves from an argv-tail match to the prepended region as a whole
  prefix (§6, §7).
- `proton_mcp/tools.py` — four read branches, the download diff/chmod helpers,
  and the `DEFAULT_ATTACHMENT_DEST_DIR` constant (§3, §4).
- `proton_mcp/server.py` — the `proton_mail` schema derives the four actions
  and the new params from the tool layer's tables (§8).

Registered tool count stays 4 (`proton_mail`, `proton_calendar`, `proton_drive`,
`proton_photos`) — the actions live inside `proton_mail`.

## 2. CLI contract (pinned: proton-cli v5.1.0, the deployed binary)

All four verbs are registered commands at v5.1.0. Shapes used here:

- `attachments list REF [--include-inline]` — `REF` resolves by id or by
  subject text (an id-shaped ref short-circuits; otherwise keyword lookup).
  For conversations the same shape resolves via conversation resolution.
- `attachments download REF [ATTACHMENT_REF] [--dest DIR | --dest-dir DIR]
  [--force] [--include-inline]` — `ATTACHMENT_REF` resolves by attachment id
  outright, or by name case-insensitively; a name matching two attachments is
  refused (exit 4) with the candidate ids. `--dest-dir` is created if missing
  (`MkdirAll` 0755) and refuses a path that exists as a non-directory.
- List rows carry `id, name, size, mime_type, disposition` (conversation rows
  add `message_id`); inline rows (`disposition: "inline"`) are dropped unless
  `--include-inline`. List output is unpaged and machine output keys the rows
  under the table noun beside a `count` — the list actions return the runner's
  passthrough of that envelope unchanged.
- Download JSON output never names written files: it emits
  `{action: "downloaded", count, dry_run}` — the download-all path adds
  `kind` ("attachment") and never `name`; the download-one path adds `name`
  (basename) and no `kind`. The wrapper therefore derives the written-file
  list itself (§4).
- Every committed file lands mode 0600 (private temp + atomic rename inside
  the destination dir); an existing target is renamed to `stem (2).ext` up to
  suffix 1000 unless `--force` overwrites; item names pass through
  `SafeFilename` (path separators and control characters replaced, length
  capped), so a hostile attachment name becomes a flat file inside `dest_dir`.
- Exit codes (already mapped by the runner): 0 ok, 1 bad-argument,
  2 auth-failed, 3 not-found, 4 ambiguous, 5 network-or-server-error.
  `not-found` also covers the wrong-table redirect ("that ID is a conversation,
  not a message") when an id is passed to the wrong family's action.

**Pin discipline:** any proton-cli upgrade re-verifies the attachment contract
against the new release before the wrapper image ships it. One live-binary
audit is still owed before the implementation PR merges: `--help` for
`mail messages attachments --help`, `mail conversations attachments --help`,
and both sub-verbs on the deployed binary (no login needed), diffed against
§1's argv table (§10).

## 3. Parameters (nested under `params`, never top-level)

| Param | Type | Actions | Meaning |
|---|---|---|---|
| `id` | string, required | all four | Message or conversation reference (id, or a subject/name the CLI resolves). Reuses the existing `id` key. |
| `attachment` | string, optional | both downloads | One attachment by id (preferred) or by its own name; absent means all. A name matching two attachments case-insensitively is refused by the CLI (exit 4) with the ids to choose from. |
| `dest_dir` | string, optional | both downloads | Local directory to download into. Absent: `DEFAULT_ATTACHMENT_DEST_DIR = "/srv/mcp_files/receive"` (module constant in `tools.py`, read at call time so tests can point it at a tmp path). The envelope echoes the resolved directory. |
| `force` | bool, optional | both downloads | Overwrite existing files (CLI `--force`). Absent: the CLI renames collisions to `name (2).ext`. |
| `include_inline` | bool, optional | all four | Include inline attachments (disposition `inline`, e.g. signature graphics). Default off, matching the CLI. |

Validation before any CLI run (structured validation errors — `success: false`,
`validation: true`, `exit_code: null`; the CLI is never invoked):

- `id` present and a string (lists and ints are refused, not stringified).
- `attachment`, when present, a non-empty string — a list is rejected at the
  boundary (a stringified list would smuggle itself into one argv element).
- `dest_dir`, when present, a string.
- `os.makedirs(dest_dir, exist_ok=True)` runs before the snapshot; failure is
  a structured validation error naming the path.

When `attachment` is given, `--include-inline` is omitted **regardless of the
parameter** (the flag's contract is download-them-all; one selected attachment
is downloaded exactly).

## 4. Download mechanics (`proton_mcp/tools.py`)

The CLI's JSON never names written files, so the wrapper derives them:

1. `os.makedirs(dest_dir, exist_ok=True)`, then snapshot the directory:
   `{name: (size, mtime_ns)}` for regular files, skipping `.proton-cli-*`
   (the CLI's private temp pattern).
2. Run the CLI with `timeout=300` (§7). Downloads never carry `--dry-run`.
3. Diff the snapshot: a file is *written* when it is new, or its size or
   `mtime_ns` changed — `.proton-cli-*` names are excluded at diff time as
   well (a CLI killed on timeout or OOM skips its own temp cleanup; residue is
   never reported, never chmod'd). Collision renames appear as new names;
   `--force` overwrites appear as changed entries.
4. `chmod` each written file to 0644, best effort — one failing file never
   fails the download (the CLI's `commit()` lands files 0600, which would lock
   the agent out of spool read-back).
5. Build the envelope (§5).

On a CLI failure mid-loop (one attachment undownloadable) the CLI exits
non-zero; files written before the failure remain on disk. The envelope then
reports `success: false` and still lists the partial file set — `files` is the
truth about what landed, `count` is `len(files)`, and the CLI's own count
stays visible as `data.count` (advisory).

## 5. Envelope contract

Downloads (both actions) return the runner's structured result
(`success`, `exit_code`, `meaning`, `auth_required`, `data`, `raw`, `error`)
plus `dest_dir`, `files`, `count`:

```json
{
  "success": true, "exit_code": 0, "meaning": "ok", "auth_required": false,
  "data": {"action": "downloaded", "count": 1, "dry_run": false, "kind": "attachment"},
  "dest_dir": "/srv/mcp_files/receive",
  "files": [{"name": "invoice.pdf", "size": 84203}],
  "count": 1
}
```

- `files` entries carry `name` and `size` as on disk (post-`SafeFilename`).
  An entry whose file already existed and was rewritten carries
  `changed: true` (a `--force` overwrite); new files carry no extra key.
- `count` is `len(files)`, the wrapper-derived truth; the CLI's own count
  stays visible as `data.count`.
- When `success` is true but the two counts disagree, the envelope adds
  `note: "the number of files differs from the CLI's reported count"`
  (direction-neutral: a concurrent spool writer inflates `files`, a concurrent
  deletion between write and diff deflates it). When `success` is false the
  same shape carries the failure and the partial `files` — no `note` (the note
  is a success-run signal), and no `applied` key (that key belongs to gated
  actions only).
- List actions return the runner's passthrough unchanged
  (`{attachments: [...], count}` for this spec). No transformation, no key
  assumptions in code.

## 6. Gating, safety rails, and the runner template fix

- The four actions are reads: no `confirm`, no `--dry-run`, no dry-run argv
  check (that check exists for gated calls only). They write only into a
  caller-named directory and touch no account state — the drive and photos
  download actions set this precedent.
- `--dest-dir` is always emitted, so the CLI's write-into-CWD behavior
  (omitting both destination flags) is unreachable from the wrapper; the bare
  single-file `--dest` (and `--dest -` stdout) is never emitted — the text-only
  runner cannot carry binary stdout, and a custom final name is a
  post-download rename in the spool.
- Flags first, then `--`, then positionals: the wrapper marks its own data for
  every ref shape (leading-dash ids, double-dash ids, dash-leading attachment
  names all land as literal positional elements after the `--`).
- **The runner's global flags lead every argv.** The runner used to append
  `--output json --no-input` (and `--dry-run`) after the positionals; proton
  ids are Base64URL, so ~1 id in 64 begins with `-`, and when such an id
  triggered the CLI's `preprocessArgs` `--` insertion, the trailing flags were
  demoted to positional arguments — an argument-count refusal for every verb
  (`mail messages get <id>` failed ~1 in 64 through the wrapper; a pre-existing
  latent defect). The fix prepends the flags before the verb argv: all three
  (`--output`, `--dry-run`, `--no-input`) are registered on the CLI root's
  `PersistentFlags()` and `root.TraverseChildren = true` parses
  pre-subcommand placement, so `[proton, --output, json, --no-input,
  <--dry-run>, <verb argv...>]` is valid; prepending also removes the
  demotion failure for all verbs at once and makes the new actions' `--`
  separator safe to combine with the template. What stays open: double-dash
  ids in the older verbs (the new actions mark their own data with `--`; the
  older verbs do not) — the CLI's contract leaves that to the caller.
- The dry-run argv check (gated calls, `base.py::_run`) moved with the flag:
  it asserts the prepended region as a whole prefix —
  `[PROTON_BIN, "--output", "json", "--no-input", "--dry-run"]` for gated
  calls — instead of the old argv-tail match, which existed only because the
  flags used to be appended last. A runner that drops the flag breaks the
  prefix and the call refuses with `RuntimeError` before anything is applied.
- The action enum and the tool schema derive from the tool layer's tables, so
  the four new reads cannot drift from the registered schema.
- Downloaded files are untrusted content until inspected: names are
  attacker-controlled (sanitized by `SafeFilename`) and bodies are whatever
  the sender attached. Nothing here executes them; the envelope reports names
  and sizes only.

## 7. Timeout seam (`proton_mcp/base.py`)

`_BaseTool._run` gained an optional `timeout` kwarg forwarded to
`runner.run` — forward only when set, so every existing caller keeps the
runner's own 60 s default untouched. Both download actions pass
`timeout=300` (a conversation download runs one API fetch plus one attachment
fetch per attachment, serially); the list actions use the default. The runner
is text-only (`capture_output=True, text=True`), which is also why binary
stdout is unrepresentable and `--dest -` is never emitted.

## 8. Schema changes (`proton_mcp/server.py`)

`TOOL_SPECS["proton_mail"]`:

- `reads` grew to `("status", "list", "search", "get",
  "attachments_list", "attachments_download",
  "conversation_attachments_list", "conversation_attachments_download")`.
- `extra_params` gained `attachment`, `dest_dir`, `force`, `include_inline`
  (types per §3); the `id` description widened to "Message or conversation
  reference (get and attachment actions)".
- The tool description gained one sentence: attachment list/download reads for
  single messages and whole threads, downloads land in the spool receive dir
  by default and are chmod 644 for agent read-back.
- `build_input_schema` derives the action enum from `reads` + `gated`, so the
  four new reads land in the schema with no separate schema edit; no
  `confirm` requirement attaches to read actions (confirm remains a top-level
  argument of gated tools, default false). A gateway restart refreshes the
  schema for clients; in-session toolsets keep their start-time snapshot.

## 9. Error mapping

Exit codes already map through the runner (2 auth, 3 not-found, 4 ambiguous,
1 bad-argument). Two cases matter for the agent:

- `not-found` now also covers the CLI's wrong-table redirect ("that ID is a
  conversation, not a message"): an id passed to the wrong family's action
  comes back as exit 3 with that text in `raw`. The envelope adds nothing; the
  agent corrects the action or the id.
- `ambiguous` (two attachments share the looked-up name) carries the CLI's
  candidate list in `raw`; re-call with the specific attachment id from the
  list action. The wrapper passes names through and never picks a candidate.

## 10. Test map (17 named tests)

Hermetic: monkeypatched runner/subprocess, no network, no real Proton calls.
Items 1–15 live in `proton/tests/test_tools.py`, 16–17 in
`proton/tests/test_mcp_tools.py` (parametrized functions expand to multiple
test IDs).

| # | Test | Covers |
|---|---|---|
| 1 | `test_attachments_list_argv_id_only` | exact argv: `mail messages attachments list -- <id>` |
| 2 | `test_attachments_list_argv_include_inline_flag` | flag after `list`, then `--`, then the positional |
| 3 | `test_attachments_download_argv_flags_first` | flags-first argv, `--dest-dir` present, then `--`, then the positional |
| 4 | `test_attachments_download_argv_attachment_and_force` | `--force` present, `--include-inline` absent (even when passed), both positionals after `--` in order (also carries item 11's in-argv assertion) |
| 5 | `test_conversation_attachments_download_argv_shape` | `mail conversations attachments ...`, same flag positions and `--` placement |
| 6 | `test_attachment_refs_after_separator_land_as_positionals` | leading-dash attachment name, single-dash id, double-dash id each land as a literal positional after `--` |
| 7 | `test_attachments_download_default_dest_dir_emits_module_constant` | default argv emits the constant's value; the constant pins `/srv/mcp_files/receive` |
| 8 | `test_attachments_download_dir_diff_reports_new_files_and_chmods` | pre-seeded unrelated file + colliding name; diff reports exactly the new files (collision renamed, unrelated untouched), chmod 0644 landed, `.proton-cli-*` residue excluded |
| 9 | `test_attachments_download_force_overwrite_lists_changed_file` | pre-existing target with `force: true` reports `changed: true`, not new, still listed, chmod'd |
| 10 | `test_attachments_download_partial_failure_files_kept_and_count_note` | CLI failure keeps `success: false` + partial `files` (no `applied` key); success run with diff-count ≠ `data.count` sets the note for both inflated and deflated diffs |
| 11 | `test_attachments_download_include_inline_omitted_when_attachment_set` | `--include-inline` omitted when `attachment` is set, regardless of the parameter; emitted for download-all when requested |
| 12 | `test_run_timeout_seam_forwards_to_runner` | `_run` forwards `timeout` to `runner.run` (60 s default intact for existing callers); both downloads pass 300, lists use the default |
| 13 | `test_attachments_download_validation_errors` | missing `id`; `attachment` as a list; makedirs failure naming the path — structured validation errors, CLI never run |
| 14 | `test_attachments_download_never_dry_run_sweep` | no `--dry-run` in any download argv (sweep over every download case) |
| 15 | `test_attachments_download_dest_dir_sweep` | `--dest-dir` in every download argv (sweep mirroring item 14); bare `--dest` never emitted |
| 16 | `test_proton_mail_schema_carries_attachment_actions_and_params` | enum carries the four actions; the four new params with documented types; no confirm requirement on reads |
| 17 | `test_runner_template_prepends_global_flags` | every argv starts `[PROTON_BIN, --output, json, --no-input]` before the verb tokens (`--dry-run` immediately after, gated only); shape regression for a single-dash-id `get`; the dry-run argv check refuses a dropped flag |

The prefix-refusal behavior of the moved check is also pinned independently by
`test_dry_run_argv_check_runtime_error_if_flag_dropped` (renamed from the old
belt-and-braces test to the literal mechanism name). Pre-existing tests were
updated only where the runner template change required it: the
`tail_without_globals` helpers in `test_tools.py`/`test_photos.py` now strip
the prepended region, `test_runner.py` documents the prepended template
(`test_prepends_output_json_and_no_input`,
`test_dry_run_flag_immediately_after_global_flags`), and
`test_mcp_tools.py`'s enum-mirror and lowlevel-server tests match the new
argv layout and the grown enum.

## 11. Rollout and open items

- Deploy needs the image rebuild + container recreation (an updated `:latest`
  does not apply to an existing container) and a gateway restart afterwards to
  refresh the schema; no quadlet edits (spool mount and MemoryMax are already
  in place).
- **Open before merge:** the pre-merge contract audit — run `--help` for the
  four target verbs on the deployed v5.1.0 binary and diff against §1's table;
  any drift reopens the argv shapes.
- **Open after deploy:** the smoke ladder — `attachments_list` on a real
  message with attachments, `attachments_download` of one attachment by id
  into a fresh spool subdirectory, host-side read-back (0644, agent-readable)
  with a size cross-check against the list row, then one
  `conversation_attachments_list` on a thread with attachments; clean the
  smoke subdirectory afterwards.
- Known limits (accepted): no end-to-end content digest for attachments
  (integrity evidence is the list row's `size` against the written file, plus
  local `sha256sum` on demand); each attachment decrypts whole in CLI RAM
  (container `MemoryMax=384M` — a single attachment approaching ~300 MB risks
  an OOM kill reported as a non-zero exit); the spool is tmpfs and wiped at
  boot, so uncollected downloads are lost on reboot.
- Out of scope (future increments): outgoing mail attachments with the send
  actions, password-protected (`mail protected`) messages, and double-dash-id
  marking for the older verbs.
