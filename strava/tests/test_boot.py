"""Boot test: the stdio server must exit cleanly on EOF (closed stdin)."""

import os
import subprocess
import sys

import pytest


def _env_without_http():
    env = dict(os.environ)
    env.pop("MCP_BEARER", None)
    env.pop("MCP_PORT", None)
    return env


def test_server_exits_cleanly_on_closed_stdin():
    proc = subprocess.run(
        [sys.executable, "-m", "strava_mcp.server"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=_env_without_http(),
    )
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )


@pytest.mark.parametrize("bad_args", [["--help"], ["definitely-not-a-flag"]])
def test_server_flags_dont_crash_boot(bad_args):
    """Extra argv must not make the server exit non-zero at boot."""
    proc = subprocess.run(
        [sys.executable, "-m", "strava_mcp.server", *bad_args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=_env_without_http(),
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\nstderr: {proc.stderr!r}"
