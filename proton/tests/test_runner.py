"""Tests for the proton-cli subprocess runner (mocked subprocess only)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from proton_mcp import runner

BIN = str(runner.PROTON_BIN)


class FakeCompleted(SimpleNamespace):
    pass


def make_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> FakeCompleted:
    return FakeCompleted(stdout=stdout.encode(), stderr=stderr.encode(), returncode=returncode)


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """Replace subprocess.run with a recorder returning a canned proc."""
    calls: dict = {}

    def fake_run(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        calls["args"] = args
        calls["input"] = input
        calls["timeout"] = timeout
        calls["kwargs"] = kw
        return calls.get("proc", make_proc())

    monkeypatch.setattr(subprocess, "run", fake_run)
    # runner.run() checks PROTON_BIN.exists() before touching subprocess;
    # point it at a fake binary so tests pass where ~/.hermes/bin/proton
    # doesn't exist (e.g. CI runners).
    fake_bin = tmp_path / "proton"
    fake_bin.touch()
    monkeypatch.setattr(runner, "PROTON_BIN", fake_bin)
    globals()["BIN"] = str(fake_bin)
    return calls


# --- exit code mapping -------------------------------------------------------

CODE_MEANINGS = {
    0: "ok",
    1: "bad-argument",
    2: "auth-failed",
    3: "not-found",
    4: "ambiguous",
    5: "network-or-server-error",
    130: "cancelled",
}


@pytest.mark.parametrize("code,meaning", sorted(CODE_MEANINGS.items()))
def test_exit_code_map(recorder, code, meaning):
    recorder["proc"] = make_proc(returncode=code, stderr="boom")
    r = runner.run(["mail", "messages", "list"])
    assert r["exit_code"] == code
    assert r["meaning"] == meaning
    assert r["success"] is (code == 0)
    assert r["auth_required"] is (code == 2)


def test_success_with_json_data(recorder):
    recorder["proc"] = make_proc(stdout='{"messages": []}')
    r = runner.run(["mail", "messages", "list"])
    assert r["success"] is True
    assert r["data"] == {"messages": []}


def test_json_parse_failure_falls_back_to_raw_text(recorder):
    recorder["proc"] = make_proc(stdout="not json at all", returncode=0)
    r = runner.run(["version"])
    assert r["success"] is True
    assert r["data"] is None
    assert r["raw"] == "not json at all"


# --- argument construction ---------------------------------------------------

def test_appends_output_json_and_no_input(recorder):
    recorder["proc"] = make_proc()
    runner.run(["mail", "messages", "list"])
    args = recorder["args"]
    assert args[:3] == [BIN, "mail", "messages"]
    assert args[-4:] == ["list", "--output", "json", "--no-input"]


def test_no_dry_run_by_default(recorder):
    recorder["proc"] = make_proc()
    runner.run(["mail", "messages", "trash"])
    assert "--dry-run" not in recorder["args"]


def test_dry_run_flag_appended(recorder):
    recorder["proc"] = make_proc()
    runner.run(["mail", "messages", "trash"], dry_run=True)
    assert recorder["args"][-1] == "--dry-run"


# --- stdin -------------------------------------------------------------------

def test_stdin_text_piped_not_argv(recorder):
    recorder["proc"] = make_proc()
    body = "line1\nline2 with 'quotes' & $ters"
    runner.run(["mail", "messages", "send"], stdin_text=body)
    assert recorder["input"] == body
    assert body not in " ".join(recorder["args"])


def test_no_stdin_passes_none(recorder):
    recorder["proc"] = make_proc()
    runner.run(["mail", "messages", "list"])
    assert recorder["input"] is None


# --- failures ----------------------------------------------------------------

def test_timeout_returns_friendly_error(recorder, monkeypatch):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw.get("timeout", 60))

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = runner.run(["mail", "messages", "list"], timeout=5)
    assert r["success"] is False
    assert r["exit_code"] is None
    assert "timed out" in r["error"].lower()


def test_missing_binary_error(monkeypatch, recorder):
    # simulate FileNotFoundError from subprocess
    def fake_run(*a, **kw):
        raise FileNotFoundError("No such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = runner.run(["version"])
    assert r["success"] is False
    assert BIN in r["error"]
