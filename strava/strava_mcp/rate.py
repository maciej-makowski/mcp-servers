"""Rate budget tracker for the Strava v3 API (design section 3.4).

Every client response's X-ReadRateLimit-Usage and X-RateLimit-Usage headers
feed this tracker. All v1 tools are reads, so the read bucket
(X-ReadRateLimit-*) is the binding one; the overall numbers are tracked as
context. Limits default to the single-player read values (100 per 15
minutes, 1,000 per day, W2) and are re-armed from X-ReadRateLimit-Limit
whenever Strava sends one.

Reserves (D5): the guard refuses to issue a read when the 15-minute bucket
has 10 or fewer requests left (of 100) or the daily bucket has 100 or fewer
left (of 1,000). On 429 the wait to the next quarter-hour and to midnight
UTC is computed and returned to the caller instead of an internal retry, so
the agent can decide when to continue.

State is in-process and single-loop: the MCP server runs one event loop and
dispatches blocking calls through asyncio.to_thread, so no lock is taken
(the same single-loop assumption proton_mcp.base documents for its argv
check). strava_rate_status reads this state without making an API call.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

# D5 reserves: stop reads when a bucket falls to these remainders.
RESERVE_SHORT = 10
RESERVE_DAILY = 100

# Single-player read limits (W2). Re-armed from X-ReadRateLimit-Limit.
DEFAULT_LIMIT_SHORT = 100
DEFAULT_LIMIT_DAILY = 1000


class RateGuardError(RuntimeError):
    """A bucket is at or below its reserve (or Strava returned 429).

    Carries the wait-to-reset for both buckets so the agent can decide when
    to continue (section 3.4); the tool layer turns this into a structured
    rate_limited result instead of a raised error.
    """

    def __init__(self, message: str, wait: dict[str, Any]) -> None:
        super().__init__(message)
        self.wait = wait


def _parse_pair(value: str | None) -> tuple[int, int] | None:
    """Parse a two-integer header value ('27,180': 15-minute, daily)."""
    if not value:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def next_quarter_hour(now: dt.datetime | None = None) -> dt.datetime:
    """Next 15-minute bucket reset (natural quarter hours, W2)."""
    now = now or _utcnow()
    nxt = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=15 - now.minute % 15)
    return nxt


def next_daily_reset(now: dt.datetime | None = None) -> dt.datetime:
    """Next midnight UTC (W2)."""
    now = now or _utcnow()
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt


class RateState:
    """Last-seen 15-minute and daily budgets (section 3.4)."""

    def __init__(self) -> None:
        self.short = {"used": 0, "limit": DEFAULT_LIMIT_SHORT}
        self.daily = {"used": 0, "limit": DEFAULT_LIMIT_DAILY}
        self.overall: dict[str, int] | None = None
        self.last_seen: str | None = None

    # -- header ingestion ---------------------------------------------------

    def update_from_headers(self, headers: Any) -> None:
        """Feed the tracker from one response's rate headers (W2 format)."""
        pair = _parse_pair(headers.get("X-ReadRateLimit-Usage"))
        if pair:
            self.short["used"], self.daily["used"] = pair
        limits = _parse_pair(headers.get("X-ReadRateLimit-Limit"))
        if limits and all(v > 0 for v in limits):
            self.short["limit"], self.daily["limit"] = limits
        overall = _parse_pair(headers.get("X-RateLimit-Usage"))
        if overall:
            self.overall = {"short": overall[0], "daily": overall[1]}
        self.last_seen = _utcnow().isoformat()

    # -- guard ----------------------------------------------------------------

    def check(self, now: dt.datetime | None = None) -> None:
        """Refuse before a call when a bucket is at or below its reserve (D5)."""
        now = now or _utcnow()
        remaining_short = self.short["limit"] - self.short["used"]
        remaining_daily = self.daily["limit"] - self.daily["used"]
        if remaining_short <= RESERVE_SHORT or remaining_daily <= RESERVE_DAILY:
            wait = self.wait_after_429(now=now)
            raise RateGuardError(
                "strava rate budget at reserve: refusing reads until the next "
                f"reset (15-minute remaining {remaining_short}, daily remaining "
                f"{remaining_daily}; quarter-hour reset "
                f"{wait['quarter_hour_reset']}, daily reset {wait['daily_reset']})",
                wait=wait,
            )

    def wait_after_429(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """Wait to the next quarter-hour and to midnight UTC (section 3.4)."""
        now = now or _utcnow()
        qh, daily = next_quarter_hour(now), next_daily_reset(now)
        return {
            "quarter_hour_seconds": round((qh - now).total_seconds()),
            "daily_seconds": round((daily - now).total_seconds()),
            "quarter_hour_reset": qh.isoformat(),
            "daily_reset": daily.isoformat(),
        }

    # -- reporting ------------------------------------------------------------

    def status(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """Report the tracked budgets without an API call (section 3.4)."""
        now = now or _utcnow()
        out: dict[str, Any] = {}
        for name, bucket, reserve in (
            ("15_minute", self.short, RESERVE_SHORT),
            ("daily", self.daily, RESERVE_DAILY),
        ):
            out[name] = {
                "used": bucket["used"],
                "limit": bucket["limit"],
                "remaining": bucket["limit"] - bucket["used"],
                "reserve": reserve,
            }
        out["15_minute"]["next_reset"] = next_quarter_hour(now).isoformat()
        out["daily"]["next_reset"] = next_daily_reset(now).isoformat()
        out["overall_usage"] = self.overall
        out["last_seen"] = self.last_seen
        return out


# Single shared tracker: one process serves one athlete (single-player, W2).
STATE = RateState()
