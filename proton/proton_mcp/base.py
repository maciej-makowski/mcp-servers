"""Shared tool-layer helpers, split out of tools.py to break a circular import.

photos.py needs _BaseTool/_result/_limit_error/_validation_error; tools.py
imports photos for PHOTOS_TOOL. Keeping the shared helpers here (importing
nothing from either module) makes every module importable directly:
base <- photos, base <- tools -> photos -> base (no cycle back into tools).

tools.py re-exports these names so `proton_mcp.tools._BaseTool` (etc.) keeps
working for existing callers.
"""

from __future__ import annotations

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


def _refs_error(refs) -> str | None:
    """Validate a refs parameter before it is iterated into CLI argv.

    I1: a bare string refs="ca_1" must never be iterated per-character (that
    once fed ['c','a','_','1'] to a PERMANENT delete). refs must be a list;
    each item must be a str or int (strs kept as-is, ints stringified).
    """
    if not isinstance(refs, list):
        return f"'refs' must be a list of ids, got {type(refs).__name__}"
    bad = [r for r in refs if not isinstance(r, (str, int)) or isinstance(r, bool)]
    if bad:
        return "'refs' items must be strings or integers, got: " + ", ".join(
            f"{type(r).__name__}" for r in bad[:3]
        )
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
