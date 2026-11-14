"""Tests for the MCP tools layer (all mocked subprocess, no network, no real proton)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from proton_mcp import runner, tools

NEXT_STEP = "Nothing changed. Ask user to approve, then call again with confirm=true."


def _with_real_upload(tmp_path, params: dict) -> dict:
    """Upload needs a real local file; substitute a temp path when requested."""
    params = dict(params)
    if params.get("path") and not Path(params["path"]).exists():
        f = tmp_path / "f.txt"
        f.write_text("x")
        params["path"] = str(f)
    return params


def make_proc(stdout: str = '{"ok": true}', returncode: int = 0):
    from types import SimpleNamespace

    return SimpleNamespace(
        stdout=stdout.encode(), stderr=b"", returncode=returncode
    )


@pytest.fixture
def rec(monkeypatch, tmp_path):
    """Recorder over subprocess.run with a fake PROTON_BIN."""
    fake_bin = tmp_path / "proton"
    fake_bin.write_text("#!/bin/sh\ntrue\n")
    monkeypatch.setattr(runner, "PROTON_BIN", fake_bin)
    calls = {"argv": []}

    def fake_run(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        calls["argv"] = list(args)
        calls["input"] = input
        calls["timeout"] = timeout
        return calls.get("proc", make_proc())

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def tail_without_globals(argv: list[str]) -> list[str]:
    """Strip the runner's PREPENDED region — binary path, --output json
    --no-input, and --dry-run for gated calls — and return the verb argv."""
    assert argv[:4] == [str(runner.PROTON_BIN), "--output", "json", "--no-input"]
    argv = list(argv)[4:]
    if argv and argv[0] == "--dry-run":
        argv = argv[1:]
    return argv


def proc_json(payload: dict):
    import json

    return make_proc(stdout=json.dumps(payload))


# --- filter flag mapping ------------------------------------------------------


def test_filter_flag_mapping_snake_to_kebab(rec):
    tools.MAIL_TOOL.dispatch(
        "list", {"filter": {"has_attachments": True, "older_than": "30d"}}
    )
    args = tail_without_globals(rec["argv"])
    assert "--has-attachments" in args
    assert "--older-than" in args
    assert "30d" in args
    assert "--has_attachments" not in args
    assert "--older_than" not in args


def test_filter_keys_cover_contract(rec):
    f = {
        "folder": "Inbox",
        "from": "a@b.c",
        "to": "d@e.f",
        "subject": "hi",
        "keyword": "kw",
        "has_attachments": True,
        "read": True,
        "unread": True,
        "starred": True,
        "after": "2026-01-01",
        "before": "2026-02-01",
        "older_than": "2w",
        "newer_than": "1h",
    }
    tools.MAIL_TOOL.dispatch("list", {"filter": f})
    args = tail_without_globals(rec["argv"])
    for flag in [
        "--folder",
        "--from",
        "--to",
        "--subject",
        "--keyword",
        "--has-attachments",
        "--read",
        "--unread",
        "--starred",
        "--after",
        "--before",
        "--older-than",
        "--newer-than",
    ]:
        assert flag in args, flag


# --- reads: never --dry-run ----------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        (tools.MAIL_TOOL, "status", {}),
        (tools.MAIL_TOOL, "list", {}),
        (tools.MAIL_TOOL, "search", {"query": "x"}),
        (tools.MAIL_TOOL, "get", {"id": "42"}),
        (tools.CALENDAR_TOOL, "calendars", {}),
        (tools.CALENDAR_TOOL, "events_list", {}),
        (tools.CALENDAR_TOOL, "events_get", {"id": "42"}),
        (tools.DRIVE_TOOL, "list", {}),
        (tools.DRIVE_TOOL, "get", {"id": "42"}),
    ],
)
def test_reads_never_dry_run(rec, call):
    tool, action, params = call
    r = tool.dispatch(action, params)
    assert r["success"] is True
    assert "--dry-run" not in rec["argv"]
    assert runner.LAST_ARGV == rec["argv"]


# --- status/search remap to real CLI verbs --------------------------------------


def test_status_remaps_to_mail_messages_count(rec):
    """`status` doesn't exist in the CLI; remap to `mail messages count`."""
    tools.MAIL_TOOL.dispatch("status", {})
    assert tail_without_globals(rec["argv"]) == ["mail", "messages", "count"]


def test_status_with_folder_filter_maps_to_count_folder(rec):
    tools.MAIL_TOOL.dispatch("status", {"filter": {"folder": "Receipts"}})
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["mail", "messages", "count"]
    assert ["--folder", "Receipts"] == args[3:]


def test_status_ignores_non_folder_filter_keys(rec):
    """count only supports --folder; other filter keys don't apply."""
    tools.MAIL_TOOL.dispatch("status", {"filter": {"folder": "Inbox", "limit": 5}})
    assert tail_without_globals(rec["argv"]) == [
        "mail",
        "messages",
        "count",
        "--folder",
        "Inbox",
    ]


def test_search_remaps_to_list_keyword(rec):
    """The CLI has no `search` verb; search is `mail messages list --keyword`."""
    tools.MAIL_TOOL.dispatch("search", {"query": "invoice"})
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["mail", "messages", "list"]
    assert "--keyword" in args
    assert "invoice" in args


def test_search_requires_query(rec):
    r = tools.MAIL_TOOL.dispatch("search", {})
    assert r["success"] is False
    assert r["validation"] is True
    assert "query" in r["error"]


def test_search_with_filter_passthrough(rec):
    """Filter flags pass through verbatim (limit/all are bulk rails, not
    read filters, so they are not forwarded here)."""
    tools.MAIL_TOOL.dispatch(
        "search", {"query": "invoice", "filter": {"folder": "all", "limit": 50}}
    )
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["mail", "messages", "list"]
    assert ["--keyword", "invoice", "--folder", "all"] == args[3:]


def test_read_with_confirm_param_ignores_it(rec):
    """Reads must not gain --dry-run even if a stray confirm key is present."""
    tools.MAIL_TOOL.dispatch("list", {"confirm": False})
    assert "--dry-run" not in rec["argv"]


# --- gating: confirm=False -> dry_run envelope ---------------------------------


@pytest.mark.parametrize(
    "tool,action,params",
    [
        (tools.MAIL_TOOL, "send", {"to": "x@y.z", "subject": "s", "body": "b"}),
        (tools.MAIL_TOOL, "move", {"refs": ["1"], "into": "Archive"}),
        (tools.MAIL_TOOL, "label", {"refs": ["1"], "label": "Work"}),
        (tools.MAIL_TOOL, "unlabel", {"refs": ["1"], "label": "Work"}),
        (tools.MAIL_TOOL, "mark", {"refs": ["1"], "read": True}),
        (tools.MAIL_TOOL, "trash", {"refs": ["1"]}),
        (tools.CALENDAR_TOOL, "events_create", {"title": "t", "start": "s", "end": "e"}),
        (tools.CALENDAR_TOOL, "events_delete", {"id": "42"}),
        (tools.DRIVE_TOOL, "upload", {"path": "/tmp/f.txt", "dir": "/dir"}),
        (tools.DRIVE_TOOL, "trash", {"refs": ["42"]}),
    ],
)
def test_gated_dry_run_envelope(rec, tmp_path, tool, action, params):
    rec["proc"] = proc_json({"affected": 1})
    params = _with_real_upload(tmp_path, params)
    r = tool.dispatch(action, dict(params, confirm=False))
    assert r["success"] is True
    assert r["applied"] is False
    assert r["dry_run"] is True
    assert r["action"] == action
    assert r["next_step"] == NEXT_STEP
    assert r["preview"] is not None
    assert "--dry-run" in rec["argv"]
    # Belt-and-braces: runner must not have dropped the flag.
    assert "--dry-run" in runner.LAST_ARGV


@pytest.mark.parametrize(
    "tool,action,params",
    [
        (tools.MAIL_TOOL, "send", {"to": "x@y.z", "subject": "s", "body": "b"}),
        (tools.MAIL_TOOL, "trash", {"refs": ["1"]}),
        (tools.CALENDAR_TOOL, "events_delete", {"id": "42"}),
        (tools.DRIVE_TOOL, "upload", {"path": "/tmp/f.txt", "dir": "/dir"}),
    ],
)
def test_gated_confirm_true_real_run(rec, tmp_path, tool, action, params):
    rec["proc"] = proc_json({"affected": 1})
    params = _with_real_upload(tmp_path, params)
    r = tool.dispatch(action, dict(params, confirm=True))
    assert "--dry-run" not in rec["argv"]
    assert r["applied"] is True
    assert r["dry_run"] is False


# --- dry-run argv check: RuntimeError if the runner drops --dry-run -------------


def test_dry_run_argv_check_runtime_error_if_flag_dropped(rec, monkeypatch):
    """The check asserts the runner's prepended region as a whole prefix
    ([bin, --output, json, --no-input, --dry-run]); a runner that drops the
    flag breaks the prefix and the call refuses."""
    def fake_run(args, **kw):
        # Simulate a runner that drops --dry-run from its recorded argv too.
        if "--dry-run" in args:
            args = [a for a in args if a != "--dry-run"]
            runner.LAST_ARGV = list(args)
        rec["argv"] = list(args)
        return make_proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": False})


# --- bulk rails -----------------------------------------------------------------


def test_bulk_explicit_refs_no_limit_injection(rec):
    rec["proc"] = proc_json({"affected": 2})
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1", "2"], "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert "trash" in args and "1" in args and "2" in args
    assert "--limit" not in args
    assert r["applied"] is True
    assert r["affected_count"] == 2
    assert r["refs"] == ["1", "2"]


def test_bulk_filter_limit_default_150(rec):
    rec["proc"] = proc_json({"affected": 150})
    tools.MAIL_TOOL.dispatch(
        "trash", {"filter": {"subject": "spam"}, "confirm": True}
    )
    args = tail_without_globals(rec["argv"])
    i = args.index("--limit")
    assert args[i + 1] == "150"


