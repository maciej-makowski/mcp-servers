"""Idle-token refresh loop for strava-mcp (design section 3.2).

Strava returns the EXISTING access token when a refresh is requested while
the current token still has more than one hour of life (W1), but the call
still rotates (and invalidates) the previous refresh token. Refreshing
early is therefore a no-op for the access token AND burns a rotation, so
the loop refreshes only when the stored expires_at is under one hour away.

The loop runs alongside the server (Starlette lifespan in HTTP mode, a task
in stdio mode); failures are logged and retried at the next tick. They never
crash the server.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import oauth

log = logging.getLogger("mcp.strava.idle_refresh")

# W1: refresh only when under one hour of life remains (an earlier refresh
# returns the same access token while still rotating the refresh token).
REFRESH_MARGIN_S = 3600.0
# Check cadence: tokens live six hours, so a quarter-hour cadence keeps at
# least three hours of margin before expiry even after a failed tick.
INTERVAL_S = 15 * 60.0


def refresh_due(store: dict, now: float | None = None) -> bool:
    """True when the stored expires_at is under one hour away (section 3.2)."""
    return oauth.seconds_left(store, now=now) < REFRESH_MARGIN_S


def start_idle_refresh(token_path: str = oauth.TOKEN_PATH,
                       interval_s: float = INTERVAL_S) -> asyncio.Task:
    """Start the background refresh task; the caller cancels it on shutdown."""

    async def _loop() -> None:
        while True:
            try:
                store = oauth.load_store(token_path)
                if refresh_due(store):
                    log.info("access token under %.0fs of life; refreshing",
                             REFRESH_MARGIN_S)
                    oauth.refresh_access_token(token_path)
                    log.info("idle refresh OK")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must survive failures  # noqa: BLE001
                log.warning("idle refresh skipped (retried next tick): %s", exc)
            await asyncio.sleep(interval_s)

    return asyncio.create_task(_loop())
