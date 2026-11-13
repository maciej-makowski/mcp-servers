"""Family registry for the Garmin local data archive.

Enumerates every Personal API family the archive syncs (garmin-local-archive
design §2.1, §3.2): 11 ranged families (inclusive date windows, stored in
canonical chunks on the anchor grid), 8 per-day families (single-date raw
responses) and 3 point reads (no date). ``mcp_exposed`` marks the families
the seven deployed tools serve today; the rest are archive-completeness
families — the sync stores them, no tool reads them.

Caps are the secondhand constants deployed in ranges.py plus conservative
30-day defaults for the families the tools never expose (no live evidence
yet; pending phase-5 verification like every other cap).
"""
from __future__ import annotations

import datetime as dt

from garmin_mcp import ranges

PROBE_WINDOW_DAYS = 28  # every ranged cap is >= 28, so one probe window fits all


class RangedFamily:
    __slots__ = ("name", "cap_days", "mcp_exposed")

    def __init__(self, name: str, cap_days: int, mcp_exposed: bool) -> None:
        self.name = name
        self.cap_days = cap_days
        self.mcp_exposed = mcp_exposed


# The 11 ranged families of §2.1. Order = probe/sync order (cheapest, most
# reliably populated first — the sparse probe walks this list).
RANGED_FAMILIES: tuple[RangedFamily, ...] = (
    RangedFamily("steps", ranges.GARMIN_CAP_STEPS_DAYS, True),
    RangedFamily("daily", ranges.GARMIN_CAP_DAILY_DAYS, True),
    RangedFamily("stress", ranges.GARMIN_CAP_STRESS_DAYS, True),
    RangedFamily("hrv", ranges.GARMIN_CAP_HRV_DAYS, True),
    RangedFamily("sleep", ranges.GARMIN_CAP_SLEEP_DAYS, True),
    RangedFamily("body_battery", ranges.GARMIN_CAP_BODY_BATTERY_DAYS, True),
    RangedFamily("hydration", ranges.GARMIN_CAP_HYDRATION_DAYS, True),
    RangedFamily("weight", ranges.GARMIN_CAP_WEIGHT_DAYS, True),
    RangedFamily("weight_goals", 30, False),
    RangedFamily("wellness_activities", 30, False),
    RangedFamily("weight_all", 30, False),
)

RANGED_BY_NAME: dict[str, RangedFamily] = {f.name: f for f in RANGED_FAMILIES}

# The 8 per-day families of §2.1 (single calendar date each).
PERDAY_FAMILIES_ARCHIVE: tuple[str, ...] = (
    "spo2",
    "fitness_age",
    "heart_rate",
    "sleep_detail",
    "hrv_detail",
    "nutrition",
    "menstrual_summary",
    "menstrual_day_view",
)
PERDAY_MCP_EXPOSED = frozenset({"spo2", "fitness_age", "heart_rate"})

# The 3 point reads of §2.1 (no date parameter).
POINT_NAMES: tuple[str, ...] = ("profile_settings", "hr_zones", "legacy_daily")

# Ranged families the MCP tools expose (names as the tools use them; these
# MUST match ranges.RANGED_FAMILY_CAPS keys).
MCP_RANGED_FAMILIES = frozenset(ranges.RANGED_FAMILY_CAPS)


def cap_for(family: str) -> int:
    """Canonical-grid cap for a ranged archive family."""
    return RANGED_BY_NAME[family].cap_days


def canonical_chunks(
    anchor: dt.date, cap_days: int, start: dt.date, end: dt.date
) -> list[tuple[dt.date, dt.date]]:
    """Canonical grid chunks (§3.2) overlapping inclusive [start, end].

    Canonical chunk k of a family = [anchor + k*cap, anchor + (k+1)*cap - 1].
    Only chunks with start >= anchor exist (the grid extends forward from the
    global anchor A; pre-anchor dates have no canonical chunk and are served
    live, never stored).
    """
    if end < start:
        return []
    k0 = max(0, (start - anchor).days // cap_days)
    k1 = (end - anchor).days // cap_days
    if (end - anchor).days < 0:
        return []
    out: list[tuple[dt.date, dt.date]] = []
    for k in range(k0, k1 + 1):
        chunk_start = anchor + dt.timedelta(days=k * cap_days)
        chunk_end = chunk_start + dt.timedelta(days=cap_days - 1)
        out.append((chunk_start, chunk_end))
    return out


def chunk_for_day(anchor: dt.date, cap_days: int, day: dt.date) -> tuple[dt.date, dt.date] | None:
    """The single canonical chunk containing ``day`` (None when pre-anchor)."""
    if day < anchor:
        return None
    k = (day - anchor).days // cap_days
    chunk_start = anchor + dt.timedelta(days=k * cap_days)
    return (chunk_start, chunk_start + dt.timedelta(days=cap_days - 1))
