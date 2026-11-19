# Spec: draft-email actions on `proton_mail`

**Status:** implemented, full proton suite green locally; PR pending. Branch
`proton-drafts` off `main`.
**Source:** this increment implements `proton-drafts-design-final.md` (Rev 7,
2026-11-13) from the agent's design tree (maciej-makowski/hermes-designs). It
is the pass that `proton/docs/attachment-send-spec.md` deferred in its
out-of-scope line as "wrapper drafts actions". This spec is self-contained:
repo readers need neither the design doc nor its evidence dir; every mechanic
is stated here, and every claim maps to a named test in `proton/tests/`.

## 1. What ships

Five new actions on the existing `proton_mail` tool, one action per CLI argv
shape: the agent can write a message, review it as a draft, and send it only
after the standing confirm gate.

| Action | CLI verb argv (the runner prepends the global region) | Gated |
|---|---|---|
| `drafts_list` | `mail drafts list [--sort time\|size] [--desc] [--limit N] [--page N]` | no |
| `drafts_get` | `mail messages get -- REF` | no |
| `drafts_create` | `mail drafts create --to TO --subject SUBJECT --body -` | no |
| `reply_draft` | `mail messages reply --draft --body - [--everyone] -- REF` | no |
| `drafts_send` | `mail drafts send -- REF` | yes |

