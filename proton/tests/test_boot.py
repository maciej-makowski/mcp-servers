"""Boot test: the stdio server must exit cleanly on EOF (closed stdin).

This was verified live during Task 5 but never tested — here it is as an
automated check: spawn `python -m proton_mcp.server` with stdin closed, give
it a timeout, and require exit code 0 (clean EOF, no traceback, no hang).
"""

import os
import subprocess
import sys

import pytest

# The spawned server must boot in stdio mode regardless of the caller's
# environment: MCP_BEARER/MCP_PORT select the HTTP transport, so on a box
# where those are set (this deployment's own host carries MCP_BEARER in the
# session environment) the child would try to bind the live port 8010 and
# die on Address already in use instead of exiting on closed stdin.
BOOT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}


def test_server_exits_cleanly_on_closed_stdin():
    proc = subprocess.run(
        [sys.executable, "-m", "proton_mcp.server"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env=BOOT_ENV,
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
        env=BOOT_ENV,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\nstderr: {proc.stderr!r}"
