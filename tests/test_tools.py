"""Tests for the MCP tools layer (all mocked subprocess, no network, no real proton)."""

from __future__ import annotations

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
    """Strip the runner's --output json --no-input (and optional --dry-run) suffix."""
    tail = argv[-1:]
    argv = list(argv)
    if tail == ["--dry-run"]:
        argv = argv[:-1]
    assert argv[-3:] == ["--output", "json", "--no-input"]
    return argv[1:-3]  # drop the binary path


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


# --- belt-and-braces: RuntimeError if runner drops --dry-run --------------------


def test_belt_and_braces_runtime_error_if_dry_run_dropped(rec, monkeypatch):
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
