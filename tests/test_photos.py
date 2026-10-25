"""Tests for the Photos tool family (mocked subprocess, no network, no real proton).

CLI contract verified against the real binary (~/.hermes/bin/proton v5.1.0)
`--help` output:
- `proton drive photos list [--album NAME] [--tag TAG] [--limit N]
  [--page N] [--sort STR] [--desc]` (sort: captured)
- `proton drive photos albums list [--limit N] [--page N] [--sort STR]
  [--desc]` (sort: name, photos)
- `proton drive photos download REF (--dest PATH | --dest-dir DIR) [--force]`
- `proton drive photos {trash,delete,favorite,unfavorite} REF... [flags]`
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from proton_mcp import runner
from proton_mcp.photos import PHOTOS_TOOL

PHOTOS = PHOTOS_TOOL

NEXT_STEP = "Nothing changed. Ask user to approve, then call again with confirm=true."


def make_proc(stdout: str = '{"ok": true}', returncode: int = 0):
    return SimpleNamespace(stdout=stdout.encode(), stderr=b"", returncode=returncode)


@pytest.fixture
def rec(monkeypatch, tmp_path):
    fake_bin = tmp_path / "proton"
    fake_bin.write_text("#!/bin/sh\ntrue\n")
    monkeypatch.setattr(runner, "PROTON_BIN", fake_bin)
    calls = {"argv": []}

    def fake_run(args, input=None, capture_output=True, text=True, timeout=None, **kw):
        calls["argv"] = list(args)
        calls["input"] = input
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
    return argv[1:-3]


# --- reads: argv shapes -------------------------------------------------------


def test_list_plain_argv(rec):
    PHOTOS.dispatch("list", {})
    assert tail_without_globals(rec["argv"]) == ["drive", "photos", "list"]


def test_list_full_flag_mapping(rec):
    PHOTOS.dispatch(
        "list",
        {"album": "Holidays", "tag": "favorites", "limit": 50,
         "page": 2, "sort": "captured", "desc": True},
    )
    args = tail_without_globals(rec["argv"])
    assert args[:3] == ["drive", "photos", "list"]
    for flag, value in (("--album", "Holidays"), ("--tag", "favorites"),
                        ("--limit", "50"), ("--page", "2"), ("--sort", "captured")):
        assert flag in args and value in args, (flag, value)
    assert "--desc" in args
    # kebab-case only, no snake_case leakage
    assert "--desc" in args and "desc" not in args[:3]


def test_list_desc_false_omits_flag(rec):
    PHOTOS.dispatch("list", {"desc": False})
    args = tail_without_globals(rec["argv"])
    assert args == ["drive", "photos", "list"]


def test_list_limit_must_be_int(rec):
    r = PHOTOS.dispatch("list", {"limit": "ten"})
    assert r["success"] is False
    assert r["validation"] is True


def test_list_bool_limit_rejected(rec):
    r = PHOTOS.dispatch("list", {"limit": True})
    assert r["validation"] is True


def test_list_tag_must_be_known(rec):
    r = PHOTOS.dispatch("list", {"tag": "not-a-tag"})
    assert r["success"] is False
    assert r["validation"] is True


def test_list_known_tags_accepted(rec):
    for tag in ("favorites", "screenshots", "videos", "live-photos",
                "motion-photos", "selfies", "portraits", "bursts",
                "panoramas", "raw"):
        PHOTOS.dispatch("list", {"tag": tag})
        args = tail_without_globals(rec["argv"])
        assert "--tag" in args and tag in args


def test_albums_argv(rec):
    PHOTOS.dispatch("albums", {})
    assert tail_without_globals(rec["argv"]) == ["drive", "photos", "albums", "list"]


def test_albums_flag_mapping(rec):
    PHOTOS.dispatch("albums", {"limit": 10, "page": 1, "sort": "photos", "desc": True})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["drive", "photos", "albums", "list"]
    for flag, value in (("--limit", "10"), ("--page", "1"), ("--sort", "photos")):
        assert flag in args and value in args, (flag, value)
    assert "--desc" in args


def test_albums_limit_must_be_int(rec):
    r = PHOTOS.dispatch("albums", {"limit": "x"})
    assert r["validation"] is True


def test_download_requires_ref_and_dest_dir(rec):
    r = PHOTOS.dispatch("download", {"ref": "ca_XYZ"})
    assert r["success"] is False and r["validation"] is True
    r = PHOTOS.dispatch("download", {"dest_dir": "/tmp"})
    assert r["success"] is False and r["validation"] is True


def test_download_argv_uses_dest_dir(rec):
    PHOTOS.dispatch("download", {"ref": "ca_XYZ", "dest_dir": "/tmp/pics"})
    args = tail_without_globals(rec["argv"])
    assert args[:4] == ["drive", "photos", "download", "ca_XYZ"]
    assert "--dest-dir" in args and "/tmp/pics" in args
    assert "--dest" not in args  # dest_dir only; never bare --dest


def test_download_force_flag(rec):
    PHOTOS.dispatch("download", {"ref": "ca_XYZ", "dest_dir": "/tmp", "force": True})
    assert "--force" in tail_without_globals(rec["argv"])


def test_download_never_dry_runs(rec):
    r = PHOTOS.dispatch("download", {"ref": "ca_XYZ", "dest_dir": "/tmp"})
    assert r["success"] is True
    assert "--dry-run" not in rec["argv"]


def test_reads_never_dry_run(rec):
    for action, params in (
        ("list", {}),
        ("albums", {}),
        ("download", {"ref": "ca_XYZ", "dest_dir": "/tmp"}),
    ):
        r = PHOTOS.dispatch(action, params)
        assert r["success"] is True, (action, r)
        assert "--dry-run" not in rec["argv"]
        assert runner.LAST_ARGV == rec["argv"]


# --- gated actions ------------------------------------------------------------


def test_gated_actions_declared():
    assert PHOTOS.GATED_ACTIONS == frozenset(
        {"trash", "delete", "favorite", "unfavorite"}
    )


@pytest.mark.parametrize("action", ["trash", "delete", "favorite", "unfavorite"])
def test_gated_default_is_dry_run_preview(rec, action):
    r = PHOTOS.dispatch(action, {"refs": ["ca_1", "ca_2"]})
    assert r["dry_run"] is True
    assert r["applied"] is False
    assert "next_step" in r
    assert "--dry-run" in rec["argv"]
    args = tail_without_globals(rec["argv"])
    assert args == ["drive", "photos", action, "ca_1", "ca_2"]


@pytest.mark.parametrize("action", ["trash", "delete", "favorite", "unfavorite"])
def test_gated_confirm_true_runs_real(rec, action):
    r = PHOTOS.dispatch(action, {"refs": ["ca_1"]}, confirm=True)
    assert r["dry_run"] is False
    assert r["applied"] in (True, r["success"])
    assert "--dry-run" not in rec["argv"]


@pytest.mark.parametrize("action", ["trash", "delete", "favorite", "unfavorite"])
def test_gated_requires_refs(rec, action):
    r = PHOTOS.dispatch(action, {})
    assert r["success"] is False
    assert r["preview"]["validation"] is True  # validation errors surface in the dry-run preview


def test_single_ref_accepted(rec):
    r = PHOTOS.dispatch("trash", {"ref": "ca_9"}, confirm=True)
    assert r["success"] is True
    assert tail_without_globals(rec["argv"]) == ["drive", "photos", "trash", "ca_9"]


def test_refs_and_ref_mutually_exclusive(rec):
    r = PHOTOS.dispatch("trash", {"ref": "a", "refs": ["b"]}, confirm=True)
    assert r["success"] is False
    assert r["validation"] is True


def test_unknown_action_rejected():
    with pytest.raises(ValueError):
        PHOTOS.dispatch("upload", {})
    with pytest.raises(ValueError):
        PHOTOS.dispatch("get", {"ref": "x"})
