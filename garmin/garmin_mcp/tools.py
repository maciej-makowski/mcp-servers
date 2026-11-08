"""The seven read-only Garmin tools (live Personal API calls).

Every result is JSON-serialisable. Ranged reads go through the TTL cache and
the 429/5xx backoff; per-family failures are recorded for garmin_api_status
and reported per family instead of aborting the whole query.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from typing import Any, Callable

from garmin_mcp import client, ranges
from garmin_mcp.cache import CACHE_TTL_SECONDS, TTLCache, call_with_backoff

CACHE = TTLCache(ttl_seconds=CACHE_TTL_SECONDS)

FAMILIES: tuple[str, ...] = tuple(ranges.RANGED_FAMILY_CAPS) + ranges.PERDAY_FAMILIES

_DATE_KEYS = ("calendarDate", "calendar_date", "date", "startDate", "start_date")


def _activity_id(value: Any) -> str:
    aid = str(value).strip()
    if not aid.isascii() or not aid.isdecimal() or int(aid) <= 0:
        raise ValueError("activity_id must be a positive integer")
    return aid


def _parse_families(families: str) -> list[str]:
    if not families or families.strip().lower() == "all":
        return list(FAMILIES)
    names = [f.strip() for f in families.split(",") if f.strip()]
    unknown = [f for f in names if f not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown families {unknown}; valid families: {sorted(FAMILIES)}")
    return list(dict.fromkeys(names))


def _has_data(data: Any) -> bool:
    return data is not None and data != [] and data != {}


def _cached_call(key: tuple, family: str, fetch: Callable[[], Any]) -> Any:
    """Cache-gated + backoff-gated call with per-family error bookkeeping."""
    cached = CACHE.get(key)
    if cached is not None:
        return cached
    try:
        data = call_with_backoff(fetch)
    except Exception as exc:  # noqa: BLE001 — record then re-raise
        client.record_family_error(family, exc)
        raise
    client.record_family_ok(family)
    CACHE.set(key, data)
    return data


def recent_activities(limit: int = 20, since: str = "") -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    since_iso = ranges.parse_date(since).isoformat() if since.strip() else ""
    data = _cached_call(
        ("recent_activities", limit, since_iso),
        "activities",
        lambda: client.search_activities(limit=limit, since=since_iso),
    )
    return {"activities": data or []}


def activity(activity_id: str) -> dict[str, Any]:
    aid = _activity_id(activity_id)
    data = _cached_call(
        ("activity", aid), "activities", lambda: client.get_activity(aid)
    )
    return {"activity": data}


def activity_file(activity_id: str) -> dict[str, Any]:
    """Summarise the activity ZIP (size, sha256, type); never returns bytes."""
    aid = _activity_id(activity_id)
    blob = _cached_call(
        ("activity_file", aid), "activities", lambda: client.download_activity(aid)
    )
    if isinstance(blob, str):
        blob = blob.encode()
    return {
        "activity_id": aid,
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "content_type": "application/zip",
        "note": (
            "API-delivered activity ZIP (FIT inside; field-stripping reported, "
            "unconfirmed for personal keys). Summary only; never auto-attached."
        ),
    }


def daily(
    start_date: str,
    end_date: str,
    families: str = "all",
    body_battery_chunks: int = 0,
) -> dict[str, Any]:
    """Fan out per family to the ranged/per-day endpoints over one window."""
    start = ranges.parse_date(start_date)
    end = ranges.parse_date(end_date)
    if end < start:
        raise ValueError("end_date is before start_date")
    requested = _parse_families(families)
    chunks_param = int(body_battery_chunks) if body_battery_chunks else None
    if chunks_param is not None and chunks_param < 1:
        raise ValueError("body_battery_chunks must be at least 1 when set")

    result: dict[str, Any] = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "families": {},
        "errors": {},
    }
    for family in requested:
        try:
            if family in ranges.RANGED_FAMILY_CAPS:
                result["families"][family] = _fetch_family_ranged(
                    family, start, end, chunks_param
                )
            else:
                result["families"][family] = _fetch_family_perday(family, start, end)
        except Exception as exc:  # noqa: BLE001 — one family must not sink the rest
            result["families"][family] = {"present": False}
            result["errors"][family] = str(exc)
    return result


def _fetch_family_ranged(
    family: str, start: dt.date, end: dt.date, chunks_param: int | None
) -> dict[str, Any]:
    cap = ranges.RANGED_FAMILY_CAPS[family]
    pieces = []
    for chunk_start, chunk_end in ranges.split_range(start, end, cap):
        key = (
            "daily",
            family,
            chunk_start.isoformat(),
            chunk_end.isoformat(),
            chunks_param if family == "body_battery" else None,
        )
        data = _cached_call(
            key,
            family,
            lambda f=family, s=chunk_start, e=chunk_end: client.fetch_ranged(
                f, s, e, chunks=chunks_param
            ),
        )
        pieces.append({"start": chunk_start.isoformat(), "end": chunk_end.isoformat(), "data": data})
    return {"chunks": pieces, "present": any(_has_data(p["data"]) for p in pieces)}


def _fetch_family_perday(family: str, start: dt.date, end: dt.date) -> dict[str, Any]:
    days = (end - start).days + 1
    if days > ranges.GARMIN_MAX_PERDAY_FANOUT_DAYS:
        raise ValueError(
            f"family {family!r} is a per-day endpoint; at most "
            f"{ranges.GARMIN_MAX_PERDAY_FANOUT_DAYS} days per request (requested {days})"
        )
    pieces = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        data = _cached_call(
            ("daily", family, day.isoformat()),
            family,
            lambda f=family, d=day: client.fetch_perday(f, d),
        )
        pieces.append({"date": day.isoformat(), "data": data})
    return {"days": pieces, "present": any(_has_data(p["data"]) for p in pieces)}


def trend(family: str, weeks: int = 8) -> dict[str, Any]:
    """Weekly aggregates for one ranged family over the trailing N weeks."""
    weeks = max(1, min(int(weeks), 52))
    if family not in ranges.RANGED_FAMILY_CAPS:
        raise ValueError(
            f"family {family!r} has no ranged endpoint; valid families: "
            f"{sorted(ranges.RANGED_FAMILY_CAPS)}"
        )
    end = dt.date.today()
    start = end - dt.timedelta(days=weeks * 7 - 1)
    records: list[dict[str, Any]] = []
    for chunk_start, chunk_end in ranges.split_range(
        start, end, ranges.RANGED_FAMILY_CAPS[family]
    ):
        data = _cached_call(
            ("daily", family, chunk_start.isoformat(), chunk_end.isoformat(), None),
            family,
            lambda s=chunk_start, e=chunk_end: client.fetch_ranged(family, s, e),
        )
        records.extend(_extract_records(data))
    return {
        "family": family,
        "weeks": weeks,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "buckets": _aggregate_weekly(records, weeks),
    }


def _extract_records(data: Any) -> list[dict[str, Any]]:
    """Pull list-of-dicts records out of the endpoint's varying response shapes."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [r for r in value if isinstance(r, dict)]
    return []


