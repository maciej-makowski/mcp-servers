"""Drive-download size budget (PROTON_DRIVE_DOWNLOAD_BUDGET_MB): the metadata
pre-flight gates oversize items before any bytes move, and the CLI timeout
derives from the actual size. The runner is faked; no CLI runs, no network."""

from __future__ import annotations

import pytest

from proton_mcp import runner, tools


def _get_envelope(size):
    return {
        "success": True,
        "exit_code": 0,
        "auth_required": False,
        "data": {
            "name": "x.zip",
            "mimeType": "application/zip",
            "original_size_bytes": size,
            "size_bytes": size + 56,
        },
    }


def _download_envelope():
    return {"success": True, "exit_code": 0, "auth_required": False, "data": {"count": 1}}


@pytest.fixture
def fake_runner(monkeypatch):
    calls: list[dict] = []
    state: dict = {"size": None}

    def fake_run(args, stdin_text=None, dry_run=False, timeout=None):
        calls.append({"args": list(args), "timeout": timeout, "dry_run": dry_run})
        runner.LAST_ARGV = list(args)
        if args[:3] == ["drive", "items", "get"]:
            if state["size"] is None:
                return {"success": True, "exit_code": 0, "data": {}}
            return _get_envelope(state["size"])
        if args[:3] == ["drive", "items", "download"]:
            return _download_envelope()
        raise AssertionError(f"unexpected argv {args}")

    monkeypatch.setattr(runner, "run", fake_run)
    return calls, state


def _download(params):
    return tools.DRIVE_TOOL.dispatch("download", params)


def test_default_budget_is_525_and_admits_475mb(fake_runner, monkeypatch):
    monkeypatch.delenv("PROTON_DRIVE_DOWNLOAD_BUDGET_MB", raising=False)
    calls, state = fake_runner
    state["size"] = 498_000_000  # ~475 MB
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    assert res["success"] is True
    dl = [c for c in calls if c["args"][:3] == ["drive", "items", "download"]]
    assert dl and dl[0]["timeout"] == 950  # min(1800, max(60, 475 * 2))
    assert tools._drive_download_budget_mb() == 525


def test_env_override_refuses_oversize_before_download(fake_runner, monkeypatch):
    monkeypatch.setenv("PROTON_DRIVE_DOWNLOAD_BUDGET_MB", "1")
    calls, state = fake_runner
    state["size"] = 498_000_000
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    assert res["success"] is False and res["validation"] is True
    assert "budget" in res["error"]
    assert not [c for c in calls if c["args"][:3] == ["drive", "items", "download"]]


def test_timeout_floors_at_60_for_small_items(fake_runner, monkeypatch):
    monkeypatch.delenv("PROTON_DRIVE_DOWNLOAD_BUDGET_MB", raising=False)
    calls, state = fake_runner
    state["size"] = 10 * 1024 * 1024
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    dl = [c for c in calls if c["args"][:3] == ["drive", "items", "download"]]
    assert dl[0]["timeout"] == 60


def test_timeout_caps_at_1800_for_huge_budgets(fake_runner, monkeypatch):
    monkeypatch.setenv("PROTON_DRIVE_DOWNLOAD_BUDGET_MB", "4000")
    calls, state = fake_runner
    state["size"] = 3000 * 1024 * 1024
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    dl = [c for c in calls if c["args"][:3] == ["drive", "items", "download"]]
    assert dl[0]["timeout"] == 1800


def test_unparseable_env_falls_back_to_default(fake_runner, monkeypatch):
    monkeypatch.setenv("PROTON_DRIVE_DOWNLOAD_BUDGET_MB", "abc")
    calls, state = fake_runner
    state["size"] = 498_000_000
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    assert res["success"] is True
    assert tools._drive_download_budget_mb() == 525


def test_nonpositive_env_falls_back_to_default(fake_runner, monkeypatch):
    monkeypatch.setenv("PROTON_DRIVE_DOWNLOAD_BUDGET_MB", "0")
    calls, state = fake_runner
    state["size"] = 498_000_000
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    assert res["success"] is True
    assert tools._drive_download_budget_mb() == 525


def test_missing_size_metadata_proceeds_at_full_budget_timeout(fake_runner):
    calls, state = fake_runner
    state["size"] = None  # the metadata carries no size fields
    res = _download({"path": "/x.zip", "dest_dir": "/srv/mcp_files/receive"})
    assert res["success"] is True
    dl = [c for c in calls if c["args"][:3] == ["drive", "items", "download"]]
    assert dl[0]["timeout"] == 1050  # min(1800, max(60, 525 * 2))
