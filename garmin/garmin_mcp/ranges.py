"""Date-range splitting for the Garmin Personal API ranged endpoints.

The per-family server-side range caps are secondhand values relayed from a
third-party implementation's README (Mason-Levyy/garmin-personal-mcp
documents 28 days for steps and 31 for sleep). Verify every cap against the
real API during the phase 6 trial and adjust the constants here.
"""
from __future__ import annotations

import datetime as dt

# Secondhand values from a third-party implementation's README; verify in trial.
GARMIN_CAP_STEPS_DAYS = 28
GARMIN_CAP_SLEEP_DAYS = 31
# No documented cap for the families below; conservative defaults aligned with
# the documented 28/31 values. Verify in the phase 6 trial.
GARMIN_CAP_STRESS_DAYS = 30
GARMIN_CAP_HYDRATION_DAYS = 30
GARMIN_CAP_WEIGHT_DAYS = 30
GARMIN_CAP_BODY_BATTERY_DAYS = 30
GARMIN_CAP_DAILY_DAYS = 30
GARMIN_CAP_HRV_DAYS = 30
# Per-day families have no range cap; this is a request-volume guard only.
GARMIN_MAX_PERDAY_FANOUT_DAYS = 92

RANGED_FAMILY_CAPS: dict[str, int] = {
    "stress": GARMIN_CAP_STRESS_DAYS,
    "steps": GARMIN_CAP_STEPS_DAYS,
    "hydration": GARMIN_CAP_HYDRATION_DAYS,
    "weight": GARMIN_CAP_WEIGHT_DAYS,
    "sleep": GARMIN_CAP_SLEEP_DAYS,
    "body_battery": GARMIN_CAP_BODY_BATTERY_DAYS,
    "daily": GARMIN_CAP_DAILY_DAYS,
    "hrv": GARMIN_CAP_HRV_DAYS,
}
PERDAY_FAMILIES: tuple[str, ...] = ("spo2", "fitness_age", "heart_rate")


def parse_date(value: str | dt.date) -> dt.date:
    """Parse an ISO YYYY-MM-DD string (date objects pass through)."""
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


def split_range(
    start: dt.date, end: dt.date, cap_days: int
) -> list[tuple[dt.date, dt.date]]:
    """Split an inclusive [start, end] window into chunks of at most cap_days.

    Returns (chunk_start, chunk_end) pairs aligned from ``start`` and
    contiguous; the last chunk carries the remainder.
    """
    if cap_days < 1:
        raise ValueError("cap_days must be at least 1")
    if end < start:
        raise ValueError("end date is before start date")
    chunks: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while True:
        chunk_end = min(cursor + dt.timedelta(days=cap_days - 1), end)
        chunks.append((cursor, chunk_end))
        if chunk_end == end:
            return chunks
        cursor = chunk_end + dt.timedelta(days=1)
