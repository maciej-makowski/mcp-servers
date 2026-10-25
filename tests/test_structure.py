"""Structural tests: module import independence + refs type validation (I1).

C1 regression guard: photos.py must not re-enter tools.py on import, so every
package module imports cleanly in any order — in particular
`import proton_mcp.photos` alone (the verified crash was a circular import:
photos -> tools -> late-import photos re-entering a partially-initialized
photos module).

I1: a string refs="ca_1" used to be iterated per-character and fed to a
PERMANENT delete. All gated paths that consume refs must reject non-list refs.
"""

from __future__ import annotations

import subprocess
import sys

from proton_mcp import runner


def make_proc(stdout: str = '{"ok": true}', returncode: int = 0):
    from types import SimpleNamespace

    return SimpleNamespace(stdout=stdout.encode(), stderr=b"", returncode=returncode)


import pytest


@pytest.fixture
def rec(monkeypatch, tmp_path):
    """Recorder over subprocess.run with a fake PROTON_BIN (same as test_tools)."""
    fake_bin = tmp_path / "proton"
    fake_bin.write_text("#!/bin/sh\ntrue\n")
    monkeypatch.setattr(runner, "PROTON_BIN", fake_bin)
    calls = {"argv": []}

    def fake_run(argv, **kwargs):
        calls["argv"] = list(argv)
        kwargs.setdefault("capture_output", True)
        return make_proc()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    return calls


def test_direct_import_photos_no_circular_import():
    code = "import proton_mcp.photos; print(proton_mcp.photos.PHOTOS_TOOL is not None)"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert "True" in proc.stdout


def _assert_import_ok(module: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"{module}: {proc.stderr}"


def test_each_module_imports_independently():
    for module in (
        "proton_mcp",
        "proton_mcp.base",
        "proton_mcp.runner",
        "proton_mcp.photos",
        "proton_mcp.tools",
        "proton_mcp.server",
    ):
        _assert_import_ok(module)


def test_tools_public_api_preserved():
    """The refactor must keep proton_mcp.tools.PHOTOS_TOOL and helpers."""
    from proton_mcp import tools

    assert tools.PHOTOS_TOOL is not None
    assert tools.MAIL_TOOL is not None
    assert tools.CALENDAR_TOOL is not None
    assert tools.DRIVE_TOOL is not None
    for name in ("_BaseTool", "_result", "_limit_error", "_map_filter", "_dry_run_envelope"):
        assert hasattr(tools, name), name


# -- I1: refs must be a list of str/int ---------------------------------------


def _validation(res: dict) -> bool:
    return res.get("validation") is True


def test_mail_string_refs_rejected(rec):
    from proton_mcp import tools

    r = tools.MAIL_TOOL.dispatch("trash", {"refs": "1", "confirm": True})
    assert _validation(r)
    assert rec["argv"] == []  # CLI never ran


def test_mail_dict_refs_rejected(rec):
    from proton_mcp import tools

    r = tools.MAIL_TOOL.dispatch("trash", {"refs": {"1": True}, "confirm": True})
    assert _validation(r)
    assert rec["argv"] == []


def test_drive_string_refs_rejected(rec):
    from proton_mcp import tools

    r = tools.DRIVE_TOOL.dispatch("trash", {"refs": "42", "confirm": True})
    assert _validation(r)
    assert rec["argv"] == []


def test_drive_string_refs_rejected_dry_run(rec):
    from proton_mcp import tools

    r = tools.DRIVE_TOOL.dispatch("trash", {"refs": "42", "confirm": False})
    assert r.get("applied") is False
    assert r["preview"].get("validation") is True
    assert "list of ids" in r["preview"]["error"]
    assert rec["argv"] == []


def test_photos_string_refs_rejected_permanent_delete(rec):
    from proton_mcp.photos import PHOTOS_TOOL

    r = PHOTOS_TOOL.dispatch("delete", {"refs": "ca_1", "confirm": True})
    assert _validation(r)
    assert rec["argv"] == []


def test_photos_single_ref_string_still_ok(rec):
    """The 'ref' (single) path stays a string — must still work."""
    from proton_mcp.photos import PHOTOS_TOOL

    r = PHOTOS_TOOL.dispatch("delete", {"ref": "ca_9", "confirm": True})
    assert r.get("success") is True
    assert "ca_9" in rec["argv"]


def test_photos_refs_non_string_items_rejected(rec):
    from proton_mcp.photos import PHOTOS_TOOL

    r = PHOTOS_TOOL.dispatch("delete", {"refs": ["ca_1", None], "confirm": True})
    assert _validation(r)
    assert rec["argv"] == []


def test_photos_refs_int_items_accepted(rec):
    from proton_mcp.photos import PHOTOS_TOOL

    r = PHOTOS_TOOL.dispatch("favorite", {"refs": [1, 2], "confirm": True})
    assert r.get("success") is True
    assert "1" in rec["argv"] and "2" in rec["argv"]


def test_calendar_list_id_rejected(rec):
    """Calendar's gated path takes a scalar id; a list/dict id is rejected."""
    from proton_mcp import tools

    r = tools.CALENDAR_TOOL.dispatch("events_delete", {"id": ["abc"], "confirm": True})
    assert _validation(r)
    assert rec["argv"] == []
