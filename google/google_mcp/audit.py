"""Invocation audit logging for the MCP servers (shared core).

Every tool call is logged (tool, args with secret values redacted, duration,
outcome) and its full output captured to a rotating JSONL file under
~/.hermes/mcp-logs/<server>/. Both files rotate at 5 MB, 3 backups kept.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import logging.handlers
import os
import re
import time
from typing import Any, Callable

_LOGS_DIR = os.path.expanduser("~/.hermes/mcp-logs")
_MAX_BYTES = 5 * 1024 * 1024

# Keys matching this get their value redacted; values matching _SECRET_VALUE too.
_SECRET_KEYS = re.compile(
    r"token|secret|password|authorization|credential", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(?:Bearer\s+)\S+|(?:ntn_)[A-Za-z0-9-]+|(?:gh[pousr]_)[A-Za-z0-9_]+|"
    r"(?:sk-)[A-Za-z0-9-]+|(?:ya29\.)[A-Za-z0-9_-]+|(?:EAA)[A-Za-z0-9-]+|"
    r"(?:1//)[A-Za-z0-9_-]+")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_VALUE.sub("***", value)
    if isinstance(value, dict):
        return {k: ("***" if _SECRET_KEYS.search(k or "") else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _server_dir(server: str) -> str:
    d = os.path.join(_LOGS_DIR, server)
    os.makedirs(d, exist_ok=True)
    return d


def _logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"mcp.{name}")
    log_path = os.path.join(_server_dir(name), "server.log")
    if not any(getattr(h, "baseFilename", None) == log_path for h in logger.handlers):
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def _output_logger(server: str, tool: str) -> logging.Logger:
    """Rotating per-tool full-output capture (JSONL); path keyed, no drift."""
    path = os.path.join(_server_dir(server), f"{tool}.json")
    logger = logging.getLogger(f"mcp.audit.{server}.{tool}")
    if not any(getattr(h, "baseFilename", None) == path for h in logger.handlers):
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=3)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_call(server: str, tool: str):
    """Decorator: logs invocation (redacted args), duration, outcome, full output."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            log = _logger(server)
            try:
                bound = dict(inspect.signature(fn).bind_partial(*a, **kw).arguments)
                args_json = json.dumps(redact(bound), default=str)
            except Exception as exc:  # noqa: BLE001 — audit must not break the call
                bound, args_json = {}, f"<audit-arg-error: {exc}>"
            t0 = time.monotonic()
            log.info("CALL %s args=%s", tool, args_json)
            try:
                result = fn(*a, **kw)
            except Exception as exc:
                log.error("FAIL %s after %.2fs: %s", tool, time.monotonic() - t0, exc)
                raise
            dur = time.monotonic() - t0
            out = result if isinstance(result, str) else json.dumps(
                redact(result), default=str)
            writer = _output_logger(server, tool)
            writer.info(json.dumps({"ts": time.time(), "args": redact(bound),
                                    "output": redact(out)}, default=str))
            log.info("OK %s in %.2fs (%d chars)", tool, dur, len(out))
            return result
        return wrapper
    return deco
