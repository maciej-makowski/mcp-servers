"""MCP tool layer: dispatch tables per proton-cli tool family.

Gating rules (structural, enforced here):
- Gated (mutating) actions with confirm=False run the CLI with --dry-run and
  return a preview envelope; nothing is changed.
- Gated actions with confirm=True run for real (no --dry-run).
- Reads never get --dry-run.
- Bulk filter actions: default --limit 150 unless all=True or an explicit
  limit is given; all=True additionally requires confirm=True to apply.
- Belt-and-braces: if a dry-run was requested but --dry-run is missing from
  the argv the runner actually built, raise RuntimeError.
"""

from __future__ import annotations

from pathlib import Path

from . import runner

DEFAULT_BULK_LIMIT = 150

NEXT_STEP = "Nothing changed. Ask user to approve, then call again with confirm=true."

# Belt-and-braces: the LAST_ARGV check is post-hoc -- it reads the runner's
# recorded argv *after* subprocess.run has returned, never during. A
# single-loop (non-threaded) MCP server therefore never races on it; if the
# server ever gains a thread pool, this check needs its own lock instead.
#
# Boolean CLI flags: read/unread/starred/has-attachments (and --all) are bare
# switches per `proton mail messages list --help` -- they take no value, so
# booleans are emitted as flag-present (a False value omits the flag), never
# as `--flag True`.
FILTER_FLAG_MAP = {
    "folder": "--folder",
    "from": "--from",
    "to": "--to",
    "subject": "--subject",
    "keyword": "--keyword",
    "after": "--after",
    "before": "--before",
    "older_than": "--older-than",
    "newer_than": "--newer-than",
    "limit": "--limit",
    "all": "--all",
}

# Bare-switch flags: emitted when truthy, omitted when falsy/None.
BOOLEAN_FILTER_FLAG_MAP = {
    "has_attachments": "--has-attachments",
    "read": "--read",
    "unread": "--unread",
    "starred": "--starred",
}

# Keys of a filter object (limit/all handled separately by bulk rails).
FILTER_ONLY_KEYS = [k for k in FILTER_FLAG_MAP if k not in ("limit", "all")]


def _map_filter(f: dict) -> list[str]:
    """Map a filter object's keys to CLI flags (snake_case -> kebab)."""
    argv: list[str] = []
    for key in FILTER_ONLY_KEYS:
        if key in f and f[key] is not None:
            argv += [FILTER_FLAG_MAP[key], str(f[key])]
    for key, flag in BOOLEAN_FILTER_FLAG_MAP.items():
        if f.get(key):
            argv.append(flag)  # bare switch: no value
    return argv


def _limit_error(f: dict) -> str | None:
    """Validate the 'limit' rail: must be a plain int (bool excluded) or absent."""
    limit = f.get("limit")
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int):
        return f"'limit' must be an integer, got {type(limit).__name__}"
    return None


def _result(data: dict, **extra) -> dict:
    out = dict(data)
    out.update(extra)
    return out


DRY_RUN_FAILED_NEXT_STEP = (
    "Nothing changed. The dry-run failed: fix the cause (see preview) before retrying."
)

VALIDATION_FAILED_NEXT_STEP = (
    "Nothing changed and the CLI was never run: local validation failed. "
    "Fix the parameters (see preview), then call again."
)


def _is_validation_error(res: dict) -> bool:
    """Local (pre-CLI) validation failure, tagged by _validation_error."""
    return res.get("validation") is True


def _dry_run_envelope(action: str, preview: dict) -> dict:
    if _is_validation_error(preview):
        return {
            "success": False,
            "applied": False,
            "dry_run": True,
            "action": action,
            "preview": preview,
            "next_step": VALIDATION_FAILED_NEXT_STEP,
        }
    failed = not preview.get("success", False)
    return {
        "success": preview.get("success", False),
        "applied": False,
        "dry_run": True,
        "action": action,
        "preview": preview,
        "next_step": DRY_RUN_FAILED_NEXT_STEP if failed else NEXT_STEP,
    }


def _affected_count(data) -> int | None:
    """Pull an affected/count value from the CLI's JSON output, if present."""
    if not isinstance(data, dict):
        return None
    for key in ("affected", "affected_count", "count", "moved", "labeled", "trashed", "deleted"):
        if key in data and isinstance(data[key], (int, float)):
            return int(data[key])
    return None


class _BaseTool:
    GATED_ACTIONS: frozenset[str] = frozenset()

    def dispatch(self, action: str, params: dict | None = None, confirm: bool | None = None) -> dict:
        params = dict(params or {})
        if confirm is not None:
            params["confirm"] = confirm
        if action not in self.GATED_ACTIONS:
            return self._read(action, params)
        confirm = bool(params.get("confirm", False))
        if confirm:
            res = self._mutate(action, params)
            return _result(
                res,
                applied=res.get("success", False),
                dry_run=False,
                action=action,
            )
        res = self._mutate(action, params, dry_run=True)
        return _dry_run_envelope(action, res)

    # -- internals overridden per family -------------------------------------

    def _read(self, action: str, params: dict) -> dict:  # pragma: no cover - abstract
        raise ValueError(f"unknown action {action!r} for {type(self).__name__}")

    def _mutate(self, action: str, params: dict, dry_run: bool = False) -> dict:  # pragma: no cover
        raise ValueError(f"unknown action {action!r} for {type(self).__name__}")

    # -- shared helpers -------------------------------------------------------

    def _run(self, args: list[str], dry_run: bool = False, stdin_text: str | None = None) -> dict:
        res = runner.run(args, stdin_text=stdin_text, dry_run=dry_run)
        if dry_run and (runner.LAST_ARGV or [])[-1:] != ["--dry-run"]:
            raise RuntimeError(
                "runner dropped --dry-run; refusing to proceed (belt-and-braces check)."
            )
        return res

    def _validation_error(self, msg: str) -> dict:
        # Tagged so the dry-run envelope can word next_step correctly
        # (validation failed, CLI never ran).
        return {"success": False, "validation": True, "error": msg, "exit_code": None, "auth_required": False}


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
        raise ValueError(f"unknown read action {action!r} for mail")

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
            if not params.get("id"):
                return self._validation_error("events_delete requires 'id'")
            return self._run(["calendar", "events", "delete", str(params["id"])], dry_run=dry_run)
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
            if not params.get("id"):
                return self._validation_error("get requires 'id'")
            return self._run(["drive", "items", "get", str(params["id"])])
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
            # never issued with --dry-run, so no belt-and-braces check.
            return self._run(args)
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
            return self._run(args, dry_run=dry_run)
        if action == "trash":
            refs = params.get("refs") or ([params["id"]] if params.get("id") else None)
            if not refs:
                return self._validation_error("trash requires 'refs' (list of ids) or 'id'")
            return self._run(["drive", "items", "trash", *[str(r) for r in refs]], dry_run=dry_run)
        raise ValueError(f"unknown gated action {action!r} for drive")


MAIL_TOOL = MailTool()
CALENDAR_TOOL = CalendarTool()
DRIVE_TOOL = DriveTool()
