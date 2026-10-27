"""Subprocess runner for the proton-cli binary.

Every invocation: run `proton <args...> --output json --no-input`, pipe stdin
text over stdin (never argv), parse stdout as JSON (fallback: raw text) and map
the CLI's exit codes to a structured result dict.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROTON_BIN = Path.home() / ".hermes" / "bin" / "proton"

# Full argv of the last subprocess invocation (for belt-and-braces checks).
LAST_ARGV: list[str] | None = None

EXIT_MEANINGS = {
    0: "ok",
    1: "bad-argument",
    2: "auth-failed",
    3: "not-found",
    4: "ambiguous",
    5: "network-or-server-error",
    130: "cancelled",
}


def _result(
    success: bool,
    exit_code: int | None,
    meaning: str | None = None,
    data=None,
    raw: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "success": success,
        "exit_code": exit_code,
        "meaning": meaning,
        "auth_required": exit_code == 2,
        "data": data,
        "raw": raw,
        "error": error,
    }


def run(
    args: list[str],
    stdin_text: str | None = None,
    timeout: int = 60,
    dry_run: bool = False,
) -> dict:
    """Run proton-cli with args, return a structured result dict."""
    if not PROTON_BIN.exists():
        return _result(
            False,
            None,
            error=f"proton binary not found at {PROTON_BIN}; install proton-cli there.",
        )

    argv = [str(PROTON_BIN), *args, "--output", "json", "--no-input"]
    if dry_run:
        argv.append("--dry-run")

    global LAST_ARGV
    LAST_ARGV = list(argv)

    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _result(
            False,
            None,
            error=(
                f"proton-cli timed out after {exc.timeout}s "
                f"(args: {' '.join(args)}); try a smaller --limit or higher timeout."
            ),
        )
    except FileNotFoundError as exc:
        return _result(False, None, error=f"proton binary not found at {PROTON_BIN}: {exc}")

    meaning = EXIT_MEANINGS.get(proc.returncode)

    def _s(v) -> str:
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        return v or ""

    stdout = _s(proc.stdout)

    if meaning is None:
        return _result(
            False,
            proc.returncode,
            data=None,
            raw=stdout + _s(proc.stderr),
            error=f"Unexpected exit code {proc.returncode} from proton-cli.",
        )

    if meaning != "ok":
        # Non-zero exit: return raw combined output for diagnostics.
        raw = stdout + _s(proc.stderr)
        data = None
        try:
            data = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            pass
        return _result(False, proc.returncode, meaning=meaning, data=data, raw=raw)

    # Success path: parse JSON, fall back to raw text.
    try:
        data = json.loads(stdout) if stdout.strip() else None
        raw = None if data is not None else stdout
    except json.JSONDecodeError:
        data, raw = None, stdout
    return _result(True, 0, meaning=meaning, data=data, raw=raw)
