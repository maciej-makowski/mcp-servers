"""Tests for the MCP wiring (tests/test_mcp_tools.py, Task 4)."""

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from proton_mcp import runner
from proton_mcp import server as srv
from proton_mcp import tools

EXPECTED_TOOLS = {"proton_mail", "proton_calendar", "proton_drive", "proton_photos"}


def test_four_tools_registered():
    assert set(srv.TOOL_SPECS) == EXPECTED_TOOLS
    assert set(srv.INPUT_SCHEMAS) == EXPECTED_TOOLS


def test_action_enums_mirror_tool_layer():
    mail = srv.INPUT_SCHEMAS["proton_mail"]["properties"]["action"]["enum"]
    assert set(mail) == {"status", "list", "search", "get",
                         "attachments_list", "attachments_download",
                         "conversation_attachments_list",
                         "conversation_attachments_download",
                         "send", "move", "label", "unlabel", "mark", "trash"}
    cal = srv.INPUT_SCHEMAS["proton_calendar"]["properties"]["action"]["enum"]
    assert set(cal) == {"calendars", "events_list", "events_get",
                        "events_create", "events_delete"}
    drv = srv.INPUT_SCHEMAS["proton_drive"]["properties"]["action"]["enum"]
    assert set(drv) == {"list", "get", "download", "upload", "trash"}
    ph = srv.INPUT_SCHEMAS["proton_photos"]["properties"]["action"]["enum"]
    assert set(ph) == {"list", "albums", "download", "trash", "delete",
                       "favorite", "unfavorite"}


def test_gated_actions_have_confirm_default_false():
    for name, spec in srv.TOOL_SPECS.items():
        schema = srv.INPUT_SCHEMAS[name]
        assert "confirm" in schema["properties"], name
        assert schema["properties"]["confirm"]["default"] is False
        # ...and every gated action is covered by that schema
        for action in spec["gated"]:
            assert action in schema["properties"]["action"]["enum"]


def test_read_only_actions_have_no_confirm():
    """Reads must not advertise confirm at all in their enum/schema pairing."""
    for name, spec in srv.TOOL_SPECS.items():
        enum = set(srv.INPUT_SCHEMAS[name]["properties"]["action"]["enum"])
        gated = set(spec["gated"])
        assert enum == gated | set(spec["reads"])
        # The schema-level confirm property exists only on gated tools; for
        # read-only tool surfaces there'd be none — all three tools here have
        # gated actions, so assert the gating doc explains the flow instead.
        assert "confirm=false" in srv.TOOL_SPECS[name]["description"]
        assert "--dry-run" in srv.TOOL_SPECS[name]["description"]


def test_mail_filter_schema_mirrors_filter_keys():
    filt = srv.INPUT_SCHEMAS["proton_mail"]["properties"]["params"]["properties"]["filter"]
    keys = set(filt["properties"])
    assert keys == {
        "folder", "from", "to", "subject", "keyword", "after", "before",
        "older_than", "newer_than", "has_attachments", "read", "unread",
        "starred", "limit", "all",
    }
    assert filt["properties"]["limit"]["type"] == "integer"
    assert filt["properties"]["all"]["type"] == "boolean"


def test_calendar_schema_has_no_mail_filter():
    props = srv.INPUT_SCHEMAS["proton_calendar"]["properties"]["params"]["properties"]
    assert "filter" not in props


