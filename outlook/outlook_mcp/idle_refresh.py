"""Idle-token refresh loop for the outlook MCP server.

Mirrors google_mcp/idle_refresh.py: runs in-process alongside the server
(FastMCP lifespan) — at startup and every ``interval_s`` (default 6h), if the
token store is older than ``max_age_s`` (default 7 days) it runs ONE refresh
through the server's existing atomic+flocked refresh function
(graph.refresh_credentials). Failures are logged and retried at the next
interval — they never crash the server.

Age is measured from tokens.json's mtime: every successful refresh rewrites
the file atomically (os.replace in graph._save_tokens), which updates mtime,
so mtime doubles as a "last successful refresh" stamp without adding a
separate state file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("mcp.idle_refresh")


def _store_age_s(token_path: str) -> float:
    """Seconds since the token store was last written; missing -> infinite."""
    try:
        return time.time() - os.path.getmtime(token_path)
    except OSError:
        return float("inf")


def start_idle_refresh(refresh_fn, token_path: str,
                       max_age_s: float = 7 * 86400,
                       interval_s: float = 6 * 3600) -> asyncio.Task:
    """Start the background refresh task; returns it so lifespan can cancel."""

    async def _loop() -> None:
        while True:
            try:
                age = _store_age_s(token_path)
                if age > max_age_s:
                    log.info("token store %.1fd old (> %.1fd), refreshing",
                             age / 86400, max_age_s / 86400)
                    await asyncio.to_thread(refresh_fn)
                    log.info("idle refresh OK")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — loop must survive failures
                log.error("idle refresh failed (retried next interval): %s", exc)
            await asyncio.sleep(interval_s)

    return asyncio.create_task(_loop())