def test_bulk_filter_explicit_limit_respected(rec):
    tools.MAIL_TOOL.dispatch(
        "trash", {"filter": {"limit": 5, "subject": "x"}, "confirm": True}
    )
    args = tail_without_globals(rec["argv"])
    i = args.index("--limit")
    assert args[i + 1] == "5"
    assert args.count("--limit") == 1


def test_bulk_all_requires_confirm(rec):
    rec["proc"] = proc_json({"affected": 999})
    r = tools.MAIL_TOOL.dispatch("trash", {"filter": {"all": True}, "confirm": False})
    assert r["applied"] is False and r["dry_run"] is True
    assert "--dry-run" in rec["argv"]

    r2 = tools.MAIL_TOOL.dispatch("trash", {"filter": {"all": True}, "confirm": True})
    assert "--dry-run" not in rec["argv"]
    assert r2["applied"] is True


def test_bulk_filter_echo_and_affected_count(rec):
    rec["proc"] = proc_json({"count": 7})
    r = tools.MAIL_TOOL.dispatch(
        "move", {"filter": {"folder": "Inbox", "older_than": "30d"}, "into": "Archive", "confirm": True}
    )
    assert r["applied"] is True
    assert r["affected_count"] == 7
    assert r["filter"] == {"folder": "Inbox", "older_than": "30d"}


def test_bulk_affected_count_absent_when_cli_omits_it(rec):
    rec["proc"] = make_proc(stdout='{"ok": true}')
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": True})
    assert r["affected_count"] is None


def test_bulk_dry_run_also_respects_limit_default(rec):
    tools.MAIL_TOOL.dispatch("trash", {"filter": {"subject": "x"}, "confirm": False})
    args = tail_without_globals(rec["argv"])
    i = args.index("--limit")
    assert args[i + 1] == "150"


# --- validation errors -----------------------------------------------------------


def test_move_requires_into(rec):
    r = tools.MAIL_TOOL.dispatch("move", {"refs": ["1"], "confirm": True})
    assert r["success"] is False
    assert "into" in r["error"]
    assert rec["argv"] == []  # never invoked the CLI


def test_label_requires_label(rec):
    r = tools.MAIL_TOOL.dispatch("label", {"refs": ["1"], "confirm": True})
    assert r["success"] is False and "label" in r["error"]


def test_mark_requires_read_or_unread(rec):
    r = tools.MAIL_TOOL.dispatch("mark", {"refs": ["1"], "confirm": True})
    assert r["success"] is False


def test_bulk_needs_refs_or_filter(rec):
    r = tools.MAIL_TOOL.dispatch("trash", {"confirm": True})
    assert r["success"] is False


def test_upload_validates_local_path(rec, tmp_path):
    r = tools.DRIVE_TOOL.dispatch("upload", {"path": str(tmp_path / "nope.txt"), "dir": "/x", "confirm": True})
    assert r["success"] is False
    assert "not found" in r["error"]
    assert rec["argv"] == []