@pytest.fixture
def rec(monkeypatch, tmp_path):
    """Recorder over subprocess.run with a fake PROTON_BIN (same shape as test_tools)."""
    fake_bin = tmp_path / "proton"
    fake_bin.write_text("#!/bin/sh\ntrue\n")
    monkeypatch.setattr(runner, "PROTON_BIN", fake_bin)
    calls = {"argv": []}

    def fake_run(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        calls["argv"] = list(args)
        calls["input"] = input
        return SimpleNamespace(stdout=b'{"ok": true}', stderr=b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_handler_dispatches_read(rec):
    r = asyncio.run(srv.call_tool_impl("proton_mail", {"action": "status"}))
    assert r["success"] is True
    assert "--dry-run" not in rec["argv"]


def test_handler_gated_default_is_dry_run_preview(rec):
    r = asyncio.run(srv.call_tool_impl("proton_drive", {"action": "trash", "params": {"refs": ["1"]}}))
    assert r["dry_run"] is True
    assert r["applied"] is False
    assert "--dry-run" in rec["argv"]


def test_handler_gated_confirm_true_runs_real(rec):
    r = asyncio.run(
        srv.call_tool_impl(
            "proton_drive", {"action": "trash", "params": {"refs": ["1"]}, "confirm": True}
        )
    )
    assert r["dry_run"] is False
    assert "--dry-run" not in rec["argv"]


def test_handler_rejects_unknown_tool():
    with pytest.raises(ValueError):
        asyncio.run(srv.call_tool_impl("proton_nope", {"action": "list"}))


def test_handler_rejects_missing_action():
    with pytest.raises(ValueError):
        asyncio.run(srv.call_tool_impl("proton_mail", {}))


def test_list_tools_and_call_tool_via_lowlevel_server(rec):
    """The registered handlers work against the real lowlevel Server objects."""
    s = srv.make_server()

    async def run():
        listed = await s._request_handlers["tools/list"].handler(None, None)
        called = await s._request_handlers["tools/call"].handler(
            None,
            SimpleNamespace(
                name="proton_calendar",
                arguments={"action": "events_list", "params": {"calendar": "Work"}},
            ),
        )
        return listed, called

    listed, called = asyncio.run(run())
    names = [t.name for t in listed.tools]
    assert names == ["proton_mail", "proton_calendar", "proton_drive", "proton_photos"]
    payload = json.loads(called.content[0].text)
    assert payload["success"] is True
    tail = rec["argv"][4:]  # drop binary path + prepended global flags
    assert tail[:3] == ["calendar", "events", "list"]
    assert "--calendar" in tail and "Work" in tail


def test_schemas_are_valid_json_schemas():
    for name, schema in srv.INPUT_SCHEMAS.items():
        assert schema["type"] == "object"
        assert schema["required"] == ["action"]
        for action in schema["properties"]["action"]["enum"]:
            assert isinstance(action, str)


def test_async_path_dispatches_via_to_thread(rec):
    """The async call_tool_impl runs the blocking dispatch off the event loop."""
    r = asyncio.run(srv.call_tool_impl("proton_mail", {"action": "status"}))
    assert r["success"] is True
    assert "status" in rec["argv"] or "count" in rec["argv"]


def test_confirm_top_level_true_runs_real(rec):
    r = asyncio.run(
        srv.call_tool_impl("proton_drive", {"action": "trash", "params": {"refs": ["1"]}, "confirm": True})
    )
    assert r["dry_run"] is False


def test_confirm_params_placement_true_runs_real(rec):
    r = asyncio.run(
        srv.call_tool_impl("proton_drive", {"action": "trash", "params": {"refs": ["1"], "confirm": True}})
    )
    assert r["dry_run"] is False


def test_confirm_params_false_overrides_top_level_true(rec):
    """params.confirm wins: false there keeps the dry-run fail-safe default."""
    r = asyncio.run(
        srv.call_tool_impl(
            "proton_drive",
            {"action": "trash", "params": {"refs": ["1"], "confirm": False}, "confirm": True},
        )
    )
    assert r["dry_run"] is True
    assert "--dry-run" in rec["argv"]


# --- attachment actions: schema + runner template (design §5 items 16-17) -------


def test_proton_mail_schema_carries_attachment_actions_and_params():
    """Item 16: the enum carries all four new actions; the params properties
    carry the four new keys with the documented types; no confirm requirement
    is attached to read actions."""
    mail = srv.INPUT_SCHEMAS["proton_mail"]["properties"]["action"]["enum"]
    for action in (
        "attachments_list",
        "attachments_download",
        "conversation_attachments_list",
        "conversation_attachments_download",
    ):
        assert action in mail
    props = srv.INPUT_SCHEMAS["proton_mail"]["properties"]["params"]["properties"]
    for key, typ in (
        ("attachment", "string"),
        ("dest_dir", "string"),
        ("force", "boolean"),
        ("include_inline", "boolean"),
    ):
        assert props[key]["type"] == typ, key
    assert props["id"]["description"] == (
        "Message or conversation reference (get and attachment actions)"
    )
    spec = srv.TOOL_SPECS["proton_mail"]
    gated = set(spec["gated"])
    for action in spec["reads"]:
        assert action not in gated  # reads are never confirm-gated
    schema = srv.INPUT_SCHEMAS["proton_mail"]
    assert schema["properties"]["confirm"]["default"] is False
    assert schema["required"] == ["action"]
    assert "Attachment list/download reads" in spec["description"]


def test_runner_template_prepends_global_flags(rec, tmp_path, monkeypatch):
    """Item 17: every argv starts [BIN, --output, json, --no-input] before the
    verb tokens (--dry-run immediately after, gated calls only), and the
    dry-run argv check asserts that prepended region as a whole prefix.
    Shape regression: a single-dash-id get now produces prepended flags before
    the positional — under the old trailing-flags template the CLI's
    preprocessArgs demoted those flags to positionals once a dash-id was
    marked (an argument-count refusal for ~1 in 64 ids)."""
    dest = tmp_path / "receive-template"
    monkeypatch.setattr(tools, "DEFAULT_ATTACHMENT_DEST_DIR", str(dest))
    cases = [
        ("status", {}, False),
        ("list", {}, False),
        ("search", {"query": "x"}, False),
        ("get", {"id": "abc"}, False),
        ("attachments_list", {"id": "abc"}, False),
        ("attachments_download", {"id": "abc"}, False),
        ("send", {"to": "x@y.z", "subject": "s", "body": "b"}, True),
        ("trash", {"refs": ["1"]}, True),
    ]
    for action, params, gated in cases:
        rec["argv"] = []
        tools.MAIL_TOOL.dispatch(action, params)  # confirm omitted: gated -> dry-run default
        prefix = [str(runner.PROTON_BIN), "--output", "json", "--no-input"]
        if gated:
            prefix += ["--dry-run"]
        assert rec["argv"][: len(prefix)] == prefix, (action, rec["argv"])
        verb_tokens = rec["argv"][len(prefix):]
        assert verb_tokens, action
        assert "--output" not in verb_tokens and "--no-input" not in verb_tokens
        if not gated:
            assert "--dry-run" not in verb_tokens

    # Shape regression: single-dash-id get — prepended flags before the
    # positional, nothing trailing to demote.
    tools.MAIL_TOOL.dispatch("get", {"id": "-abc123"})
    args = rec["argv"]
    assert args[:4] == [str(runner.PROTON_BIN), "--output", "json", "--no-input"]
    assert args[4:] == ["mail", "messages", "get", "-abc123"]

    # The dry-run argv check asserts that prepended region as a whole prefix:
    # a runner whose argv lost the --dry-run breaks the prefix and the call
    # refuses before anything is applied.
    def stripping_run(args, **kw):
        runner.LAST_ARGV = [a for a in args if a != "--dry-run"]
        return SimpleNamespace(stdout=b"{}", stderr=b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", stripping_run)
    with pytest.raises(RuntimeError, match="dry-run argv check"):
        tools.MAIL_TOOL.dispatch("trash", {"refs": ["1"], "confirm": False})