def _record_date(record: dict[str, Any]) -> dt.date | None:
    for key in _DATE_KEYS:
        value = record.get(key)
        if isinstance(value, str):
            try:
                return ranges.parse_date(value)
            except ValueError:
                continue
    return None


def _aggregate_weekly(
    records: list[dict[str, Any]], max_weeks: int
) -> list[dict[str, Any]]:
    """Group dated records into Monday-anchored weeks; mean of flat numeric fields."""
    buckets: dict[dt.date, dict[str, Any]] = {}
    for record in records:
        day = _record_date(record)
        if day is None:
            continue
        week_start = day - dt.timedelta(days=day.weekday())
        bucket = buckets.setdefault(
            week_start, {"week_start": week_start.isoformat(), "days": set(), "sums": {}, "counts": {}}
        )
        bucket["days"].add(day.isoformat())
        for key, value in record.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            bucket["sums"][key] = bucket["sums"].get(key, 0.0) + value
            bucket["counts"][key] = bucket["counts"].get(key, 0) + 1
    out = []
    for week_start in sorted(buckets)[-max_weeks:]:
        bucket = buckets[week_start]
        means = {k: bucket["sums"][k] / bucket["counts"][k] for k in bucket["sums"]}
        out.append({
            "week_start": bucket["week_start"],
            "days": len(bucket["days"]),
            "mean": {k: round(v, 3) for k, v in means.items()},
        })
    return out


def workout_usage(activity_id: str) -> dict[str, Any]:
    aid = _activity_id(activity_id)
    data = _cached_call(
        ("workouts", aid), "activities", lambda: client.get_activity_workouts(aid)
    )
    return {"workouts": data or []}


def api_status() -> dict[str, Any]:
    """Bearer-token validity/expiry, per-family last errors, cache stats."""
    key = os.environ.get(client.API_KEY_ENV_VAR, "").strip()
    status: dict[str, Any] = {
        "mode": "live-read-v1",
        "api_key_configured": bool(key),
        "token": None,
        "families": client.family_errors(),
        "cache": CACHE.stats(),
    }
    if not key:
        status["token"] = {"valid": False, "reason": f"{client.API_KEY_ENV_VAR} is not set"}
        return status
    try:
        token = client.obtain_bearer_token()
    except Exception as exc:  # noqa: BLE001 — status must diagnose, not raise
        status["token"] = {"valid": False, "reason": str(exc)}
    else:
        now = time.time()
        status["token"] = {
            "valid": token.expires_at > now,
            "expires_at": dt.datetime.fromtimestamp(token.expires_at, tz=dt.timezone.utc).isoformat(),
            "seconds_remaining": round(token.expires_at - now),
        }
    return status