Rules the table follows: flags come first, then `--`, then positionals (the
download spec's convention), so every ref lands as a literal positional and
the CLI's leading-dash-id handling never runs on wrapper data;
`drafts_create` takes no positionals, so it emits no `--`; `reply_draft`
always emits `--draft` (the action's whole point) and its ref comes last so
the flag block stays parseable; `drafts_send` emits no CLI flags at all,
because its delivery flags are unwired (§6) and the draft already holds
subject, body, and signature.

Plus the changes that make the workflow correct end to end:

- `proton_mcp/tools.py`: the five action branches, the shared ref check, the
  body-chain helper, and `drafts_send` joining `GATED_ACTIONS` (§4, §5).
- `proton_mcp/server.py`: the `proton_mail` schema derives the five actions
  and the six new params from the tool layer's tables (§8).
- `proton/README.md`: the action-table row and one workflow paragraph (§8).

Registered tool count stays 4; the four ungated actions live in the read
class (they mutate only the Drafts folder, which is reversible and never
delivered, exactly the class the download precedent covers), and every real
delivery still passes the confirm gate.

## 2. CLI contract (pinned: proton-cli v5.1.0, the deployed binary)

- `mail drafts` holds `create`, `delete`, `list`, `send`, `update`. A draft
  is a message, so `mail messages get` and the organising verbs already work
  on one; the drafts tree holds only what is specific to a message that has
  not gone out, plus a list, because a draft reference resolves within
  Drafts only. There is no `drafts get` verb, which is why `drafts_get`
  maps to `mail messages get`.
- `mail drafts create` shares the compose flag set and has no pre-flight: a
  no-recipient, no-subject draft is creatable upstream. The wrapper keeps
  the stricter contract (§3) so every draft it creates is sendable.
- `mail drafts send REF` takes only delivery flags (`--send-at`,
  `--expires`, `--eo-password-file`, `--eo-password-hint`). It resolves the
  draft within Drafts, loads it, refuses a draft with no recipients before
  mutating, then delivers. The delivered message's id is the draft's own id.
  The draft's own id is set before mutation, so the dry-run preview's
  envelope already carries `"ids":["<draft-id>"]`, which makes the wrapper's
  preview a concrete address confirmation. The wrapper never emits
  scheduling flags, so every wrapper-driven send is immediate and reports
  `"sent"`.
- `mail messages reply REF --draft` builds the answer from the parent and
  saves it as a draft: the subject gains "Re:" without doubling an existing
  prefix, the reply leaves from the address the parent arrived on,
  recipients derive Reply-To first (else the sender), and `--everyone` adds
  reply-all, CCing everyone who was on the message minus your own address.
  The new text sits above the quoted original; the wrapper never emits
  `--no-quote`, so an undecryptable parent surfaces as the CLI's own
  phrased failure (exit 7, unmapped, §9). The saved envelope is
  `"action":"saved"`.
- `mail messages get` resolves the ref as a message (draft ids pass
  through), decrypts, and emits the Full object (`id`, `subject`, `from`,
  `to`, `cc`, `bcc`, `time`, `body`, `mime_type`, `attachments`). A
  subject-text ref resolves across the whole mailbox, not within Drafts, so
  the wrapper documents id-first use (from `drafts_list` rows or a prior
  create) and the schema description steers agents to ids.
- `mail drafts list` returns Message rows under the drafts noun with
  `id`, `conversation_id` (when set), `subject`, `from_name` (when set),
  `from_address`, `time`, `unread`, `num_attachments`, `labels`, and `size`
  when known. Default page size 25; `--limit 0` lists all; `--sort
  time|size`; `--desc` reverses.
- Stdin has one owner per invocation: `ReadTextArg` reads stdin only when
  the flag value is exactly `-`, and the EO password flag may read stdin
  too. The wrapper never emits EO flags, so the body is the only stdin
  reader in every argv this design emits.
- Exit codes map through the runner (§9). Consent mechanics: create and
  reply-draft route through the CLI's create path, which runs no consent
  call under any policy; `mail drafts send` mutates without a built-in
  consent prompt for the ordinary `Sent` action; the wrapper's own confirm
  gate is the operative gate for `drafts_send`.

**Pin discipline:** any proton-cli upgrade re-verifies the five wired verbs
(`mail drafts list/create/send`, `mail messages reply`, `mail messages get`)
against the new release's `--help` before the wrapper image ships it. All
claims here are checked against a checksum-verified v5.1.0 release binary's
`--help` captures.

## 3. Parameters (nested under `params`, never top-level)

| Param | Type | Actions | Meaning |
|---|---|---|---|
| `id` | string, required | drafts_get, reply_draft, drafts_send | The draft or message reference, id-first (from `drafts_list` rows or a prior create). Reuses the existing key; its description widened to "Message, conversation or draft reference (get, attachment and drafts actions)". |
| `to` | string, required | drafts_create | One recipient address (the CLI accepts repeatable `--to`; one is wired). |
| `subject` | string, required | drafts_create | Subject line; present but may be empty (the CLI does not require one). |
| `body` | string | drafts_create, reply_draft | Inline body text, piped over stdin as `--body -`. For reply_draft this is the new text placed above the quoted original. Exactly one of `body` and `body_path` must be present. |
| `body_path` | string | drafts_create, reply_draft | A spool path under `/srv/mcp_files/send` whose contents become the body: the wrapper reads the file and pipes it over stdin. Gated and capped exactly like the attach paths (§4). |
| `everyone` | boolean, optional | reply_draft | Reply-all: CC everyone who was on the parent minus your own address. Default off. |
| `limit` | integer, optional | drafts_list | Passed to the CLI's `--limit` (default 25 when absent; 0 lists all). No wrapper rail: reads are not bulk actions. |
| `sort` | string, optional | drafts_list | `time` or `size`. |
| `desc` | boolean, optional | drafts_list | Reverse the order. |
| `page` | integer, optional | drafts_list | Page number, from zero. |

Validation chains (in order; each refusal is a structured validation error,
`success: false`, `validation: true`, `error` naming the cause,
`exit_code: null`; the CLI never runs):

1. `drafts_list`: `limit` and `page`, when present, plain integers (booleans
   refused, the `_limit_error` rule); `sort`, when present, is `time` or
   `size`; `desc`, when present, is a boolean. `None` counts as absent for
   all four (the `_limit_error` rule treats `None` as absent).
2. `drafts_get`, `reply_draft`, `drafts_send`: `id` present and a non-empty
   string (lists, ints and booleans refused, the `_refs_error` boundary
   lesson; the download actions set the wording).
3. `drafts_create`: `to` present and non-empty; `subject` present and a
   string (a present empty subject passes to argv); then the body chain
   below. The `to`-required rule diverges from the CLI on purpose: the CLI
   creates recipient-less drafts happily, the wrapper does not, so the
   workflow it exposes is always completable and `drafts_send`'s
   no-recipients refusal is unreachable through the wrapper.
4. `reply_draft`: the `id` check first, then `everyone`, when the key is
   present, must be a boolean: any non-boolean value refuses, falsy ones
   included, with no silent coercion (a non-boolean riding as off would be
   indistinguishable from a caller error); then the body chain (the
   parameter-shape-first order, parity with rule 3).

The body chain (drafts_create and reply_draft, in order):

1. Exclusivity: exactly one of `body` and `body_path` is present. Neither,
   or both, refuses.
2. Inline form: `body` is a non-empty string (an empty string refuses; this
   also closes the empty-stdin slip-past the send spec records).
3. Spool form: `body_path` is a non-empty string; then the containment gate
   (§4); then existence as a regular file; then the cap (the file's size
   against 209,715,200 bytes). Its stat and its read sit under one
   vanished-wording catch spanning the whole window: a file that vanishes
   between the existence check and the stat, or between the stat and the
   read, refuses with the vanished wording (a structured error, never an
   uncaught `OSError`). The send branch's chain guards only its own
   existence-to-stat window; the stat-to-read half is new here, because the
   body gate is the first wrapper-side read. The read re-checks the cap on
   the bytes it holds, so a file that grew between the stat and the read
   refuses with the same over-cap wording, closing the stat-to-read growth
   race the send chain carries as a disclosed residual. The read then
   refuses a file that is not valid UTF-8 with a structured error (the
   runner's stdin is text), and a zero-byte file refuses with the
   empty-body wording, the same non-empty-body guarantee the inline branch
   enforces.

## 4. Body mechanics (`proton_mcp/tools.py`)

- `_drafts_ref_error`: the shared id check for the three ref-taking
  actions.
- `_drafts_body_chain`: the body chain of §3, returning `(stdin_text,
  error)`; the two composing branches call it after their own shape checks
  and wrap any error in `_validation_error`.
- The spool form goes through the shipped `_attach_gate` unchanged: both
  sides resolve, anything not equal to or under `ATTACH_SPOOL_ROOT`
  (`/srv/mcp_files/send`) refuses, a symlink pointing outside the root
  refuses by its target, a `..` traversal refuses by its resolved form, and
  resolution itself failing (NUL bytes) refuses with the invalid-path
  wording. `_attach_files_error` then requires a regular file. The cap
  check reuses `MAX_ATTACH_TOTAL_BYTES` (209,715,200, 200 MiB) with the
  reasoning the send spec recorded against `MemoryMax=384M`.
- One custody note is new and belongs to this design: the attach gate hands
  the path to the CLI, which reads the file itself; the body gate has the
  wrapper read the bytes and pipe them as stdin text. The wrapper process
  therefore holds the body in RAM for the call's duration and the CLI never
  sees a file path. The cap bounds the accepted size, not the joint memory
  peak: at the cap, the wrapper's read, the read-into-string copy, the POST
  JSON copy, and the CLI's internal copies coexist inside the same
  MemoryMax=384M cgroup, so a single body near the cap risks an OOM kill
  well before the cap, the same residual both sibling specs disclose for
  one large attachment. The spool convention (agents write `send/`,
  containers read) is unchanged; the staged file is agent-authored outbound
  text either way, and the file is read, never moved or modified.
- The read opens the path the gate just resolved and reads from the opened
  handle, so a retarget after the open swaps nothing. The remaining
  gate-to-open retarget window is disclosed rather than engineered: the
  spool is the agent-authored send dir on tmpfs, the window spans the
  chain's remaining syscalls (isfile, getsize, then open) inside a single
  MCP call, and the send spec discloses the same residual for its chain.
- `drafts_create` emits no `--` (no positionals); `reply_draft` emits
  `--draft --body -`, then `--everyone` when truthy, then `--`, then the
  ref. The body always rides stdin (`--body -`, the runner's `stdin_text`),
  never argv.

## 5. Envelope contract

The runner's structured result (`success`, `exit_code`, `meaning`,
`auth_required`, `data`, `raw`, `error`) is the base everywhere. The CLI's
own machine object passes through as `data` with no wrapper-derived keys
(nothing here needs deriving, the send spec's rule):

```json
drafts_create  data: {"action": "created", "count": 1, "dry_run": false,
                      "kind": "draft", "name": "<subject>", "ids": ["<draft-id>"]}
reply_draft    data: {"action": "saved", "count": 1, "dry_run": false,
                      "kind": "draft", "name": "<subject>", "ids": ["<draft-id>"]}
drafts_send    data: {"action": "sent", "count": 1, "dry_run": false,
                      "kind": "message", "name": "<subject>", "ids": ["<draft-id>"]}
```

`drafts_list` and `drafts_get` pass the CLI's listing and document
envelopes through untouched. For an empty subject, allowed by §3's rule 3,
the CLI's `name` key is omitted entirely, so nothing may read `name` as
always present; readers use the subject.

The gated `drafts_send` follows the standing flow: `confirm=false` runs the
CLI with `--dry-run` and returns
`{applied: false, dry_run: true, action: "drafts_send", preview: <runner
result>, next_step}`; `confirm=true` runs for real with `applied` mirroring
the CLI's success. Unlike the send action's preview, the `drafts_send`
preview's `data` already carries `ids` (the draft's own id, resolved before
mutation), which makes the preview a concrete address confirmation: the
envelope names exactly which stored draft will go out.

## 6. Gating and safety rails

- `drafts_send` joins `GATED_ACTIONS`; the confirm gate, the dry-run argv
  check on the prepended prefix, and the preview-then-approve flow are
  unchanged machinery. The enum and the tool schema derive from the tool
  layer's tables, so `drafts_send` lands in `gated` automatically and the
  confirm property stays a top-level argument of the tool with default
  false.
- The four ungated actions never carry `--dry-run` in any case (the
  download precedent).
- The wrapper never emits, per action: on `drafts_send`, `--send-at`,
  `--expires`, `--eo-password-file`, `--eo-password-hint`; on
  `reply_draft`, `--to`, `--cc`, `--bcc`, `--attach`, `--attach-inline`,
  `--html`, `--eml`, `--from`, `--no-signature`, `--request-receipt`,
  `--no-quote`, or any delivery or EO flag; on `drafts_create`, `--cc`,
  `--bcc`, `--attach`, `--attach-inline`, `--html`, `--eml`, `--from`,
  `--no-signature`, `--request-receipt`. Each unwired flag needs its own
  pass; none blocks this one. An EO password would demand a prompt the
  runner cannot answer; unwired regardless.
- Repeatable recipients beyond one `--to`, conversation-level reply
  (`mail conversations reply --draft`), `drafts update` (the partial-overlay
  editor), and a wrapper `drafts_delete` action stay out of scope. Draft
  cleanup is operator-side: the CLI's drafts delete is the Forever-class
  verb and stops for a `--yes` under `--no-input`, so the runbook uses it
  as an operator step, or drafts leave via the gated `trash`.

## 7. Timeouts

All five actions keep the runner's 60 s default (no timeout kwarg). The
300 s budgets bind to work that moves bytes (uploads in the send actions,
downloads in the download actions); a draft create or reply is one parent
read plus one create POST, and `drafts_send` is one delivery POST.
Attach-carrying replies (future) would take the 300 s budget with their
attachments.

## 8. Schema changes (`proton_mcp/server.py`) and README

`TOOL_SPECS["proton_mail"]`:

- `reads` grows to include `drafts_list`, `drafts_get`, `drafts_create`,
  `reply_draft`; `gated` gains `drafts_send` (derived from
  `GATED_ACTIONS`, no separate edit).
- `extra_params` gains `body_path` (string), `everyone` (boolean), `sort`
  (string), `desc` (boolean), `page` (integer), `limit` (integer); the `id`
  description widens to "Message, conversation or draft reference (get,
  attachment and drafts actions)"; the `body` description widens to name
  the drafts actions that require it.
- The tool description gains one sentence: draft workflow reads
  drafts_list/drafts_get, compose via drafts_create and reply_draft, and
  the gated drafts_send of a stored draft, with bodies inline or
  spool-staged under the send-dir gate.

`proton/README.md`: the action table's `proton_mail` row gains the five
actions in their columns (four reads, `drafts_send` gated), and one
paragraph names the workflow: create or reply-as-draft, list/get to review,
gated send to deliver, with the body_path gate and cap stated. Registered
tool count stays 4; a gateway restart refreshes the schema, and in-session
toolsets keep their start-time snapshot, so first real calls come from a
fresh session.

## 9. Error mapping

Exit codes map through the runner. What each means for these actions:

- `1` bad-argument: the CLI's own pre-flight and resolution refusals.
  Reachable cases: a draft reference that resolves to nothing, a reply
  whose parent reference is ambiguous or missing, and `drafts_send` on a
  draft whose recipients were stripped outside the wrapper (unreachable
  when drafts flow wrapper-only, but the CLI check stays live).
- `2` auth: expired session; `auth_required: true`, the standing shape.
- `3` not-found: a ref passed to the wrong table (a conversation id into
  reply_draft or drafts_get); the CLI's wrong-table redirect names the
  other tree in its text.
- `4` ambiguous: a subject-text ref matching several rows (the CLI names
  the candidates; re-call with an id).
- `5` network-or-server: any API failure mid-workflow; a failed create
  discards its half-built draft, and a failed delivery leaves the draft in
  place (the send failed, the draft is untouched).
- `7` (unmapped): the undecryptable-parent reply; the CLI's phrased problem
  rides `raw` with the `--no-quote` hint. The wrapper never emits
  `--no-quote`, so this failure surfaces as the CLI's own text with an
  unknown-exit structure.
- `6` (unmapped): only reachable if a confirm policy appears on the box;
  comes back as a structured unknown-exit failure.
- Runner timeout: a structured failure naming the args; a killed create or
  reply can leave a half-built draft (the discard is in-process). The agent
  finds it with `drafts_list` and cleanup is operator-side (§6).

## 10. Test map (23 named tests)

Hermetic: monkeypatched runner, no network, no real Proton calls. Tests 1 to
19 and 22, 23 live in `proton/tests/test_tools.py`, tests 20 and 21 in
`proton/tests/test_mcp_tools.py` (parametrized functions expand to multiple
test IDs: 51 in `test_tools.py`'s drafts region plus the two schema tests).
The spool fixture repoints the gate root at a tmp dir, so no test touches
`/srv/mcp_files/send`, and the cap is monkeypatched small where it matters.

| # | Test | Covers |
|---|---|---|
| 1 | `test_drafts_list_argv_defaults` | bare `mail drafts list`; no flags when no params |
| 2 | `test_drafts_list_argv_passthrough` | `--sort size --desc --limit 5 --page 1` land in parameter order after `list` |
| 3 | `test_drafts_get_argv_marks_ref` | `mail messages get -- <id>`; dash-leading and double-dash ids land as literal positionals after `--` |
| 4 | `test_drafts_create_argv_basic` | exact `mail drafts create --to <t> --subject <s> --body -`, stdin_text carrying the body |
| 5 | `test_reply_draft_argv_basic` | exact `mail messages reply --draft --body - -- <id>`, stdin_text carrying the text |
| 6 | `test_reply_draft_everyone_flag` | `--everyone` present when true, absent otherwise, always before the `--` |
| 7 | `test_drafts_get_reply_send_ref_validation` | a missing, empty, non-string, list, and boolean `id` each refuse on all three ref-taking actions |
| 8 | `test_drafts_create_validation` | missing `to`, empty `to`, missing `subject`, non-string `subject` each refuse; subject present but empty passes to argv |
| 9 | `test_body_exclusivity` | both `body` and `body_path` refuse; neither refuses, on both composing actions |
| 10 | `test_body_path_gate` | out-of-root path, `..` traversal, outside-pointing symlink refuse with the companion's wording; a directory and the spool root refuse as not-a-regular-file |
| 11 | `test_body_path_cap_and_content` | a file over the monkeypatched cap refuses with the total and cap named; at the cap the content rides stdin_text and the argv carries `--body -`; a file grown between the stat and the read refuses at the read with the same wording |
| 12 | `test_drafts_send_gated_flow` | confirm=false produces the preview envelope with `--dry-run` in the prepended region and `data.ids` present; confirm=true runs without `--dry-run`; the dry-run argv check refuses a dropped flag |
| 13 | `test_drafts_send_argv` | exact `mail drafts send -- <id>` |
| 14 | `test_drafts_send_forbidden_flags_sweep` | no argv carries `--send-at`, `--expires`, `--eo-password-file`, or `--eo-password-hint` |
| 15 | `test_reply_draft_forbidden_flags_sweep` | no argv carries `--to`, `--cc`, `--bcc`, `--attach`, `--attach-inline`, `--html`, `--eml`, `--from`, `--no-signature`, `--request-receipt`, `--no-quote`, or any delivery or EO flag; `--draft` always present |
| 16 | `test_drafts_create_forbidden_flags_sweep` | no argv carries `--cc`, `--bcc`, `--attach`, `--attach-inline`, `--html`, `--eml`, `--from`, `--no-signature`, or `--request-receipt` |
| 17 | `test_ungated_drafts_never_dry_run_sweep` | no argv for the four ungated actions carries `--dry-run` in any case |
| 18 | `test_drafts_envelope_passthrough` | the runner's `data` reaches the envelope unchanged for create, reply, send, list, and get |
| 19 | `test_drafts_list_validation` | a non-integer `limit` or `page`, a boolean `limit` or `page`, a non-boolean `desc`, and a `sort` outside {time, size} each refuse; no junk path emits argv; valid shapes pass |
| 20 | `test_proton_mail_schema_carries_drafts_actions_and_params` | the enum carries the five new actions; `drafts_send` sits in `gated` (confirm covers it, default false); the four reads do not; the six new params carry their documented types |
| 21 | `test_schema_string_updates_ridden` | the two shipped schema-string assertions §8's widening touches are updated to the new strings in the same PR (the `id` description literal and the `body` description literal), and this test pins the new literals |
| 22 | `test_body_path_read_refusals` | the vanished refusal is pinned for both sub-windows, vanish before the stat and vanish between the stat and the read, each refusing with the vanished wording as a structured error; a non-UTF-8 file refuses with a structured error; the CLI never runs |
| 23 | `test_body_and_everyone_shape_refusals` | an empty inline `body` refuses on both composing actions; a non-string `body_path` refuses; a zero-byte `body_path` file refuses with the empty-body wording; `everyone` refuses any non-boolean, truthy or falsy; the CLI never runs |

The enum-mirror test's expected set grows accordingly, and the shipped
exact-enum assertion in `test_proton_mail_schema_send_attach_param` grows
with it (forced by its own exact-set pin).

## 11. Rollout and open items

- Deploy needs the image rebuild + container recreation (the image changes,
  so `update.sh` recreates when the digest changed) and a gateway restart
  afterwards to refresh the schema; no quadlet edits (the spool mount and
  MemoryMax are already in place).
- **Open before merge:** the pre-merge contract audit: run `--help` for the
  five verbs on the deployed v5.1.0 binary (as the mcp user, no login
  needed) and diff each flag set against §1's table; any drift reopens the
  argv shapes. A `proton version` check settles which binary the deployed
  container mounts.
- **Open after deploy:** the smoke ladder, MCP-only from a fresh session,
  Drafts starting at 0: `drafts_list` (empty envelope); `drafts_create`
  with a marked-subject throwaway (expect the created envelope, record the
  id); `drafts_get` on that id (same subject, body as sent); `reply_draft`
  on one named received message (saved envelope, "Re:" subject, quoted
  original); `drafts_send` with `confirm=false` (preview naming the draft
  id) and, after explicit approval, `confirm=true` addressed to the
  owner's own mailbox (sent envelope; verify arrival with `get` on the
  returned id). Cleanup: trash the smoke mail (gated, recoverable) and,
  for any leftover drafts, the operator runs `mail drafts delete <id>
  --yes --output json --no-input` as the mcp user (Forever-class cleanup
  needs `--yes`).
- Known limits (accepted, disclosed): the cap bounds the accepted file
  size, not the joint wrapper+CLI memory peak (one body near 200 MiB risks
  an OOM kill inside MemoryMax=384M); the gate-to-open retarget window is
  disclosed, not engineered (the agent-authored tmpfs spool); a runner
  timeout can leave a half-built draft; exit codes 6 and 7 are unmapped in
  the runner (structured unknown-exit failures with the CLI's text in
  `raw`).
- Out of scope (future increments, flag sets verified present): repeatable
  `--to`/`--cc`/`--bcc` beyond one recipient, `--attach`/`--attach-inline`
  on create and reply, `--html`, `--eml`, `--from`, `--no-signature`,
  `--request-receipt`, `--no-quote`, the delivery flags (`--send-at`,
  `--expires`, EO passwords), conversation-level reply, `mail drafts
  update`, and a wrapper `drafts_delete` action.
