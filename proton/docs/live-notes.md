# Live-verified proton-cli notes (v5.1.0, real account, verified Oct 25 2026)

Findings from exercising the real CLI against a live Proton account (17k-message
archive, bulk moves/trash, category exploration). These complement the CLI
contract notes in the README — source review alone missed several of these.

## Hidden category labels (auto-categorization)

Proton's inbox tabs (Primary / Transactions / Updates / Promotions /
Newsletters / Socials) exist in the API as **system label IDs on the message**,
not as folders:

| Label ID | Category |
|---|---|
| `24` | Primary |
| `26` | Transactions |
| `22` | Updates |
| `21` | Promotions |
| `25` | Newsletters |
| `20` | Socials |

- Categories are **label overlays**: a categorized message keeps living in the
  inbox folder; the UI "hides" it from Primary by label. Mutually exclusive —
  a message carries exactly one category ID (verified: zero overlap in 800+
  inbox messages).
- They do **not** appear in `mail settings categories list` (returns empty) or
  in the CLI's folder listings — discover them only from message `labels`.
- `move --into` accepts a **tab name** (`Promotions`, `Transactions`, …) as
  destination ("a tab" per `--help`). Moving an already-categorized message
  into its current tab is a **no-op**; moving into another tab swaps the label.
- Practical bulk pattern: filter by folder + date/sender, `--into <TabName>`.

## ref-ID quirks (bulk actions)

- Message/folder IDs may **begin with `-`** (base64-ish). The CLI parses a
  leading `-` as a flag, so flags-first ordering is required:
  `proton mail messages trash --output json --no-input -- <id>`.
- Large multi-ref batches fail with **`HTTP 400 15225` ("did not understand
  your search")** — the ref list goes into a query that has a length cap.
  Chunk refs to **≤ ~5–10 per call** for reliable bulk trash/move/label.
- A bulk `move` with refs reports `count` of messages it actually moved;
  messages no longer matching (e.g. moved since the snapshot) are silently
  skipped — re-verify with a follow-up listing rather than trusting the
  planned count.

## Export (backup)

- `mail messages export` writes RFC 822 documents: `--format eml` (one file
  per message) or `mbox` (single stream); `--dest-dir DIR` or `--dest FILE`.
- `--no-attachments` makes large exports much faster.
- Exports are **plaintext and unencrypted**; DKIM/ARC headers no longer verify
  (Proton's own web export behaves the same). Full folder backup:
  `export --folder <folder> --all --format mbox --dest folder.mbox`.

## Misc

- No local mail index by default: `--keyword` searches subjects/names/
  addresses only. `proton index create mail` builds a body-searchable index.
- Rate/volume effects: several hundred API calls in quick succession (yearly
  bulk moves of ~2.4k messages) complete fine, but per-chunk errors should be
  retried rather than assumed fatal.