def test_upload_existing_path_uses_positional_args(rec, tmp_path):
    f = tmp_path / "real.txt"
    f.write_text("hi")
    rec["proc"] = proc_json({"id": "9"})
    r = tools.DRIVE_TOOL.dispatch("upload", {"path": str(f), "dir": "/docs", "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["drive", "items", "upload"] and str(f) in args
    assert args[-1] == "/docs" and "--file" not in args and "--dir" not in args
    assert r["applied"] is True


def test_upload_if_exists_passthrough(rec, tmp_path):
    f = tmp_path / "real.txt"
    f.write_text("hi")
    rec["proc"] = proc_json({"id": "9"})
    r = tools.DRIVE_TOOL.dispatch(
        "upload", {"path": str(f), "dir": "/docs", "if_exists": "replace", "confirm": True}
    )
    args = tail_without_globals(rec["argv"])
    i = args.index("--if-exists")
    assert args[i + 1] == "replace"
    assert r["applied"] is True


def test_upload_if_exists_invalid_value_rejected(rec, tmp_path):
    f = tmp_path / "real.txt"
    f.write_text("hi")
    r = tools.DRIVE_TOOL.dispatch(
        "upload", {"path": str(f), "if_exists": "clobber", "confirm": True}
    )
    assert r["success"] is False
    assert "if_exists" in r["error"]
    assert rec["argv"] == []


def test_drive_get_accepts_path_target(rec):
    rec["proc"] = proc_json({"name": "DESIGN-INDEX.md"})
    tools.DRIVE_TOOL.dispatch("get", {"path": "/hermes-designs/DESIGN-INDEX.md"})
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["drive", "items", "get"]
    assert args[3] == "/hermes-designs/DESIGN-INDEX.md"


# --- argv shape for key verbs -----------------------------------------------------


def test_mail_move_argv(rec):
    tools.MAIL_TOOL.dispatch("move", {"refs": ["5"], "into": "Archive", "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["mail", "messages", "move", "5"]
    assert "--into" in args and "Archive" in args


def test_mark_read_argv(rec):
    tools.MAIL_TOOL.dispatch("mark", {"refs": ["5"], "read": True, "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["mail", "messages", "mark", "read"]


def test_send_uses_stdin_for_body(rec):
    rec["proc"] = proc_json({"sent": True})
    tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "secret-body"}, confirm=True
    )
    args = tail_without_globals(rec["argv"])
    assert "secret-body" not in " ".join(args)
    assert rec["input"] is not None and "secret-body" in rec["input"]
    assert "x@y.z" in args and "--subject" in args and "Hi" in args


def test_calendar_events_create_argv(rec):
    rec["proc"] = proc_json({"id": "1"})
    tools.CALENDAR_TOOL.dispatch(
        "events_create",
        {"calendar": "Personal", "title": "Sync", "start": "2026-11-01T10:00:00Z", "end": "2026-11-01T11:00:00Z"},
        confirm=True,
    )
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["calendar", "events", "create"]
    for flag in ["--calendar", "--title", "--start", "--end"]:
        assert flag in args


def test_calendar_events_list_passes_calendar(rec):
    tools.CALENDAR_TOOL.dispatch("events_list", {"calendar": "Work"})
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["calendar", "events", "list"]
    assert "--calendar" in args and "Work" in args


def test_drive_list_and_trash_argv(rec):
    tools.DRIVE_TOOL.dispatch("list", {"dir": "/docs"})
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["drive", "items", "list"] and "/docs" in args and "--dir" not in args

    rec["argv"] = []
    tools.DRIVE_TOOL.dispatch("trash", {"refs": ["9"], "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["drive", "items", "trash", "9"]


# --- unknown action ----------------------------------------------------------------


def test_unknown_action_raises(rec):
    with pytest.raises(ValueError):
        tools.MAIL_TOOL.dispatch("explode", {})


# --- review fixes: applied/success correctness ----------------------------------


def test_failed_real_run_sets_applied_false(rec):
    rec["proc"] = make_proc(stdout="boom", returncode=5)
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": True})
    assert r["success"] is False
    assert r["applied"] is False


def test_unknown_exit_code_branch(rec):
    rec["proc"] = make_proc(stdout="weird", returncode=7)
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": True})
    assert r["success"] is False
    assert r["exit_code"] == 7
    assert r["meaning"] is None
    assert "Unexpected exit code" in r["error"]
    assert r["applied"] is False


def test_failed_dry_run_envelope_not_success(rec):
    rec["proc"] = make_proc(stdout="bad", returncode=1)
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": False})
    assert r["success"] is False
    assert r["applied"] is False
    assert r["dry_run"] is True
    assert "fix" in r["next_step"].lower()
    assert "approve" not in r["next_step"].lower()


def test_success_dry_run_envelope_invites_approval(rec):
    rec["proc"] = proc_json({"affected": 1})
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": False})
    assert r["success"] is True
    assert r["next_step"] == NEXT_STEP


def test_empty_stdout_success_path(rec):
    rec["proc"] = make_proc(stdout="")
    r = tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": True})
    assert r["success"] is True
    assert r["applied"] is True
    assert r["raw"] == ""


# --- boolean filter flags are bare switches -------------------------------------


def test_boolean_flags_have_no_value_string(rec):
    f = {
        "has_attachments": True,
        "read": True,
        "unread": True,
        "starred": True,
        "subject": "x",
    }
    tools.MAIL_TOOL.dispatch("list", {"filter": f})
    args = tail_without_globals(rec["argv"])
    for flag in ["--has-attachments", "--read", "--unread", "--starred"]:
        i = args.index(flag)
        assert i + 1 == len(args) or not args[i + 1] in ("True", "False"), (flag, args[i:i + 2])
    assert "True" not in args and "False" not in args


def test_boolean_flag_false_is_omitted(rec):
    tools.MAIL_TOOL.dispatch("list", {"filter": {"read": False, "starred": False, "subject": "x"}})
    args = tail_without_globals(rec["argv"])
    assert "--read" not in args and "--starred" not in args
    assert "--subject" in args


# --- null limit rails to default -------------------------------------------------


def test_null_limit_defaults_to_150(rec):
    tools.MAIL_TOOL.dispatch("trash", {"filter": {"subject": "x", "limit": None}, "confirm": True})
    args = tail_without_globals(rec["argv"])
    i = args.index("--limit")
    assert args[i + 1] == "150"


# --- missing id -> validation error, not KeyError --------------------------------


@pytest.mark.parametrize(
    "tool,action",
    [
        (tools.MAIL_TOOL, "get"),
        (tools.CALENDAR_TOOL, "events_get"),
        (tools.DRIVE_TOOL, "get"),
    ],
)
def test_missing_id_is_validation_error(rec, tool, action):
    r = tool.dispatch(action, {})
    assert r["success"] is False
    assert "id" in r["error"]
    assert rec["argv"] == []


# --- mark: explicit read/unread semantics ----------------------------------------


def test_mark_unread_true_uses_unread(rec):
    tools.MAIL_TOOL.dispatch("mark", {"refs": ["5"], "unread": True, "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["mail", "messages", "mark", "unread"]


def test_mark_read_false_unread_true(rec):
    tools.MAIL_TOOL.dispatch("mark", {"refs": ["5"], "read": False, "unread": True, "confirm": True})
    args = tail_without_globals(rec["argv"])
    assert args[3] == "unread"


def test_mark_needs_a_true_flag(rec):
    r = tools.MAIL_TOOL.dispatch("mark", {"refs": ["5"], "read": False, "unread": False, "confirm": True})
    assert r["success"] is False


# --- drive download (read action) -------------------------------------------------


def test_drive_download_argv_shape(rec):
    rec["proc"] = make_proc(stdout="")
    r = tools.DRIVE_TOOL.dispatch("download", {"path": "/Documents/report.pdf", "dest_dir": "/tmp/out"})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["drive", "items", "download", "/Documents/report.pdf"]
    i = args.index("--dest-dir")
    assert args[i + 1] == "/tmp/out"
    assert "--dry-run" not in rec["argv"]
    assert r["success"] is True


def test_drive_download_recursive(rec):
    tools.DRIVE_TOOL.dispatch(
        "download", {"path": "/Docs", "dest_dir": ".", "recursive": True, "force": True}
    )
    args = tail_without_globals(rec["argv"])
    assert "--recursive" in args and "--force" in args


def test_drive_download_requires_dest_dir(rec):
    r = tools.DRIVE_TOOL.dispatch("download", {"path": "/x"})
    assert r["success"] is False
    assert "dest_dir" in r["error"]
    assert rec["argv"] == []


def test_drive_download_requires_path(rec):
    r = tools.DRIVE_TOOL.dispatch("download", {"dest_dir": "."})
    assert r["success"] is False
    assert "path" in r["error"]


def test_drive_download_never_in_dispatched_dry_run(rec):
    tools.DRIVE_TOOL.dispatch("download", {"path": "/x", "dest_dir": ".", "confirm": False})
    assert "--dry-run" not in rec["argv"]


def test_drive_download_chmods_single_target(rec, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    target = dest / "report.pdf"
    target.write_bytes(b"x")
    target.chmod(0o600)
    rec["proc"] = make_proc(stdout="")
    r = tools.DRIVE_TOOL.dispatch("download", {"path": "/Documents/report.pdf", "dest_dir": str(dest)})
    assert r["success"] is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_drive_download_recursive_leaves_modes(rec, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    target = dest / "keep.bin"
    target.write_bytes(b"x")
    target.chmod(0o600)
    tools.DRIVE_TOOL.dispatch("download", {"path": "/Docs", "dest_dir": str(dest), "recursive": True})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --- Task 4a polish: re-review fixes ----------------------------------------------

def test_refs_and_filter_not_combined(rec):
    """refs + filter together is ambiguous -> validation error, no CLI run."""
    r = tools.MAIL_TOOL.dispatch(
        "move", {"refs": ["1", "2"], "filter": {"folder": "Inbox"}, "into": "Archive", "confirm": True}
    )
    assert r["success"] is False
    assert r["exit_code"] is None
    assert "refs" in r["error"] and "filter" in r["error"]
    assert rec["argv"] == []


def test_filter_echo_includes_boolean_filters(rec):
    rec["proc"] = proc_json({"affected": 3})
    r = tools.MAIL_TOOL.dispatch(
        "move",
        {"filter": {"subject": "x", "read": True, "starred": True}, "into": "Archive", "confirm": True},
    )
    assert r["filter"]["subject"] == "x"
    assert r["filter"]["read"] is True
    assert r["filter"]["starred"] is True
    assert "unread" not in r["filter"]
    assert "has_attachments" not in r["filter"]


@pytest.mark.parametrize("limit", ["50", 1.5, True])
def test_non_int_limit_is_validation_error(rec, limit):
    r = tools.MAIL_TOOL.dispatch("trash", {"filter": {"subject": "x", "limit": limit}, "confirm": True})
    assert r["success"] is False
    assert r["exit_code"] is None
    assert "limit" in r["error"]
    assert rec["argv"] == []  # raw string never reaches argv


def test_non_int_limit_rejected_on_reads_too(rec):
    r = tools.MAIL_TOOL.dispatch("list", {"filter": {"limit": "50"}})
    assert r["success"] is False
    assert "limit" in r["error"]
    assert rec["argv"] == []


def test_int_limit_still_works(rec):
    rec["proc"] = proc_json({"affected": 5})
    r = tools.MAIL_TOOL.dispatch("trash", {"filter": {"subject": "x", "limit": 10}, "confirm": True})
    assert r["success"] is True
    args = tail_without_globals(rec["argv"])
    i = args.index("--limit")
    assert args[i + 1] == "10"


def test_dry_run_validation_failure_next_step_says_validation(rec):
    """Gated confirm=False + local validation error -> envelope says validation failed."""
    r = tools.MAIL_TOOL.dispatch("move", {"refs": ["1"]})  # missing 'into', no confirm
    assert r["success"] is False
    assert r["applied"] is False
    assert r["dry_run"] is True
    assert r["preview"]["error"]
    assert "validation" in r["next_step"].lower()
    assert "dry-run failed" not in r["next_step"].lower()
    assert rec["argv"] == []


# --- calendar CLI contract (verified against `proton calendar ... --help`) --------


def test_calendars_maps_to_settings_calendars_list(rec):
    """There is no `proton calendar calendars` command; the real list lives
    under `calendar settings calendars list`."""
    tools.CALENDAR_TOOL.dispatch("calendars", {})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["calendar", "settings", "calendars", "list"]
    assert "--limit" not in args and "--page" not in args


def test_calendars_limit_page(rec):
    tools.CALENDAR_TOOL.dispatch("calendars", {"limit": 20, "page": 1})
    args = tail_without_globals(rec["argv"])
    i = args.index("--limit")
    assert args[i + 1] == "20"
    j = args.index("--page")
    assert args[j + 1] == "1"


def test_calendars_non_int_limit_rejected(rec):
    r = tools.CALENDAR_TOOL.dispatch("calendars", {"limit": "20"})
    assert r["success"] is False and "limit" in r["error"]
    assert rec["argv"] == []


def test_events_list_full_flag_map(rec):
    tools.CALENDAR_TOOL.dispatch(
        "events_list",
        {"after": "2026-04-01", "before": "2026-04-30", "calendar": "Work",
         "keyword": "dentist", "limit": 50, "page": 0},
    )
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["calendar", "events", "list"]
    for flag, val in [
        ("--after", "2026-04-01"), ("--before", "2026-04-30"),
        ("--calendar", "Work"), ("--keyword", "dentist"),
        ("--limit", "50"), ("--page", "0"),
    ]:
        i = args.index(flag)
        assert args[i + 1] == val


def test_events_list_non_int_limit_rejected(rec):
    r = tools.CALENDAR_TOOL.dispatch("events_list", {"limit": "50"})
    assert r["success"] is False and "limit" in r["error"]
    assert rec["argv"] == []


# --- attachment actions (design §5 items 1-15; items 16-17 in test_mcp_tools) ---
#
# All hermetic: monkeypatched runner, no network, no real Proton calls.
# Downloads run real directory mechanics against tmp paths; the default spool
# destination is repointed at a tmp dir so tests never touch
# /srv/mcp_files/receive.


@pytest.fixture
def tmp_dest(monkeypatch, tmp_path):
    """Point the attachment download default dest at a tmp dir."""
    dest = tmp_path / "receive"
    monkeypatch.setattr(tools, "DEFAULT_ATTACHMENT_DEST_DIR", str(dest))
    return dest


def _downloaded_payload(count: int = 0, **extra) -> dict:
    """The CLI's download-all JSON result (fact 6: kind, count, no name)."""
    payload = {"action": "downloaded", "count": count, "dry_run": False, "kind": "attachment"}
    payload.update(extra)
    return payload


def _download_sweep_cases() -> list[tuple[str, dict]]:
    """Every attachments_download / conversation_attachments_download argv
    variant exercised by this module's download tests (items 3-6, 9, 11)."""
    return [
        ("attachments_download", {"id": "abc123"}),
        ("attachments_download", {"id": "abc123", "attachment": "att-1", "force": True, "include_inline": True}),
        ("conversation_attachments_download", {"id": "conv9", "force": True}),
        ("attachments_download", {"id": "-abc123"}),
        ("attachments_download", {"id": "--abc123"}),
        ("attachments_download", {"id": "abc123", "attachment": "-weird"}),
        ("attachments_download", {"id": "abc123", "force": True}),
        ("attachments_download", {"id": "abc123", "include_inline": True}),
    ]


def test_attachments_list_argv_id_only(rec):
    """Item 1: exact argv — no flags, `--`, then the positional."""
    rec["proc"] = proc_json({"attachments": [], "count": 0})
    tools.MAIL_TOOL.dispatch("attachments_list", {"id": "abc123"})
    assert tail_without_globals(rec["argv"]) == [
        "mail", "messages", "attachments", "list", "--", "abc123",
    ]


def test_attachments_list_argv_include_inline_flag(rec):
    """Item 2: the flag after `list`, then the `--`, then the positional."""
    rec["proc"] = proc_json({"attachments": [], "count": 0})
    tools.MAIL_TOOL.dispatch("attachments_list", {"id": "abc123", "include_inline": True})
    args = tail_without_globals(rec["argv"])
    assert args[:5] == ["mail", "messages", "attachments", "list", "--include-inline"]
    assert args[5:] == ["--", "abc123"]


def test_attachments_download_argv_flags_first(rec, tmp_dest):
    """Item 3: flags-first argv — --dest-dir present, then `--`, then the
    positional."""
    rec["proc"] = proc_json(_downloaded_payload())
    tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123"})
    args = tail_without_globals(rec["argv"])
    assert args == [
        "mail", "messages", "attachments", "download",
        "--dest-dir", str(tmp_dest), "--", "abc123",
    ]


def test_attachments_download_argv_attachment_and_force(rec, tmp_dest):
    """Items 4 + 11 (asserted here per the design): --force present,
    --include-inline absent even though the parameter was passed, both
    positionals after the `--` in order."""
    rec["proc"] = proc_json(_downloaded_payload(1, name="a.pdf"))
    tools.MAIL_TOOL.dispatch(
        "attachments_download",
        {"id": "abc123", "attachment": "att-1", "force": True, "include_inline": True},
    )
    args = tail_without_globals(rec["argv"])
    assert args == [
        "mail", "messages", "attachments", "download",
        "--dest-dir", str(tmp_dest), "--force", "--", "abc123", "att-1",
    ]
    assert "--include-inline" not in args


def test_conversation_attachments_download_argv_shape(rec, tmp_dest):
    """Item 5: `mail conversations attachments ...` argv, same flag positions
    and `--` placement."""
    rec["proc"] = proc_json(_downloaded_payload())
    tools.MAIL_TOOL.dispatch(
        "conversation_attachments_download", {"id": "conv9", "force": True}
    )
    args = tail_without_globals(rec["argv"])
    assert args == [
        "mail", "conversations", "attachments", "download",
        "--dest-dir", str(tmp_dest), "--force", "--", "conv9",
    ]


@pytest.mark.parametrize(
    "rid, att",
    [
        ("abc123", "-weird"),  # leading-dash attachment name
        ("-abc123", None),  # single-dash id
        ("--abc123", None),  # double-dash id
    ],
)
def test_attachment_refs_after_separator_land_as_positionals(rec, tmp_dest, rid, att):
    """Item 6: after the `--` every ref shape lands as a literal positional
    element; the region before the `--` stays exactly the wrapper's own flags
    (the CLI's shape-guessing never runs on these calls)."""
    rec["proc"] = proc_json(_downloaded_payload())
    params = {"id": rid}
    if att is not None:
        params["attachment"] = att
    tools.MAIL_TOOL.dispatch("attachments_download", params)
    args = tail_without_globals(rec["argv"])
    sep = args.index("--")
    assert args[:sep] == [
        "mail", "messages", "attachments", "download", "--dest-dir", str(tmp_dest),
    ]
    assert args[sep] == "--"
    assert args[sep + 1:] == ([rid, att] if att else [rid])


def test_attachments_download_default_dest_dir_emits_module_constant(rec, monkeypatch, tmp_path):
    """Item 7: omitting dest_dir emits the module constant's value in argv;
    the constant itself pins /srv/mcp_files/receive."""
    assert tools.DEFAULT_ATTACHMENT_DEST_DIR == "/srv/mcp_files/receive"  # the pin
    dest = tmp_path / "receive"
    monkeypatch.setattr(tools, "DEFAULT_ATTACHMENT_DEST_DIR", str(dest))  # hermetic stand-in
    rec["proc"] = proc_json(_downloaded_payload())
    tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123"})
    args = tail_without_globals(rec["argv"])
    assert args[args.index("--dest-dir") + 1] == str(dest)  # the constant's value in argv


def test_attachments_download_dir_diff_reports_new_files_and_chmods(rec, monkeypatch, tmp_path):
    """Item 8: pre-seed dest_dir with an unrelated file and a colliding name;
    the fake CLI writes the "downloaded" files (collision renamed, as the CLI
    does). The diff reports exactly the new files, chmod lands 0644 on each,
    and .proton-cli-* residue is excluded from both the report and the chmod."""
    dest = tmp_path / "out"
    dest.mkdir()
    unrelated = dest / "notes.txt"
    unrelated.write_bytes(b"unrelated")
    unrelated.chmod(0o600)
    collision = dest / "invoice.pdf"
    collision.write_bytes(b"old-bytes")
    collision.chmod(0o600)

    def fake_cli(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        rec["argv"] = list(args)
        renamed = dest / "invoice (2).pdf"  # the CLI renames the collision
        renamed.write_bytes(b"new-invoice-bytes")
        sig = dest / "sig.png"
        sig.write_bytes(b"sig-bytes")
        residue = dest / ".proton-cli-77.tmp"  # a killed CLI skips its cleanup
        residue.write_bytes(b"partial-temp")
        residue.chmod(0o600)
        return proc_json(_downloaded_payload(2))

    monkeypatch.setattr(subprocess, "run", fake_cli)
    r = tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123", "dest_dir": str(dest)})
    assert r["success"] is True
    assert [f["name"] for f in r["files"]] == ["invoice (2).pdf", "sig.png"]
    assert r["files"][0] == {"name": "invoice (2).pdf", "size": len(b"new-invoice-bytes")}
    assert r["files"][1]["size"] == len(b"sig-bytes")
    assert r["count"] == 2
    assert r["dest_dir"] == str(dest)
    assert "note" not in r
    assert stat.S_IMODE((dest / "invoice (2).pdf").stat().st_mode) == 0o644
    assert stat.S_IMODE((dest / "sig.png").stat().st_mode) == 0o644
    # Untouched files stay untouched: not reported, not chmod'd.
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o600
    assert unrelated.read_bytes() == b"unrelated"
    assert stat.S_IMODE(collision.stat().st_mode) == 0o600
    assert collision.read_bytes() == b"old-bytes"
    assert all(f["name"] != ".proton-cli-77.tmp" for f in r["files"])
    assert stat.S_IMODE((dest / ".proton-cli-77.tmp").stat().st_mode) == 0o600


def test_attachments_download_force_overwrite_lists_changed_file(rec, monkeypatch, tmp_path):
    """Item 9: a --force overwrite of a pre-existing target reports as
    changed (not new) and is still listed."""
    dest = tmp_path / "out"
    dest.mkdir()
    target = dest / "invoice.pdf"
    target.write_bytes(b"old-bytes")
    target.chmod(0o600)

    def fake_cli(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        rec["argv"] = list(args)
        target.write_bytes(b"brand-new-bytes")  # --force overwrite in place
        return proc_json(_downloaded_payload(1))

    monkeypatch.setattr(subprocess, "run", fake_cli)
    r = tools.MAIL_TOOL.dispatch(
        "attachments_download", {"id": "abc123", "dest_dir": str(dest), "force": True}
    )
    assert r["success"] is True
    assert r["files"] == [
        {"name": "invoice.pdf", "size": len(b"brand-new-bytes"), "changed": True}
    ]
    assert r["count"] == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert "--force" in tail_without_globals(rec["argv"])


def test_attachments_download_partial_failure_files_kept_and_count_note(rec, monkeypatch, tmp_path):
    """Item 10: a CLI failure mid-loop keeps success:false with the partial
    file set (no applied key on reads); a success run whose diff count
    disagrees with data.count sets the direction-neutral note in both
    directions (inflated and deflated)."""
    dest = tmp_path / "out"
    dest.mkdir()

    # Partial failure: one file written before the CLI aborts.
    def failing_cli(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        rec["argv"] = list(args)
        (dest / "first.pdf").write_bytes(b"part-one")
        return make_proc(stdout="", returncode=5)

    monkeypatch.setattr(subprocess, "run", failing_cli)
    r = tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123", "dest_dir": str(dest)})
    assert r["success"] is False
    assert r["exit_code"] == 5
    assert [f["name"] for f in r["files"]] == ["first.pdf"]
    assert r["count"] == 1
    assert stat.S_IMODE((dest / "first.pdf").stat().st_mode) == 0o644
    assert "applied" not in r
    assert "note" not in r  # the note is a success-run signal

    # Inflated: a concurrent spool writer adds a file between run and diff.
    def inflating_cli(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        rec["argv"] = list(args)
        (dest / "only.pdf").write_bytes(b"mine")
        (dest / "stray-by-other-container.txt").write_bytes(b"not mine")
        return proc_json(_downloaded_payload(1))

    monkeypatch.setattr(subprocess, "run", inflating_cli)
    r = tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123", "dest_dir": str(dest)})
    assert r["success"] is True
    assert r["count"] == 2 and r["data"]["count"] == 1
    assert r["note"] == "the number of files differs from the CLI's reported count"

    # Deflated: the CLI planned two, one vanished between write and diff.
    def deflating_cli(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        rec["argv"] = list(args)
        (dest / "only2.pdf").write_bytes(b"mine")
        (dest / "stray-by-other-container.txt").unlink()
        return proc_json(_downloaded_payload(2))

    monkeypatch.setattr(subprocess, "run", deflating_cli)
    r = tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123", "dest_dir": str(dest)})
    assert r["success"] is True
    assert r["count"] == 1 and r["data"]["count"] == 2
    assert "note" in r


def test_attachments_download_include_inline_omitted_when_attachment_set(rec, tmp_dest):
    """Item 11: with `attachment` given, --include-inline is omitted regardless
    of the parameter; without it, a truthy include_inline emits the flag."""
    rec["proc"] = proc_json(_downloaded_payload(1, name="a.pdf"))
    for include in (True, False):
        tools.MAIL_TOOL.dispatch(
            "attachments_download",
            {"id": "abc123", "attachment": "att-9", "include_inline": include},
        )
        assert "--include-inline" not in tail_without_globals(rec["argv"])
    tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123", "include_inline": True})
    assert "--include-inline" in tail_without_globals(rec["argv"])


def test_run_timeout_seam_forwards_to_runner(rec, tmp_dest, monkeypatch):
    """Item 12: _run forwards its timeout kwarg to runner.run with the 60 s
    default intact for existing callers; both download actions pass
    timeout=300 through _run and the list actions use the default."""
    seen: list = []
    real_run = runner.run

    def spy_run(args, stdin_text=None, timeout=None, dry_run=False):
        seen.append(timeout)
        if timeout is None:
            return real_run(args, stdin_text=stdin_text, dry_run=dry_run)
        return real_run(args, stdin_text=stdin_text, dry_run=dry_run, timeout=timeout)

    monkeypatch.setattr(runner, "run", spy_run)

    # Direct forwarding through _run.
    tools.MAIL_TOOL._run(["probe"], timeout=300)
    assert seen == [300]

    # Downloads pass 300 (both families), visible at subprocess level too.
    rec["timeout"] = None
    tools.MAIL_TOOL.dispatch("attachments_download", {"id": "abc123"})
    assert seen[-1] == 300
    assert rec["timeout"] == 300
    tools.MAIL_TOOL.dispatch("conversation_attachments_download", {"id": "conv9"})
    assert seen[-1] == 300

    # Lists and existing callers keep the runner's own 60 s default.
    seen.clear()
    rec["timeout"] = None
    tools.MAIL_TOOL.dispatch("attachments_list", {"id": "abc123"})
    tools.MAIL_TOOL.dispatch("conversation_attachments_list", {"id": "conv9"})
    tools.MAIL_TOOL.dispatch("status", {})
    assert seen == [None, None, None]
    assert rec["timeout"] == 60


@pytest.mark.parametrize(
    "case, params, needle",
    [
        ("missing-id", {}, "id"),
        ("attachment-as-list", {"id": "abc123", "attachment": ["a.pdf"]}, "attachment"),
        ("empty-dest-dir", {"id": "abc123", "dest_dir": ""}, "dest_dir"),
        ("makedirs-failure", None, "dest_dir"),
    ],
)
def test_attachments_download_validation_errors(rec, tmp_path, case, params, needle):
    """Item 13: structured validation errors before any argv build: a
    missing id; attachment given as a list (a stringified list would smuggle
    itself into one argv element); the empty dest_dir string (aligned with
    the drive download's refusal); an un-creatable dest_dir naming the path.
    The CLI never runs."""
    if case == "makedirs-failure":
        blocked = tmp_path / "blocked"
        blocked.write_text("x")  # exists as a file: makedirs refuses
        params = {"id": "abc123", "dest_dir": str(blocked)}
    r = tools.MAIL_TOOL.dispatch("attachments_download", params)
    assert r["success"] is False
    assert r["validation"] is True
    assert needle in r["error"]
    if case == "makedirs-failure":
        assert str(blocked) in r["error"]
    assert r["exit_code"] is None
    assert r["auth_required"] is False
    assert rec["argv"] == []


def test_attachments_download_never_dry_run_sweep(rec, tmp_dest):
    """Item 14: downloads never receive --dry-run in any argv (sweep over
    every download case in this module)."""
    for action, params in _download_sweep_cases():
        rec["argv"] = []
        rec["proc"] = proc_json(_downloaded_payload())
        r = tools.MAIL_TOOL.dispatch(action, params)
        assert r["success"] is True, (action, params, r)
        assert rec["argv"], (action, params)
        assert "--dry-run" not in rec["argv"], (action, params)
        assert "--dry-run" not in (runner.LAST_ARGV or [])


def test_attachments_download_dest_dir_sweep(rec, tmp_dest):
    """Item 15: every download argv carries --dest-dir (explicit or the module
    default), so the CLI's write-into-CWD behavior is unreachable; the bare
    single-file --dest variant is never emitted (D5)."""
    for action, params in _download_sweep_cases():
        rec["argv"] = []
        rec["proc"] = proc_json(_downloaded_payload())
        tools.MAIL_TOOL.dispatch(action, params)
        assert "--dest-dir" in rec["argv"], (action, params)
        i = rec["argv"].index("--dest-dir")
        expected = params.get("dest_dir") or str(tmp_dest)
        assert rec["argv"][i + 1] == expected
        assert "--dest" not in tail_without_globals(rec["argv"])


# --- send with attachments (design §5 tests 1-12 and 14; test 13 lives in
# test_mcp_tools.py) ---
#
# All hermetic: monkeypatched runner, no network, no real Proton calls. The
# spool gate root and the size cap are repointed at tmp paths, so no test
# touches /srv/mcp_files/send and no test needs a 200 MiB fixture.


@pytest.fixture
def spool(monkeypatch, tmp_path):
    """Point the attach gate's spool root at a tmp dir (the module constants
    are read at call time, so monkeypatching works)."""
    root = tmp_path / "send"
    root.mkdir()
    monkeypatch.setattr(tools, "ATTACH_SPOOL_ROOT", root)
    return root


def _staged(root: Path, name: str, size: int = 4) -> Path:
    f = root / name
    f.write_bytes(b"x" * size)
    return f


def _sent_payload() -> dict:
    """The CLI's real-send JSON result (fact 6's Create shape, sent side)."""
    return {
        "action": "sent", "count": 1, "dry_run": False,
        "kind": "message", "name": "Hi", "ids": ["m123"],
    }


def _spy_on_runner(monkeypatch, seen: list):
    """Wrap runner.run recording the timeout it received (the sibling spec's
    seam-spy pattern)."""
    real_run = runner.run

    def spy_run(args, stdin_text=None, timeout=None, dry_run=False):
        seen.append(timeout)
        if timeout is None:
            return real_run(args, stdin_text=stdin_text, dry_run=dry_run)
        return real_run(args, stdin_text=stdin_text, dry_run=dry_run, timeout=timeout)

    monkeypatch.setattr(runner, "run", spy_run)


def test_send_plain_argv_carries_body_dash(rec):
    """Test 1: exact argv; the body rides stdin via `--body -`. The shipped
    branch carried no --body flag at all, and the CLI reads stdin only when
    the flag value is exactly `-`, so every send through the MCP exited 1
    with 'A body is required.'; this is that defect's regression test."""
    rec["proc"] = proc_json(_sent_payload())
    r = tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "secret-body"}, confirm=True
    )
    assert tail_without_globals(rec["argv"]) == [
        "mail", "messages", "send", "--to", "x@y.z", "--subject", "Hi", "--body", "-",
    ]
    assert rec["input"] == "secret-body"
    assert r["success"] is True and r["applied"] is True


def test_send_attach_argv_shape(rec, spool):
    """Test 2: one staged file; the argv ends with `--attach=<resolved path>`
    after the body flag; no positional elements anywhere (the equals form
    binds each value at the token level)."""
    f = _staged(spool, "report.pdf")
    rec["proc"] = proc_json(_sent_payload())
    tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [str(f)]},
        confirm=True,
    )
    args = tail_without_globals(rec["argv"])
    assert args == [
        "mail", "messages", "send", "--to", "x@y.z", "--subject", "Hi",
        "--body", "-", f"--attach={f.resolve()}",
    ]
    assert rec["input"] == "b"


def test_send_attach_multiple_paths_order(rec, spool):
    """Test 3: two files; both `--attach=` flags present, in parameter
    order."""
    a = _staged(spool, "a.txt")
    b = _staged(spool, "b.txt")
    rec["proc"] = proc_json(_sent_payload())
    tools.MAIL_TOOL.dispatch(
        "send",
        {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [str(a), str(b)]},
        confirm=True,
    )
    args = tail_without_globals(rec["argv"])
    assert f"--attach={a.resolve()}" in args
    assert f"--attach={b.resolve()}" in args
    assert args.index(f"--attach={a.resolve()}") < args.index(f"--attach={b.resolve()}")


def test_send_empty_attach_list_is_plain_send(rec, spool, monkeypatch):
    """Test 4: `attach: []` emits no --attach flags and keeps the default
    timeout; the 300 s budget binds to paths, not to the branch."""
    seen: list = []
    _spy_on_runner(monkeypatch, seen)
    rec["proc"] = proc_json(_sent_payload())
    tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": []},
        confirm=True,
    )
    args = tail_without_globals(rec["argv"])
    assert not [a for a in args if a.startswith("--attach")]
    assert args == [
        "mail", "messages", "send", "--to", "x@y.z", "--subject", "Hi", "--body", "-",
    ]
    assert seen == [None]


def test_send_attach_containment_refusals(rec, spool, tmp_path):
    """Test 5: a path under a tmp dir outside the root; a `..` traversal
    string; a symlink inside the root pointing outside; each a structured
    validation error naming the path. The CLI never runs."""
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "f.txt"
    outside_file.write_text("x")
    link = spool / "link.txt"
    link.symlink_to(outside_file)  # inside the root, target outside
    for raw in (str(outside_file), f"{spool}/../f.txt", str(link)):
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch(
            "send",
            {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [raw]},
            confirm=True,
        )
        assert r["success"] is False, raw
        assert r["validation"] is True, raw
        assert "must live under" in r["error"], (raw, r["error"])
        assert raw in r["error"], raw  # the offending path is named
        assert r["exit_code"] is None and r["auth_required"] is False, raw
        assert rec["argv"] == [], raw


def test_send_attach_existence_refusals(rec, spool):
    """Test 6: a missing file refuses with the drive-upload wording ('local
    file not found'); the spool root itself and a directory refuse as 'not a
    regular file'."""
    sub = spool / "subdir"
    sub.mkdir()
    for raw, needle in (
        (str(spool / "missing.txt"), "local file not found"),
        (str(spool), "not a regular file"),
        (str(sub), "not a regular file"),
    ):
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch(
            "send",
            {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [raw]},
            confirm=True,
        )
        assert r["success"] is False, raw
        assert r["validation"] is True, raw
        assert needle in r["error"], (raw, r["error"])
        assert rec["argv"] == [], raw


def test_send_attach_cap_refusal_and_boundary(rec, spool, monkeypatch):
    """Test 7 (the cap monkeypatched small): one byte over refuses with the
    total and the cap named; exactly at the cap passes to argv."""
    monkeypatch.setattr(tools, "MAX_ATTACH_TOTAL_BYTES", 10)
    rec["proc"] = proc_json(_sent_payload())
    over = _staged(spool, "over.bin", 11)
    r = tools.MAIL_TOOL.dispatch(
        "send",
        {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [str(over)]},
        confirm=True,
    )
    assert r["success"] is False and r["validation"] is True
    assert "11 bytes exceeds the 10-byte cap" in r["error"]
    assert rec["argv"] == []  # the CLI never runs

    at = _staged(spool, "at.bin", 10)  # exactly at the cap: passes
    tools.MAIL_TOOL.dispatch(
        "send",
        {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [str(at)]},
        confirm=True,
    )
    assert f"--attach={at.resolve()}" in tail_without_globals(rec["argv"])


def test_send_body_required_validation(rec):
    """Test 8: body absent, empty string, and non-string each refuse as
    structured validation errors; the shipped observable contract (a send
    without a body fails) preserved, moved in front of the CLI."""
    for body in (None, "", 5):
        params = {"to": "x@y.z", "subject": "Hi"}
        if body is not None:
            params["body"] = body
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch("send", params, confirm=True)
        assert r["success"] is False, body
        assert r["validation"] is True, body
        assert "non-empty 'body'" in r["error"], body
        assert rec["argv"] == [], body


def test_send_shape_error_on_bare_string_attach(rec, spool):
    """Test 9: `attach: \"<path>\"` refuses; a stringified path must never
    become one argv element (the _refs_error lesson at the boundary)."""
    f = _staged(spool, "real.pdf")
    r = tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": str(f)},
        confirm=True,
    )
    assert r["success"] is False and r["validation"] is True
    assert "'attach' must be a list" in r["error"]
    assert "str" in r["error"]
    assert rec["argv"] == []


def test_send_attach_timeout_seam(rec, spool, monkeypatch):
    """Test 10: an attach-carrying send passes timeout=300 to the runner; a
    plain send passes no timeout (the 60 s default intact)."""
    seen: list = []
    _spy_on_runner(monkeypatch, seen)
    rec["proc"] = proc_json(_sent_payload())
    f = _staged(spool, "big.bin")
    tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [str(f)]},
        confirm=True,
    )
    assert seen[-1] == 300
    assert rec["timeout"] == 300

    rec["timeout"] = None
    tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b"}, confirm=True
    )
    assert seen[-1] is None
    assert rec["timeout"] == 60  # the runner's own default intact


def test_send_resolved_path_in_argv(rec, spool):
    """Test 11: a symlink inside the root pointing deeper inside resolves, and
    the argv carries the resolved target; the path the CLI will actually
    read."""
    sub = spool / "sub"
    sub.mkdir()
    actual = sub / "actual.pdf"
    actual.write_bytes(b"x")
    link = spool / "link.pdf"
    link.symlink_to(actual)
    rec["proc"] = proc_json(_sent_payload())
    tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": [str(link)]},
        confirm=True,
    )
    assert tail_without_globals(rec["argv"])[-1] == f"--attach={actual.resolve()}"


def _send_sweep_cases(spool: Path) -> list[dict]:
    """Every send argv variant exercised by this module's send tests."""
    plain = {"to": "x@y.z", "subject": "Hi", "body": "b"}
    return [
        plain,
        dict(plain, attach=[str(_staged(spool, "one.pdf"))]),
        dict(plain, attach=[str(_staged(spool, "two-a.pdf")), str(_staged(spool, "two-b.pdf"))]),
        dict(plain, attach=[]),
    ]


def test_send_forbidden_flags_sweep(rec, spool):
    """Test 12: no send argv in any case carries the unwired compose flags
    (D7) or a bare `--attach` element (attach values ride the equals form)."""
    rec["proc"] = proc_json(_sent_payload())
    forbidden = [
        "--cc", "--bcc", "--attach-inline", "--html", "--eml", "--from",
        "--no-signature", "--request-receipt", "--send-at", "--expires",
    ]
    for params in _send_sweep_cases(spool):
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch("send", params, confirm=True)
        assert r["success"] is True, (params, r)
        args = rec["argv"]
        for flag in forbidden:
            assert flag not in args, (params, flag)
        assert not [a for a in args if a.startswith("--eo-password")], params
        assert "--attach" not in args, params


def test_send_validation_order_pinning(rec, spool, monkeypatch):
    """Test 14: calls tripping adjacent chain stages together pin the order
    (the cap monkeypatched small, test 7's pattern): containment wins over
    existence; existence wins over the cap; shape wins over everything."""
    monkeypatch.setattr(tools, "MAX_ATTACH_TOTAL_BYTES", 5)
    big = _staged(spool, "big.bin", 9)  # over the small cap
    outside = spool.parent / "outside"
    outside.mkdir(exist_ok=True)
    outside_file = outside / "f.txt"
    outside_file.write_text("x")

    # Containment + existence: an out-of-root path plus a missing in-root
    # path; the containment wording wins.
    r = tools.MAIL_TOOL.dispatch(
        "send",
        {"to": "x@y.z", "subject": "Hi", "body": "b",
         "attach": [str(spool / "missing.txt"), str(outside_file)]},
        confirm=True,
    )
    assert r["validation"] is True
    assert "must live under" in r["error"]
    assert "local file not found" not in r["error"]
    assert rec["argv"] == []

    # Existence + cap: a missing in-root path plus an over-cap file; the
    # existence wording wins.
    r = tools.MAIL_TOOL.dispatch(
        "send",
        {"to": "x@y.z", "subject": "Hi", "body": "b",
         "attach": [str(spool / "missing.txt"), str(big)]},
        confirm=True,
    )
    assert r["validation"] is True
    assert "local file not found" in r["error"]
    assert "exceeds" not in r["error"]
    assert rec["argv"] == []

    # Shape + everything: a present non-list attach wins over every later
    # stage, to/subject/body being valid.
    r = tools.MAIL_TOOL.dispatch(
        "send", {"to": "x@y.z", "subject": "Hi", "body": "b", "attach": str(big)},
        confirm=True,
    )
    assert r["validation"] is True
    assert "'attach' must be a list" in r["error"]
    assert rec["argv"] == []


# --- drafts actions (design §5 tests 1-19 and 22-23; tests 20-21 live in
# test_mcp_tools.py) ---
#
# All hermetic: monkeypatched runner, no network, no real Proton calls. The
# spool fixture repoints ATTACH_SPOOL_ROOT at a tmp dir, so the body_path
# gate never touches /srv/mcp_files/send; the cap is monkeypatched small
# where it matters (tests 11 and the cap refusals).


def _drafts_list_payload(count: int = 0) -> dict:
    """The CLI's drafts listing envelope (fact 15)."""
    return {"drafts": [], "count": count}


def _created_payload() -> dict:
    """The CLI's created envelope (fact 3)."""
    return {
        "action": "created", "count": 1, "dry_run": False,
        "kind": "draft", "name": "Hi", "ids": ["d123"],
    }


def _saved_payload() -> dict:
    """The CLI's saved envelope for a reply saved as a draft (fact 8)."""
    return {
        "action": "saved", "count": 1, "dry_run": False,
        "kind": "draft", "name": "Re: Hi", "ids": ["d124"],
    }


def _sent_draft_payload() -> dict:
    """The CLI's sent envelope for a delivered draft (fact 6: the draft's
    own id rides in both the dry-run preview and the real run)."""
    return {
        "action": "sent", "count": 1, "dry_run": False,
        "kind": "message", "name": "Hi", "ids": ["d123"],
    }


# --- argv shape (tests 1-6) ------------------------------------------------------


def test_drafts_list_argv_defaults(rec):
    """Test 1: bare `mail drafts list`; no flags when no params."""
    rec["proc"] = proc_json(_drafts_list_payload())
    tools.MAIL_TOOL.dispatch("drafts_list", {})
    assert tail_without_globals(rec["argv"]) == ["mail", "drafts", "list"]


def test_drafts_list_argv_passthrough(rec):
    """Test 2: `--sort size --desc --limit 5 --page 1` land in parameter
    order after `list`."""
    rec["proc"] = proc_json(_drafts_list_payload())
    tools.MAIL_TOOL.dispatch(
        "drafts_list", {"sort": "size", "desc": True, "limit": 5, "page": 1}
    )
    assert tail_without_globals(rec["argv"]) == [
        "mail", "drafts", "list",
        "--sort", "size", "--desc", "--limit", "5", "--page", "1",
    ]


@pytest.mark.parametrize("rid", ["abc123", "-abc123", "--abc123"])
def test_drafts_get_argv_marks_ref(rec, rid):
    """Test 3: `mail messages get -- <id>`; a dash-leading id and a
    double-dash id land as literal positionals after the `--`."""
    rec["proc"] = proc_json({"id": rid, "subject": "Hi", "body": "hello"})
    tools.MAIL_TOOL.dispatch("drafts_get", {"id": rid})
    assert tail_without_globals(rec["argv"]) == ["mail", "messages", "get", "--", rid]


def test_drafts_create_argv_basic(rec):
    """Test 4: exact `mail drafts create --to <t> --subject <s> --body -`,
    stdin_text carrying the body."""
    rec["proc"] = proc_json(_created_payload())
    r = tools.MAIL_TOOL.dispatch(
        "drafts_create", {"to": "x@y.z", "subject": "Hi", "body": "secret-body"}
    )
    assert tail_without_globals(rec["argv"]) == [
        "mail", "drafts", "create", "--to", "x@y.z", "--subject", "Hi", "--body", "-",
    ]
    assert rec["input"] == "secret-body"
    assert r["success"] is True


def test_reply_draft_argv_basic(rec):
    """Test 5: exact `mail messages reply --draft --body - -- <id>`,
    stdin_text carrying the text."""
    rec["proc"] = proc_json(_saved_payload())
    r = tools.MAIL_TOOL.dispatch("reply_draft", {"id": "m9", "body": "my reply text"})
    assert tail_without_globals(rec["argv"]) == [
        "mail", "messages", "reply", "--draft", "--body", "-", "--", "m9",
    ]
    assert rec["input"] == "my reply text"
    assert r["success"] is True


def test_reply_draft_everyone_flag(rec):
    """Test 6: `--everyone` present when true, absent otherwise, always
    before the `--`."""
    rec["proc"] = proc_json(_saved_payload())
    tools.MAIL_TOOL.dispatch("reply_draft", {"id": "m9", "body": "t", "everyone": True})
    args = tail_without_globals(rec["argv"])
    assert args == [
        "mail", "messages", "reply", "--draft", "--body", "-", "--everyone", "--", "m9",
    ]
    rec["proc"] = proc_json(_saved_payload())
    tools.MAIL_TOOL.dispatch("reply_draft", {"id": "m9", "body": "t", "everyone": False})
    args = tail_without_globals(rec["argv"])
    assert "--everyone" not in args
    assert args == ["mail", "messages", "reply", "--draft", "--body", "-", "--", "m9"]


# --- validation: the CLI never runs, the runner is not called (tests 7-11) -------


@pytest.mark.parametrize("bad", [None, "", 5, ["abc"], True])
@pytest.mark.parametrize("action", ["drafts_get", "reply_draft", "drafts_send"])
def test_drafts_get_reply_send_ref_validation(rec, action, bad):
    """Test 7: a missing, empty, non-string, list, and boolean `id` each
    refuse on all three ref-taking actions."""
    params = {} if bad is None else {"id": bad}
    if action == "drafts_send":
        rec["proc"] = proc_json(_sent_draft_payload())
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch(action, params, confirm=True)
    assert r["success"] is False, (action, bad)
    assert r["validation"] is True, (action, bad)
    assert "id" in r["error"], (action, bad)
    assert r["exit_code"] is None and r["auth_required"] is False, (action, bad)
    assert rec["argv"] == [], (action, bad)


@pytest.mark.parametrize(
    "params, needle",
    [
        ({}, "to"),
        ({"to": ""}, "to"),
        ({"to": "x@y.z"}, "subject"),
        ({"to": "x@y.z", "subject": 5}, "subject"),
    ],
)
def test_drafts_create_validation(rec, params, needle):
    """Test 8: missing `to`, empty `to`, missing `subject`, non-string
    `subject` each refuse; a subject present but empty passes to argv."""
    for given in (params, dict(params, body="b")):
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch("drafts_create", given)
        assert r["success"] is False, given
        assert r["validation"] is True, given
        assert needle in r["error"], (given, r["error"])
        assert rec["argv"] == [], given
    # Subject present but empty passes to argv (D6: present, may be empty).
    rec["proc"] = proc_json(_created_payload())
    tools.MAIL_TOOL.dispatch("drafts_create", {"to": "x@y.z", "subject": "", "body": "b"})
    args = tail_without_globals(rec["argv"])
    assert args[args.index("--subject") + 1] == ""


@pytest.mark.parametrize("action", ["drafts_create", "reply_draft"])
def test_body_exclusivity(rec, spool, action):
    """Test 9: both `body` and `body_path` refuse; neither refuses, on both
    composing actions (upstream shape checks passing first)."""
    staged = _staged(spool, "body.txt")
    if action == "drafts_create":
        base = {"to": "x@y.z", "subject": "Hi"}
    else:
        base = {"id": "m9"}
    for extra in ({"body": "inline", "body_path": str(staged)}, {}):
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch(action, dict(base, **extra))
        assert r["success"] is False, (action, extra)
        assert r["validation"] is True, (action, extra)
        assert "body" in r["error"] and "body_path" in r["error"], (action, extra)
        assert rec["argv"] == [], (action, extra)


def test_body_path_gate(rec, spool, tmp_path):
    """Test 10: out-of-root path, `..` traversal, and an outside-pointing
    symlink each refuse with the companion's wording; a directory and the
    spool root refuse as not-a-regular-file."""
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "f.txt"
    outside_file.write_text("x")
    link = spool / "link.txt"
    link.symlink_to(outside_file)  # inside the root, target outside
    sub = spool / "subdir"
    sub.mkdir()
    containment_cases = [
        (str(outside_file), "must live under"),
        (f"{spool}/../f.txt", "must live under"),
        (str(link), "must live under"),
    ]
    regular_file_cases = [
        (str(sub), "not a regular file"),
        (str(spool), "not a regular file"),
    ]
    for action in ("drafts_create", "reply_draft"):
        base = {"to": "x@y.z", "subject": "Hi"} if action == "drafts_create" else {"id": "m9"}
        for raw, needle in containment_cases + regular_file_cases:
            rec["argv"] = []
            r = tools.MAIL_TOOL.dispatch(action, dict(base, body_path=raw))
            assert r["success"] is False, (action, raw)
            assert r["validation"] is True, (action, raw)
            assert needle in r["error"], (action, raw, r["error"])
            assert r["exit_code"] is None and r["auth_required"] is False, (action, raw)
            assert rec["argv"] == [], (action, raw)
        for raw, _ in containment_cases:
            r = tools.MAIL_TOOL.dispatch(action, dict(base, body_path=raw))
            assert raw in r["error"], raw  # the offending path is named


def test_body_path_cap_and_content(rec, spool, monkeypatch):
    """Test 11: a file over the monkeypatched cap refuses with the total and
    cap named; at the cap the content rides stdin_text and the argv carries
    `--body -`; a file grown between the stat and the read refuses at the
    read with the same wording."""
    monkeypatch.setattr(tools, "MAX_ATTACH_TOTAL_BYTES", 10)
    rec["proc"] = proc_json(_created_payload())
    over = _staged(spool, "over.txt", 11)
    r = tools.MAIL_TOOL.dispatch(
        "drafts_create", {"to": "x@y.z", "subject": "Hi", "body_path": str(over)}
    )
    assert r["success"] is False and r["validation"] is True
    assert "11 bytes exceeds the 10-byte cap" in r["error"]
    assert rec["argv"] == []  # the CLI never runs

    at = _staged(spool, "at.txt", 10)  # exactly at the cap: content rides stdin
    tools.MAIL_TOOL.dispatch(
        "drafts_create", {"to": "x@y.z", "subject": "Hi", "body_path": str(at)}
    )
    args = tail_without_globals(rec["argv"])
    assert args == [
        "mail", "drafts", "create", "--to", "x@y.z", "--subject", "Hi", "--body", "-",
    ]
    assert rec["input"] == "x" * 10

    # Grown between the stat and the read: the stat reports under-cap, the
    # read holds over-cap, and the read's re-check refuses with the same
    # over-cap wording.
    grown = _staged(spool, "grown.txt", 12)
    real_getsize = os.path.getsize
    monkeypatch.setattr(os.path, "getsize", lambda p: 9)
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch(
        "drafts_create", {"to": "x@y.z", "subject": "Hi", "body_path": str(grown)}
    )
    assert r["success"] is False and r["validation"] is True
    assert "12 bytes exceeds the 10-byte cap" in r["error"]
    assert rec["argv"] == []
    assert real_getsize(grown) == 12  # the file was never modified


# --- behavior (tests 12-18) --------------------------------------------------------


def test_drafts_send_gated_flow(rec, monkeypatch):
    """Test 12: confirm=false produces the preview envelope with --dry-run
    in the prepended region and data.ids present; confirm=true runs without
    --dry-run; the dry-run argv check still refuses a dropped flag."""
    rec["proc"] = proc_json(_sent_draft_payload())
    r = tools.MAIL_TOOL.dispatch("drafts_send", {"id": "d123"}, confirm=False)
    assert r["success"] is True
    assert r["applied"] is False and r["dry_run"] is True
    assert r["action"] == "drafts_send"
    assert r["preview"]["data"]["ids"] == ["d123"]  # fact 6: the draft's own id
    assert rec["argv"][:5] == [
        str(runner.PROTON_BIN), "--output", "json", "--no-input", "--dry-run",
    ]
    assert tail_without_globals(rec["argv"]) == ["mail", "drafts", "send", "--", "d123"]

    r2 = tools.MAIL_TOOL.dispatch("drafts_send", {"id": "d123"}, confirm=True)
    assert "--dry-run" not in rec["argv"]
    assert r2["applied"] is True and r2["dry_run"] is False
    assert r2["data"]["ids"] == ["d123"]

    # The dry-run argv check still refuses a runner that drops the flag.
    def stripping_run(args, **kw):
        if "--dry-run" in args:
            runner.LAST_ARGV = [a for a in args if a != "--dry-run"]
        rec["argv"] = list(args)
        return make_proc()

    monkeypatch.setattr(subprocess, "run", stripping_run)
    with pytest.raises(RuntimeError):
        tools.MAIL_TOOL.dispatch("drafts_send", {"id": "d123"}, confirm=False)


def test_drafts_send_argv(rec):
    """Test 13: exact `mail drafts send -- <id>`."""
    rec["proc"] = proc_json(_sent_draft_payload())
    tools.MAIL_TOOL.dispatch("drafts_send", {"id": "d123"}, confirm=True)
    assert tail_without_globals(rec["argv"]) == ["mail", "drafts", "send", "--", "d123"]


def test_drafts_send_forbidden_flags_sweep(rec):
    """Test 14: no argv carries --send-at, --expires, --eo-password-file, or
    --eo-password-hint (D7: delivery flags unwired), in either confirm
    mode."""
    rec["proc"] = proc_json(_sent_draft_payload())
    forbidden = ["--send-at", "--expires", "--eo-password-file", "--eo-password-hint"]
    for params in ({"id": "d123"}, {"id": "-d123"}, {"id": "--d123"}):
        for confirm in (False, True):
            rec["argv"] = []
            r = tools.MAIL_TOOL.dispatch("drafts_send", params, confirm=confirm)
            assert r["success"] is True, (params, confirm, r)
            for flag in forbidden:
                assert flag not in rec["argv"], (params, flag)
            assert not [a for a in rec["argv"] if a.startswith("--eo-password")], params


def test_reply_draft_forbidden_flags_sweep(rec, spool):
    """Test 15: no reply_draft argv carries the unwired compose flags, any
    delivery or EO flag, or --no-quote (D7); --draft always present, before
    the `--`."""
    rec["proc"] = proc_json(_saved_payload())
    forbidden = [
        "--to", "--cc", "--bcc", "--attach", "--attach-inline", "--html",
        "--eml", "--from", "--no-signature", "--request-receipt", "--no-quote",
        "--send-at", "--expires", "--eo-password-file", "--eo-password-hint",
    ]
    staged = _staged(spool, "rb.txt")
    cases = [
        {"id": "m9", "body": "t"},
        {"id": "m9", "body": "t", "everyone": True},
        {"id": "-m9", "body": "t"},
        {"id": "m9", "body_path": str(staged)},
        {"id": "m9", "body_path": str(staged), "everyone": True},
    ]
    for params in cases:
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch("reply_draft", params)
        assert r["success"] is True, (params, r)
        args = rec["argv"]
        for flag in forbidden:
            assert flag not in args, (params, flag)
        assert not [a for a in args if a.startswith("--eo-password")], params
        assert "--draft" in args, params
        assert args.index("--") > args.index("--draft"), params


def test_drafts_create_forbidden_flags_sweep(rec, spool):
    """Test 16: no drafts_create argv carries --cc, --bcc, --attach,
    --attach-inline, --html, --eml, --from, --no-signature, or
    --request-receipt (D7)."""
    rec["proc"] = proc_json(_created_payload())
    forbidden = [
        "--cc", "--bcc", "--attach", "--attach-inline", "--html", "--eml",
        "--from", "--no-signature", "--request-receipt",
    ]
    staged = _staged(spool, "cb.txt")
    cases = [
        {"to": "x@y.z", "subject": "Hi", "body": "b"},
        {"to": "x@y.z", "subject": "", "body": "b"},
        {"to": "x@y.z", "subject": "Hi", "body_path": str(staged)},
    ]
    for params in cases:
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch("drafts_create", params)
        assert r["success"] is True, (params, r)
        for flag in forbidden:
            assert flag not in rec["argv"], (params, flag)
        assert not [a for a in rec["argv"] if a.startswith("--eo-password")], params


def test_ungated_drafts_never_dry_run_sweep(rec, spool):
    """Test 17: no argv for drafts_list, drafts_get, drafts_create, or
    reply_draft carries --dry-run in any case (the read class, the download
    precedent)."""
    staged = _staged(spool, "nd.txt")
    cases = [
        ("drafts_list", {}),
        ("drafts_list", {"sort": "size", "desc": True, "limit": 5, "page": 1}),
        ("drafts_list", {"confirm": False}),  # a stray confirm key changes nothing
        ("drafts_get", {"id": "d1"}),
        ("drafts_create", {"to": "x@y.z", "subject": "Hi", "body": "b"}),
        ("drafts_create", {"to": "x@y.z", "subject": "Hi", "body_path": str(staged)}),
        ("reply_draft", {"id": "m9", "body": "t"}),
        ("reply_draft", {"id": "m9", "body_path": str(staged), "everyone": True}),
    ]
    for action, params in cases:
        rec["argv"] = []
        rec["proc"] = proc_json({"drafts": [], "count": 0, "ok": True})
        r = tools.MAIL_TOOL.dispatch(action, params)
        assert r["success"] is True, (action, params, r)
        assert rec["argv"], (action, params)
        assert "--dry-run" not in rec["argv"], (action, params)
        assert "--dry-run" not in (runner.LAST_ARGV or []), (action, params)


def test_drafts_envelope_passthrough(rec):
    """Test 18: the runner's data reaches the envelope unchanged for create,
    reply, send, list, and get (no wrapper keys added)."""
    payloads = {
        "drafts_create": _created_payload(),
        "reply_draft": _saved_payload(),
        "drafts_send": _sent_draft_payload(),
        "drafts_list": _drafts_list_payload(2),
        "drafts_get": {"id": "d123", "subject": "Hi", "body": "hello"},
    }
    params_by_action = {
        "drafts_create": {"to": "x@y.z", "subject": "Hi", "body": "b"},
        "reply_draft": {"id": "m9", "body": "t"},
        "drafts_send": {"id": "d123"},
        "drafts_list": {},
        "drafts_get": {"id": "d123"},
    }
    for action, payload in payloads.items():
        rec["proc"] = proc_json(payload)
        r = tools.MAIL_TOOL.dispatch(
            action, params_by_action[action],
            confirm=True if action == "drafts_send" else None,
        )
        assert r["success"] is True, action
        assert r["data"] == payload, action


# --- list-param validation (test 19) -----------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "50"},
        {"limit": 1.5},
        {"limit": True},
        {"page": "2"},
        {"page": 2.5},
        {"page": False},
        {"desc": "yes"},
        {"desc": 1},
        {"sort": "subject"},
        {"sort": 5},
    ],
)
def test_drafts_list_validation(rec, params):
    """Test 19: a non-integer limit or page, a boolean limit or page, a
    non-boolean desc, and a sort outside {time, size} each refuse; no junk
    path emits argv."""
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch("drafts_list", params)
    assert r["success"] is False, params
    assert r["validation"] is True, params
    assert r["exit_code"] is None, params
    assert rec["argv"] == [], params

    # Valid shapes pass to argv: limit 0 lists all, a False desc omits the
    # bare switch, sort time rides through.
    rec["proc"] = proc_json(_drafts_list_payload())
    tools.MAIL_TOOL.dispatch("drafts_list", {"limit": 0, "desc": False, "sort": "time"})
    assert tail_without_globals(rec["argv"]) == [
        "mail", "drafts", "list", "--sort", "time", "--limit", "0",
    ]


# --- read and shape refusals (tests 22-23) ------------------------------------------


def test_body_path_read_refusals(rec, spool, monkeypatch):
    """Test 22: the vanished refusal is pinned for both sub-windows, vanish
    before the stat and vanish between the stat and the read (monkeypatched
    file calls), each refusing with the shipped vanished wording as a
    structured error; a file that is not valid UTF-8 refuses with a
    structured error; the CLI never runs in any case."""
    real_getsize = os.path.getsize  # captured before any patching
    real_remove = os.remove
    for action in ("drafts_create", "reply_draft"):
        base = {"to": "x@y.z", "subject": "Hi"} if action == "drafts_create" else {"id": "m9"}

        # Sub-window 1: vanish between the existence check and the stat.
        f1 = _staged(spool, "vanish1.txt", 5)

        def vanished_stat(p):
            raise FileNotFoundError(f"vanished: {p}")

        monkeypatch.setattr(os.path, "getsize", vanished_stat)
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch(action, dict(base, body_path=str(f1)))
        assert r["success"] is False, action
        assert r["validation"] is True, action
        assert "vanished during validation" in r["error"], (action, r["error"])
        assert rec["argv"] == [], action

        # Sub-window 2: vanish between the stat and the read. The fake
        # getsize reports the real size and then removes the file, so the
        # read opens into nothing.
        f2 = _staged(spool, "vanish2.txt", 5)

        def vanish_after_stat(p):
            size = real_getsize(p)
            real_remove(p)
            return size

        monkeypatch.setattr(os.path, "getsize", vanish_after_stat)
        rec["argv"] = []
        r = tools.MAIL_TOOL.dispatch(action, dict(base, body_path=str(f2)))
        assert r["success"] is False, action
        assert r["validation"] is True, action
        assert "vanished during validation" in r["error"], (action, r["error"])
        assert rec["argv"] == [], action
        assert not f2.exists()

    # Non-UTF-8 bytes refuse with a structured error (the stat restored).
    monkeypatch.setattr(os.path, "getsize", real_getsize)
    bad = spool / "bad.bin"
    bad.write_bytes(b"\xff\xfe\x00not utf-8")
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch(
        "drafts_create", {"to": "x@y.z", "subject": "Hi", "body_path": str(bad)}
    )
    assert r["success"] is False and r["validation"] is True
    assert "UTF-8" in r["error"]
    assert rec["argv"] == []


@pytest.mark.parametrize("action", ["drafts_create", "reply_draft"])
def test_body_and_everyone_shape_refusals(rec, spool, action):
    """Test 23: an empty inline body string refuses on both composing
    actions; a non-string body_path refuses; a zero-byte body_path file
    refuses with the empty-body wording; everyone refuses on reply_draft for
    any non-boolean, truthy or falsy; the CLI never runs in any case."""
    base = {"to": "x@y.z", "subject": "Hi"} if action == "drafts_create" else {"id": "m9"}

    # Empty inline body (D6's non-empty-body guarantee, wrapper-enforced).
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch(action, dict(base, body=""))
    assert r["success"] is False, action
    assert r["validation"] is True, action
    assert "non-empty 'body'" in r["error"], (action, r["error"])
    assert rec["argv"] == [], action

    # Non-string body_path.
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch(action, dict(base, body_path=5))
    assert r["success"] is False, action
    assert r["validation"] is True, action
    assert "body_path" in r["error"], (action, r["error"])
    assert rec["argv"] == [], action

    # Zero-byte spool file: the same D6 guarantee on the spool path.
    zero = spool / "zero.txt"
    zero.write_bytes(b"")
    rec["argv"] = []
    r = tools.MAIL_TOOL.dispatch(action, dict(base, body_path=str(zero)))
    assert r["success"] is False, action
    assert r["validation"] is True, action
    assert "non-empty 'body'" in r["error"], (action, r["error"])
    assert rec["argv"] == [], action

    if action == "reply_draft":
        # everyone: any non-boolean refuses, falsy ones included, with no
        # silent coercion.
        for bad in (0, "", "x", 1, [], {}):
            rec["argv"] = []
            r = tools.MAIL_TOOL.dispatch(
                "reply_draft", {"id": "m9", "body": "t", "everyone": bad}
            )
            assert r["success"] is False, bad
            assert r["validation"] is True, bad
            assert "everyone" in r["error"], bad
            assert rec["argv"] == [], bad
