"""MCP tool layer: dispatch tables per proton-cli tool family.

Gating rules (structural, enforced here):
- Gated (mutating) actions with confirm=False run the CLI with --dry-run and
  return a preview envelope; nothing is changed.
- Gated actions with confirm=True run for real (no --dry-run).
- Reads never get --dry-run. Downloads are reads: they write only into a
  caller-named directory and touch no account state.
- Bulk filter actions: default --limit 150 unless all=True or an explicit
  limit is given; all=True additionally requires confirm=True to apply.
- Dry-run argv check: if a gated call requested --dry-run but the argv the
  runner actually built does not lead with the prepended global-flags region
  including --dry-run, raise RuntimeError.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import runner
from .base import (
    DEFAULT_BULK_LIMIT,
    DRY_RUN_FAILED_NEXT_STEP,
    NEXT_STEP,
    VALIDATION_FAILED_NEXT_STEP,
    BOOLEAN_FILTER_FLAG_MAP,
    FILTER_FLAG_MAP,
    FILTER_ONLY_KEYS,
    _BaseTool,
    _affected_count,
    _dry_run_envelope,
    _is_validation_error,
    _limit_error,
    _map_filter,
    _refs_error,
    _result,
)

# photos imports helpers from .base (not from this module), so importing it
# here is safe — no circular import.
from .photos import PHOTOS_TOOL  # noqa: F401  (re-exported)

__all__ = [
    "DEFAULT_ATTACHMENT_DEST_DIR",
    "DEFAULT_BULK_LIMIT",
    "NEXT_STEP",
    "DRY_RUN_FAILED_NEXT_STEP",
    "VALIDATION_FAILED_NEXT_STEP",
    "FILTER_FLAG_MAP",
    "BOOLEAN_FILTER_FLAG_MAP",
    "FILTER_ONLY_KEYS",
    "_map_filter",
    "_limit_error",
    "_result",
    "_is_validation_error",
    "_dry_run_envelope",
    "_affected_count",
    "_refs_error",
    "_BaseTool",
    "MailTool",
    "CalendarTool",
    "DriveTool",
    "PHOTOS_TOOL",
    "MAIL_TOOL",
    "CALENDAR_TOOL",
    "DRIVE_TOOL",
]

# Attachment downloads default to the shared spool receive dir; the constant
# is a module global read at call time, so tests can point it at a tmp path.
DEFAULT_ATTACHMENT_DEST_DIR = "/srv/mcp_files/receive"

# The CLI's private temp pattern inside a destination dir (kit.destination.go:
# commit() writes a 0600 temp then renames). Excluded from the snapshot and
# the diff — a CLI killed mid-write leaves such residue, never a download.
_ATTACHMENT_TEMP_PREFIX = ".proton-cli-"


def _snapshot_dir(dest_dir: str) -> dict[str, tuple[int, int]]:
    """Snapshot {name: (size, mtime_ns)} for regular files, skipping temps."""
    snap: dict[str, tuple[int, int]] = {}
    try:
        entries = list(os.scandir(dest_dir))
    except OSError:
        return snap
    for entry in entries:
        try:
            if not entry.is_file() or entry.name.startswith(_ATTACHMENT_TEMP_PREFIX):
                continue
            st = entry.stat()
        except OSError:
            continue
        snap[entry.name] = (st.st_size, st.st_mtime_ns)
    return snap


def _diff_written(dest_dir: str, before: dict[str, tuple[int, int]]) -> tuple[list[dict], list[str]]:
    """Diff the destination dir against `before`.

    A file is "written" when it is new, or its size or mtime_ns changed.
    Returns (files entries, written names): entries carry name and size as on
    disk (post-SafeFilename); an entry whose file already existed and was
    rewritten carries `changed: True` (a --force overwrite), absent for new
    files. `.proton-cli-*` residue is excluded here as well as in the
    snapshot, so a killed CLI's temp file is never reported or chmod'd.
    """
    files: list[dict] = []
    written: list[str] = []
    try:
        entries = sorted(os.scandir(dest_dir), key=lambda e: e.name)
    except OSError:
        return files, written
    for entry in entries:
        try:
            if not entry.is_file() or entry.name.startswith(_ATTACHMENT_TEMP_PREFIX):
                continue
            st = entry.stat()
        except OSError:
            continue
        old = before.get(entry.name)
        if old is None:
            entry_info: dict = {"name": entry.name, "size": st.st_size}
        elif (st.st_size, st.st_mtime_ns) != old:
            entry_info = {"name": entry.name, "size": st.st_size, "changed": True}
        else:
            continue
        files.append(entry_info)
        written.append(entry.name)
    return files, written


def _chmod_0644(dest_dir: str, names: list[str]) -> None:
    """chmod each written file to 0644, best effort (spool read-back needs it:
    the CLI's commit() lands files 0600). One failing file never fails the
    download."""
    for name in names:
        try:
            os.chmod(os.path.join(dest_dir, name), 0o644)
        except OSError:
            pass


class MailTool(_BaseTool):
    """Actions on `proton mail`."""

    GATED_ACTIONS = frozenset({"send", "move", "label", "unlabel", "mark", "trash"})

    def _read(self, action: str, params: dict) -> dict:
        if action == "status":
            # `mail status` doesn't exist in the CLI; remap to
            # `mail messages count` (per-folder/label counts). `count`
            # only supports --folder, so map just that filter key.
            args = ["mail", "messages", "count"]
            folder = (params.get("filter") or {}).get("folder")
            if folder is not None:
                args += ["--folder", str(folder)]
            return self._run(args)
        if action == "list":
            args = ["mail", "messages", "list"]
            if "filter" in params:
                err = _limit_error(params["filter"])
                if err:
                    return self._validation_error(err)
                args += _map_filter(params["filter"])
            return self._run(args)
        if action == "search":
            # The CLI has no `search` verb; its search mechanism is
            # `mail messages list --keyword <query>`. Keep the action
            # name for callers, emit list argv, pass other filter
            # flags through unchanged.
            query = params.get("query")
            if not query:
                return self._validation_error("search requires 'query'")
            args = ["mail", "messages", "list", "--keyword", str(query)]
            if "filter" in params:
                err = _limit_error(params["filter"])
                if err:
                    return self._validation_error(err)
                args += _map_filter(params["filter"])
            return self._run(args)
        if action == "get":
            if not params.get("id"):
                return self._validation_error("get requires 'id'")
            return self._run(["mail", "messages", "get", str(params["id"])])
        if action == "attachments_list":
            return self._attachment_list(
                action, ["mail", "messages", "attachments", "list"], params
            )
        if action == "conversation_attachments_list":
            return self._attachment_list(
                action, ["mail", "conversations", "attachments", "list"], params
            )
        if action == "attachments_download":
            return self._attachment_download(
                action, ["mail", "messages", "attachments", "download"], params
            )
        if action == "conversation_attachments_download":
            return self._attachment_download(
                action, ["mail", "conversations", "attachments", "download"], params
            )
        raise ValueError(f"unknown read action {action!r} for mail")

    def _attachment_list(self, action: str, head: list[str], params: dict) -> dict:
        """Attachment list is a passthrough read: flags first, then `--`, then
        the reference positional (the `--` marks the wrapper's own data for
        every ref shape — dash-ids included; the runner's global flags are
        prepended before the verb argv). No transformation of the envelope."""
        ref = params.get("id")
        if not isinstance(ref, str) or not ref:
            return self._validation_error(
                f"{action} requires 'id' (message or conversation reference as a string)"
            )
        args = list(head)
        if params.get("include_inline"):
            args.append("--include-inline")
        args.append("--")
        args.append(ref)
        return self._run(args)

    def _attachment_download(self, action: str, head: list[str], params: dict) -> dict:
        """Attachment download: a read that writes into a caller-named
        directory (default: the shared spool receive dir). Never --dry-run,
        never --dest (single named file or stdout); the CLI's JSON result
        never names written files, so the wrapper derives them itself from a
        before/after directory diff and chmods each to 0644 for read-back."""
        ref = params.get("id")
        if not isinstance(ref, str) or not ref:
            return self._validation_error(
                f"{action} requires 'id' (message or conversation reference as a string)"
            )
        attachment = params.get("attachment")
        if attachment is not None and (not isinstance(attachment, str) or not attachment):
            return self._validation_error(
                f"{action}: 'attachment' must be a non-empty string (attachment id "
                f"or name), got {type(attachment).__name__}"
            )
        dest_dir = params.get("dest_dir")
        if dest_dir is not None and not isinstance(dest_dir, str):
            return self._validation_error(
                f"{action}: 'dest_dir' must be a string, got {type(dest_dir).__name__}"
            )
        if not dest_dir:
            dest_dir = DEFAULT_ATTACHMENT_DEST_DIR
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            return self._validation_error(
                f"{action}: cannot create dest_dir {dest_dir!r}: {exc}"
            )

        args = [*head, "--dest-dir", dest_dir]
        if params.get("force"):
            args.append("--force")
        # When one attachment is selected the CLI downloads exactly that one;
        # --include-inline is its download-them-all filter and is omitted
        # regardless of the parameter.
        if params.get("include_inline") and attachment is None:
            args.append("--include-inline")
        # Mark the wrapper's own data: every positional sits after the `--`,
        # so dash-ids and dash-leading names stay data (the CLI's own
        # shape-guessing never runs on these calls).
        args.append("--")
        args.append(ref)
        if attachment is not None:
            args.append(attachment)

        before = _snapshot_dir(dest_dir)
        res = self._run(args, timeout=300)
        files, written = _diff_written(dest_dir, before)
        _chmod_0644(dest_dir, written)

        out = dict(res)
        out["dest_dir"] = dest_dir
        out["files"] = files
        out["count"] = len(files)
        data = res.get("data")
        if (
            res.get("success")
            and isinstance(data, dict)
            and isinstance(data.get("count"), (int, float))
            and int(data["count"]) != len(files)
        ):
            # Direction-neutral: a concurrent spool writer inflates `files`,
            # a concurrent deletion between write and diff deflates it. The
            # CLI's own count stays visible as data.count (advisory).
            out["note"] = "the number of files differs from the CLI's reported count"
        return out

    def _mutate(self, action: str, params: dict, dry_run: bool = False) -> dict:
        if action == "send":
            to, subject, body = params.get("to"), params.get("subject"), params.get("body")
            if not to or subject is None:
                return self._validation_error("send requires 'to' and 'subject'")
            stdin_text = body if body is not None else ""
            args = ["mail", "messages", "send", "--to", str(to), "--subject", str(subject)]
            return self._run(args, dry_run=dry_run, stdin_text=stdin_text)

        # Bulk actions: explicit refs OR a filter scope, never both.
        refs = params.get("refs")
        filt = params.get("filter") or {}
        if refs and filt:
            return self._validation_error(
                f"{action} accepts either 'refs' or a 'filter' scope, not both "
                "(combine them by narrowing the filter instead)"
            )
        if not refs and not filt:
            return self._validation_error(
                f"{action} requires either 'refs' (list of message ids) or a 'filter' object"
            )
        if refs:
            # I1: reject non-list refs (a string would be iterated
            # per-character and act on garbage ids).
            err = _refs_error(refs)
            if err:
                return self._validation_error(f"{action}: {err}")
        err = _limit_error(filt)
        if err:
            return self._validation_error(err)

        if action == "move":
            into = params.get("into")
            if not into:
                return self._validation_error("move requires 'into' (destination folder)")
        elif action in ("label", "unlabel"):
            if not params.get("label"):
                return self._validation_error(f"{action} requires 'label'")
        elif action == "mark":
            # Explicit semantics: mark read only when params["read"] is True;
            # mark unread when params["unread"] is True (this includes
            # read=False + unread=True). Both False / neither True -> error,
            # so truthiness can never silently pick a direction.
            if params.get("read") is not True and params.get("unread") is not True:
                return self._validation_error("mark requires 'read': true or 'unread': true")
        elif action != "trash":
            raise ValueError(f"unknown gated action {action!r} for mail")

        argv_extra: list[str] = []
        if action == "move":
            argv_extra = ["--into", str(into)]
        elif action == "label":
            argv_extra = ["--label", str(params["label"])]
        elif action == "unlabel":
            argv_extra = ["--label", str(params["label"])]
        elif action == "mark":
            argv_extra = ["read" if params.get("read") is True else "unread"]

        args = ["mail", "messages", action]
        if action == "mark":
            args += argv_extra
        if refs:
            args += [str(r) for r in refs]
            if action != "mark":
                args += argv_extra
        filter_echo = None
        if filt:
            args += _map_filter(filt)
            filter_echo = {
                k: filt[k] for k in FILTER_ONLY_KEYS if k in filt
            }
            # Echo the boolean filters that were actually applied (truthy).
            filter_echo.update(
                {k: filt[k] for k in BOOLEAN_FILTER_FLAG_MAP if filt.get(k)}
            )
            args += self._bulk_rails(filt)
            if action == "move":
                args += ["--into", str(into)]

        res = self._run(args, dry_run=dry_run)
        out = dict(res)
        out["refs"] = refs
        out["filter"] = filter_echo
        out["affected_count"] = _affected_count(res.get("data"))
        return out

    @staticmethod
    def _bulk_rails(filt: dict) -> list[str]:
        """Bulk rails: enforce --limit 150 unless all=True or explicit limit.

        An explicit limit of None is treated as absent (rails to the default);
        only an integer limit skips the rail.
        """
        args: list[str] = []
        if filt.get("all"):
            args.append("--all")
        elif filt.get("limit") is None:
            args += ["--limit", str(DEFAULT_BULK_LIMIT)]
        else:
            args += ["--limit", str(filt["limit"])]
        return args


class CalendarTool(_BaseTool):
    """Actions on `proton calendar`."""

    GATED_ACTIONS = frozenset({"events_create", "events_delete"})

    def _read(self, action: str, params: dict) -> dict:
        if action == "calendars":
            # Real CLI tree (verified against `--help`): calendars are listed
            # under `calendar settings calendars list` — there is NO
            # `proton calendar calendars` command.
            args = ["calendar", "settings", "calendars", "list"]
            limit = params.get("limit")
            if limit is not None:
                err = _limit_error({"limit": limit})
                if err:
                    return self._validation_error(err)
                args += ["--limit", str(limit)]
            if params.get("page") is not None:
                args += ["--page", str(params["page"])]
            return self._run(args)
        if action == "events_list":
            args = ["calendar", "events", "list"]
            # Verified flags: --after --before --calendar --keyword --limit --page
            for key, flag in (
                ("after", "--after"),
                ("before", "--before"),
                ("calendar", "--calendar"),
                ("keyword", "--keyword"),
                ("page", "--page"),
            ):
                if params.get(key) is not None:
                    args += [flag, str(params[key])]
            limit = params.get("limit")
            if limit is not None:
                err = _limit_error({"limit": limit})
                if err:
                    return self._validation_error(err)
                args += ["--limit", str(limit)]
            return self._run(args)
        if action == "events_get":
            if not params.get("id"):
                return self._validation_error("events_get requires 'id'")
            return self._run(["calendar", "events", "get", str(params["id"])])
        raise ValueError(f"unknown read action {action!r} for calendar")

    def _mutate(self, action: str, params: dict, dry_run: bool = False) -> dict:
        if action == "events_create":
            for req in ("title", "start", "end"):
                if not params.get(req):
                    return self._validation_error(f"events_create requires '{req}'")
            args = [
                "calendar",
                "events",
                "create",
                "--title",
                str(params["title"]),
                "--start",
                str(params["start"]),
                "--end",
                str(params["end"]),
            ]
            if params.get("calendar"):
                args += ["--calendar", str(params["calendar"])]
            if params.get("description"):
                args += ["--description", str(params["description"])]
            return self._run(args, dry_run=dry_run)
        if action == "events_delete":
            eid = params.get("id")
            if not eid:
                return self._validation_error("events_delete requires 'id'")
            # I1: the CLI takes REF... (space-separated ids); a list here
            # would either TypeError or be stringified wrong — reject it
            # before any CLI run.
            if not isinstance(eid, (str, int)) or isinstance(eid, bool):
                return self._validation_error(
                    f"events_delete 'id' must be a string or integer, got {type(eid).__name__}"
                )
            return self._run(["calendar", "events", "delete", str(eid)], dry_run=dry_run)
        raise ValueError(f"unknown gated action {action!r} for calendar")


class DriveTool(_BaseTool):
    """Actions on `proton drive`."""

    GATED_ACTIONS = frozenset({"upload", "trash"})

    def _read(self, action: str, params: dict) -> dict:
        if action == "list":
            args = ["drive", "items", "list"]
            if params.get("dir"):
                # CLI takes the drive folder as a positional PATH, not a flag.
                args.append(str(params["dir"]))
            return self._run(args)
        if action == "get":
            # The CLI's `drive items get` resolves its argument as a PATH,
            # not a link id. Accept either key name ('id' kept for callers
            # written against the old schema).
            target = params.get("path") or params.get("id")
            if not target:
                return self._validation_error(
                    "get requires 'id' or 'path' (drive item path)"
                )
            return self._run(["drive", "items", "get", str(target)])
        if action == "download":
            path = params.get("path")
            if not path:
                return self._validation_error("download requires 'path' (drive item path)")
            dest_dir = params.get("dest_dir")
            if not dest_dir:
                return self._validation_error("download requires 'dest_dir' (local directory)")
            args = ["drive", "items", "download", str(path), "--dest-dir", str(dest_dir)]
            if params.get("recursive"):
                args.append("--recursive")
            if params.get("force"):
                args.append("--force")
            # Download is a read (writes only into the caller-named dir):
            # never issued with --dry-run, so no dry-run argv check.
            res = self._run(args)
            # Shared-spool use: the CLI's in-container umask writes single-file
            # downloads as 0600, which locks the agent out of receive-dir
            # copies (live-verified Nov 5). Relax the single-file target to
            # 0644, best-effort — a chmod failure must not fail a download.
            # Recursive trees are left untouched (modes vary by content).
            try:
                if not params.get("recursive"):
                    target = Path(dest_dir) / Path(str(path)).name
                    if target.is_file():
                        target.chmod(0o644)
            except OSError:
                pass
            return res
        raise ValueError(f"unknown read action {action!r} for drive")

    def _mutate(self, action: str, params: dict, dry_run: bool = False) -> dict:
        if action == "upload":
            path = params.get("path")
            if not path:
                return self._validation_error("upload requires 'path' (local file)")
            if not Path(path).exists():
                return self._validation_error(f"local file not found: {path}")
            args = ["drive", "items", "upload", str(path)]
            if params.get("dir"):
                args.append(str(params["dir"]))  # CLI: positional DEST
            if_exists = params.get("if_exists")
            if if_exists is not None:
                if if_exists not in ("replace", "rename", "skip"):
                    return self._validation_error(
                        "upload 'if_exists' must be one of: replace, rename, skip"
                    )
                args += ["--if-exists", str(if_exists)]
            return self._run(args, dry_run=dry_run)
        if action == "trash":
            refs = params.get("refs") or ([params["id"]] if params.get("id") else None)
            if not refs:
                return self._validation_error("trash requires 'refs' (list of ids) or 'id'")
            # I1: reject non-list refs (a string would be iterated
            # per-character and act on garbage ids).
            err = _refs_error(refs)
            if err:
                return self._validation_error(f"trash: {err}")
            return self._run(["drive", "items", "trash", *[str(r) for r in refs]], dry_run=dry_run)
        raise ValueError(f"unknown gated action {action!r} for drive")


MAIL_TOOL = MailTool()
CALENDAR_TOOL = CalendarTool()
DRIVE_TOOL = DriveTool()
