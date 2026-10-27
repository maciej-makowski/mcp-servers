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
