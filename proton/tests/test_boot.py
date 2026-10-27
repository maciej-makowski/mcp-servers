"""Boot test: the stdio server must exit cleanly on EOF (closed stdin).

This was verified live during Task 5 but never tested — here it is as an
automated check: spawn `python -m proton_mcp.server` with stdin closed, give
it a timeout, and require exit code 0 (clean EOF, no traceback, no hang).
"""

import subprocess
import sys

import pytest


def test_server_exits_cleanly_on_closed_stdin():
    proc = subprocess.run(
        [sys.executable, "-m", "proton_mcp.server"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"exit {proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )


@pytest.mark.parametrize("bad_args", [["--help"], ["definitely-not-a-flag"]])
def test_server_flags_dont_crash_boot(bad_args):
    """Extra argv must not make the server exit non-zero at boot."""
    proc = subprocess.run(
        [sys.executable, "-m", "proton_mcp.server", *bad_args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\nstderr: {proc.stderr!r}"
