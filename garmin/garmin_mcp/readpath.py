"""Store read-through path for the seven garmin tools (design §3.5, §5).

Modes (§5.1, env knob GARMIN_STORE_MODE with GARMIN_STORE_DIR):

- ``off`` (default, also when the dir is set but the mode is unset): every
  read is a live API read exactly as today — this PR alone changes nothing
  until the env flips on the box.
- ``live_only``: the store stays mounted for the sync to write while reads
  ignore it (used during a rebuild, §5.4).
- ``read_through``: reads serve from the store when it can answer completely
  and freshly, with ZERO upstream calls; any missing or stale piece is
  fetched live and written through (§5.2). ``read_through`` is rejected at
  startup unless the init checks pass (coverage present, schema version
  match), so a broken store can never be adopted silently.

Every response is assembled to byte-faithfully match the live tool envelopes
(P4/P6): the read path NEVER changes a tool's response shape, and never
serves the store's stale bytes as history — a piece whose on-demand re-fetch
fails (open circuit, 401, upstream error) lands in the response's errors map
exactly as a failed live family call does today. A store I/O error degrades
the affected read to the live path (§4.1) rather than failing the tool.

Staleness rule (§5.2): a non-today day D is stale when its
daily_records.fetched_at predates the end of D (local midnight) plus a 6 h
publish lag; today is governed by the 600 s TTL-cache window. A stale or
today-touching piece triggers the containing canonical chunk's live re-fetch
(ranged families) or the single-date re-fetch (per-day families), TTL-
collapsed, before it is served.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any

from garmin_mcp import families, ranges
from garmin_mcp.store import (STORE_SCHEMA_VERSION, Store, StoreError, has_data,
                              parse_ts, slice_data)

log = logging.getLogger("mcp.garmin.readpath")

STORE_DIR_ENV = "GARMIN_STORE_DIR"
STORE_MODE_ENV = "GARMIN_STORE_MODE"

TTL_SECONDS = 600                # the existing TTL-cache window (§3.5)
PUBLISH_LAG_SECONDS = 6 * 3600   # secondhand constant, pending phase-5 (§5.2)
STATUS_STALE_HOURS = 30          # off_host staleness alarm window (§5.3/§6.1)

MODE_OFF = "off"
MODE_LIVE_ONLY = "live_only"
MODE_READ_THROUGH = "read_through"
_VALID_MODES = (MODE_OFF, MODE_LIVE_ONLY, MODE_READ_THROUGH)

_LOCK = threading.Lock()
_STORE: Store | None = None
_MODE: str | None = None  # None = not configured yet (lazy init)
_DEGRADED: str | None = None


def init_from_env(environ: dict[str, str] | None = None) -> None:
    """Resolve (store, mode) from env once (server startup; tests call reset)."""
    global _STORE, _MODE, _DEGRADED
    env = environ if environ is not None else os.environ
    root = (env.get(STORE_DIR_ENV) or "").strip()
    mode = (env.get(STORE_MODE_ENV) or "").strip().lower()
    with _LOCK:
        _DEGRADED = None
        if not root or mode == MODE_OFF:
            _STORE = None
            _MODE = MODE_OFF
            return
        if mode not in (MODE_LIVE_ONLY, MODE_READ_THROUGH):
            if mode:
                log.warning("invalid %s=%r; treating as off", STORE_MODE_ENV, mode)
            _STORE = None
            _MODE = MODE_OFF
            return
        store = Store(root)
        try:
            store.ensure_dirs()
        except StoreError as exc:
            log.error("store init failed (%s); sync/reads disabled", exc)
            _STORE = None
            _MODE = MODE_OFF
            _DEGRADED = str(exc)
            return
        _STORE = store
        _MODE = mode
        if mode == MODE_READ_THROUGH:
            try:
                check_read_through_ready(store)
            except StoreError as exc:
                # §5.1: read_through rejected at startup unless init checks
                # pass — the store is mounted for the sync, reads stay live.
                log.error("read_through rejected (%s); serving live_only", exc)
                _MODE = MODE_LIVE_ONLY
                _DEGRADED = f"init checks failed: {exc}"


def reset_for_tests(store: Store | None = None, mode: str | None = None) -> None:
    global _STORE, _MODE, _DEGRADED
    with _LOCK:
        _STORE = store
        _MODE = mode
        _DEGRADED = None


def store() -> Store | None:
    return _STORE


def _store() -> Store | None:
    """Internal accessor used by every read function."""
    return _STORE


def mode() -> str:
    return _MODE or MODE_OFF


def degraded() -> str | None:
    return _DEGRADED


def read_through() -> bool:
    """True when reads may serve from the store (§5.1)."""
    return _MODE == MODE_READ_THROUGH and _STORE is not None


def check_read_through_ready(store: Store) -> None:
    """Startup init checks (§5.1): schema version match + coverage present."""
    version = store.get_meta("schema_version")
    if version != STORE_SCHEMA_VERSION:
        raise StoreError(
            f"schema version mismatch (index reports {version!r}, want {STORE_SCHEMA_VERSION!r})")
    rows = store.coverage_rows()
    if not rows:
        raise StoreError("no coverage rows yet (store has no ranged chunks)")
    anchor = store.get_meta("anchor")
    if not anchor:
        raise StoreError("anchor not set (backfill has not initialised the grid)")


def _tools():
    """Deferred import: the read path reuses the live tool call machinery."""
    from garmin_mcp import tools

    return tools


def _cached_call(key: tuple, family: str, fetch) -> Any:
    """TTL-cache-gated + backoff-gated live call with family bookkeeping."""
    return _tools()._cached_call(key, family, fetch)


# --------------------------------------------------------------------- reads


def recent_activities(limit: int, since_iso: str) -> dict[str, Any]:
    """§3.5: index ORDER BY start_time_gmt DESC, filter >= since, LIMIT,
    gated by the activity freshness gate."""
    s = _store()
    assert s is not None
    _activity_gate()
    ids = s.search_record_ids(since_iso or None, limit)
    records: list[dict[str, Any]] = []
    for activity_id in ids:
        try:
            record = s.read_json(s.activity_dir(activity_id) / "search-record.json")
        except StoreError:
            continue  # row without its file: skip (index is derived, rebuildable)
        if isinstance(record, dict):
            records.append(record)
    return {"activities": records}


def _activity_gate() -> None:
    """Arrival-safe gate (§3.5): when the newest stored search_fetched_at is
    older than the TTL window, run the sweep's arrival-safe search live
    starting at page 1 with a 2-page cap (TTL-collapsed, 1-2 upstream calls),
    write through (new ids get detail/workouts/ZIP per the sweep rules; known
    ids are no-ops), then serve from the refreshed index. A failed gate
    search leaves the index unrefreshed and the list lands in the errors
    path, as the live tool's own failure would."""
    from garmin_mcp import client, sync as sync_mod

    s = _store()
    assert s is not None
    newest = parse_ts(s.newest_search_fetched_at())
    now = dt.datetime.now(dt.timezone.utc)
    if newest is not None and (now - newest).total_seconds() <= TTL_SECONDS:
        return  # fresh index: serve from the store with zero upstream calls

    def gate_op() -> bool:
        new_ids: list[int] = []
        offset = 0
        for page in range(2):  # 2-page cap (§3.5)
            records = client.search_activities_page(offset, 100)
            if not records:
                break
            fresh = 0
            for record in records:
                if not isinstance(record, dict):
                    continue
                aid = record.get("activityId")
                if not isinstance(aid, int) or s.has_activity(aid):
                    continue
                s.write_json_atomic(s.activity_dir(aid) / "search-record.json", record)
                s.upsert_activity_search(record, store_mod_utc())
                new_ids.append(aid)
                fresh += 1
            offset += len(records)
            if len(records) < 100:
                break
        engine = sync_mod.engine()
        for activity_id in new_ids:
            if engine is not None and engine.run_lock.locked():
                break  # the sweep is already enriching; do not double up
            try:
                detail = _cached_call(
                    ("store_gate_detail", activity_id), "activities",
                    lambda a=activity_id: client.get_activity(str(a)))
            except Exception:  # noqa: BLE001 — enrichment is best-effort
                detail = None
            if detail is not None:
                s.write_json_atomic(s.activity_dir(activity_id) / "detail.json", detail)
                s.set_activity_marker(activity_id, "detail_fetched_at", store_mod_utc())
        return True

    _cached_call(("activity_gate",), "activities", gate_op)


def store_mod_utc() -> str:
    from garmin_mcp.store import utc_now_iso

    return utc_now_iso()


def activity(activity_id: str) -> dict[str, Any]:
    """§3.5: activities/<id>/detail.json, on miss live with write-through."""
    s = _store()
    assert s is not None
    detail = None
    try:
        detail = s.read_json(s.activity_dir(activity_id) / "detail.json")
    except StoreError:
        detail = None
    if detail is not None:
        return {"activity": detail}
    data = _cached_call(("activity", activity_id), "activities",
                        lambda: _client().get_activity(activity_id))
    _write_through_detail(activity_id, data)
    return {"activity": data}


def _write_through_detail(activity_id: str, data: Any) -> None:
    try:
        s = _store()
        assert s is not None
        s.write_json_atomic(s.activity_dir(activity_id) / "detail.json", data)
        if isinstance(data, dict) and isinstance(data.get("activityId"), int):
            s.set_activity_marker(int(data["activityId"]), "detail_fetched_at",
                                  store_mod_utc())
    except StoreError:
        log.warning("detail write-through failed for %s (read still served)", activity_id)


def activity_zip_bytes(activity_id: str) -> bytes | None:
    """Stored ZIP bytes, or None when the live path must fetch them (§3.5:
    size/sha256 computed from the stored file with zero API calls)."""
    s = _store()
    assert s is not None
    try:
        return s.read_bytes(s.activity_dir(activity_id) / "file.zip")
    except StoreError:
        return None


def write_through_zip(activity_id: str, blob: bytes) -> None:
    try:
        import hashlib as _hashlib

        s = _store()
        assert s is not None
        s.write_bytes_atomic(s.activity_dir(activity_id) / "file.zip", blob)
        aid = int(activity_id)
        s.set_activity_marker(aid, "zip_fetched_at", store_mod_utc(),
                              zip_sha256=_hashlib.sha256(blob).hexdigest(),
                              zip_size=len(blob))
    except (StoreError, ValueError):
        log.warning("zip write-through failed for %s (read still served)", activity_id)


def workout_usage(activity_id: str) -> dict[str, Any]:
    """§3.5: activities/<id>/workouts.json, on miss live with write-through."""
    s = _store()
    assert s is not None
    data = None
    try:
        data = s.read_json(s.activity_dir(activity_id) / "workouts.json")
    except StoreError:
        data = None
    if data is not None:
        return {"workouts": data}
    live = _cached_call(("workouts", activity_id), "activities",
                        lambda: _client().get_activity_workouts(activity_id))
    try:
        s.write_json_atomic(s.activity_dir(activity_id) / "workouts.json",
                            live if live is not None else [])
    except StoreError:
        log.warning("workouts write-through failed for %s", activity_id)
    return {"workouts": live or []}


def daily(start: dt.date, end: dt.date, requested: list[str],
          chunks_param: int | None) -> dict[str, Any]:
    """§3.5 garmin_daily read path: ranged families from canonical chunks
    (sliced to the request dates), per-day families from their perday files."""
    tools = _tools()
    result: dict[str, Any] = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "families": {},
        "errors": {},
    }
    for family in requested:
        try:
            if family in ranges.RANGED_FAMILY_CAPS:
                if family == "body_battery" and chunks_param is not None:
                    # A chunks-param request is a different upstream query; the
                    # canonical store holds only the plain (chunks=None) shape.
                    result["families"][family] = tools._fetch_family_ranged(
                        family, start, end, chunks_param)
                else:
                    result["families"][family] = _daily_ranged(family, start, end)
            else:
                result["families"][family] = _daily_perday(family, start, end)
        except StoreError:
            # §4.1: a store I/O error degrades the affected family to live.
            try:
                if family in ranges.RANGED_FAMILY_CAPS:
                    result["families"][family] = tools._fetch_family_ranged(
                        family, start, end, chunks_param)
                else:
                    result["families"][family] = tools._fetch_family_perday(
                        family, start, end)
            except Exception as exc:  # noqa: BLE001 — live-tool failure semantics
                result["families"][family] = {"present": False}
                result["errors"][family] = str(exc)
        except Exception as exc:  # noqa: BLE001 — one family must not sink the rest
            result["families"][family] = {"present": False}
            result["errors"][family] = str(exc)
    return result


def _daily_ranged(family: str, start: dt.date, end: dt.date) -> dict[str, Any]:
    s = _store()
    assert s is not None
    cap = ranges.RANGED_FAMILY_CAPS[family]
    anchor = _anchor(s)
    if anchor is None or start < anchor:
        # No canonical grid coverage for this window: serve live (§5.2).
        return _tools()._fetch_family_ranged(family, start, end, None)
    pieces = []
    for piece_start, piece_end in ranges.split_range(start, end, cap):
        data = _piece_data(s, family, cap, anchor, piece_start, piece_end)
        pieces.append({"start": piece_start.isoformat(),
                       "end": piece_end.isoformat(),
                       "data": data})
    return {"chunks": pieces, "present": any(has_data(p["data"]) for p in pieces)}


def _anchor(s: Store) -> dt.date | None:
    raw = s.get_meta("anchor")
    if not raw:
        from garmin_mcp import sync as sync_mod

        engine = sync_mod.engine()
        if engine is not None:
            raw = engine.state.get("anchor")
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _piece_data(s: Store, family: str, cap: int, anchor: dt.date,
                piece_start: dt.date, piece_end: dt.date) -> Any:
    """One envelope piece for [piece_start, piece_end], assembled from the
    canonical chunks covering it, honouring the freshness edge (§3.5)."""
    today = dt.datetime.now().astimezone().date()
    out_data: Any = None
    saw_chunk = False
    for chunk_start, chunk_end in families.canonical_chunks(anchor, cap,
                                                            piece_start, piece_end):
        saw_chunk = True
        raw = _chunk_raw(s, family, chunk_start, chunk_end)
        fetched_at = parse_ts(s.coverage_row(family, chunk_start, chunk_end))
        demand = _window_demands_refresh(family, s, max(piece_start, chunk_start),
                                         min(piece_end, chunk_end), today)
        if demand and (fetched_at is None
                       or _seconds_since(fetched_at) > TTL_SECONDS):
            # Freshness edge (§3.5): re-fetch the WHOLE canonical chunk
            # (TTL-collapsed), atomically replace it, re-derive its perday
            # files, update coverage and daily_records — §3.2's invariant is
            # untouched: every ranged write stays canonical.
            refetched = _cached_call(
                ("store_chunk", family, chunk_start.isoformat(), chunk_end.isoformat()),
                family,
                lambda cs=chunk_start, ce=chunk_end: _client().fetch_ranged_archive(
                    family, cs, ce))
            s.write_ranged_chunk(family, chunk_start, chunk_end, refetched)
            raw = refetched
            fetched_at = parse_ts(s.coverage_row(family, chunk_start, chunk_end))
        if raw is None:
            # Store miss: fetch the missing canonical chunk live (write-
            # through), then serve from the store (§3.5 on-miss cell).
            refetched = _cached_call(
                ("store_chunk", family, chunk_start.isoformat(), chunk_end.isoformat()),
                family,
                lambda cs=chunk_start, ce=chunk_end: _client().fetch_ranged_archive(
                    family, cs, ce))
            s.write_ranged_chunk(family, chunk_start, chunk_end, refetched)
            raw = refetched
        if out_data is None:
            out_data = raw
        else:
            merged = _concat(out_data, raw)
            out_data = merged if merged is not None else out_data
    if not saw_chunk:
        raise StoreError("no canonical chunk covers this window")
    if out_data is None:
        raise StoreError("no data assembled for this window")
    if (piece_start, piece_end) == families.chunk_for_day(anchor, cap, piece_start):
        return out_data  # piece == canonical chunk: the API's own bytes
    sliced = slice_data(out_data, piece_start, piece_end)
    if sliced is None:
        raise StoreError("response shape not date-sliceable for this window")
    return sliced


def _concat(left: Any, right: Any) -> Any | None:
    """Merge two chunk raws of the same shape for a piece spanning chunks."""
    if isinstance(left, list) and isinstance(right, list):
        return list(left) + list(right)
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = list(merged[key]) + list(value)
            else:
                merged[key] = value
        return merged
    return None


def _chunk_raw(s: Store, family: str, chunk_start: dt.date, chunk_end: dt.date) -> Any | None:
    try:
        return s.read_json(s.raw_path(family, chunk_start, chunk_end))
    except StoreError:
        return None


def _window_demands_refresh(family: str, s: Store, from_day: dt.date, to_day: dt.date,
                            today: dt.date) -> bool:
    """True when the piece's days touch today or a stale day (§5.2)."""
    if to_day < from_day:
        return False
    if to_day >= today:
        return True  # today (or future dates in the trailing chunk)
    day = from_day
    while day <= to_day:
        if _day_stale(family, s, day, today):
            return True
        day += dt.timedelta(days=1)
    return False


def _day_stale(family: str, s: Store, day: dt.date, today: dt.date) -> bool:
    """Staleness rule (§5.2): a non-today day D is stale when its
    daily_records.fetched_at predates the end of D (local midnight) plus the
    6 h publish lag. The check is an index lookup; zero upstream calls."""
    if day >= today:
        return False  # today is governed by the TTL window, not the lag rule
    from garmin_mcp.sync import local_day_end

    fetched_at = parse_ts(s.daily_fetched_at(family, day))
    if fetched_at is None:
        return True  # never seen for this day: treat as stale/missing
    boundary = local_day_end(day) + dt.timedelta(seconds=PUBLISH_LAG_SECONDS)
    return fetched_at < boundary


def _seconds_since(ts: dt.datetime | None) -> float:
    if ts is None:
        return float("inf")
    now = dt.datetime.now(dt.timezone.utc)
    return (now - ts).total_seconds()


def _daily_perday(family: str, start: dt.date, end: dt.date) -> dict[str, Any]:
    tools = _tools()
    days = (end - start).days + 1
    if days > ranges.GARMIN_MAX_PERDAY_FANOUT_DAYS:
        raise ValueError(
            f"family {family!r} is a per-day endpoint; at most "
            f"{ranges.GARMIN_MAX_PERDAY_FANOUT_DAYS} days per request (requested {days})")
    s = _store()
    assert s is not None
    today = dt.datetime.now().astimezone().date()
    pieces = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        data = _perday_day_data(s, family, day, today)
        pieces.append({"date": day.isoformat(), "data": data})
    return {"days": pieces, "present": any(has_data(p["data"]) for p in pieces)}


def _perday_day_data(s: Store, family: str, day: dt.date, today: dt.date) -> Any:
    """One per-day piece: the perday file when present and fresh, else a
    single-date live re-fetch with atomic perday replace (§3.5)."""
    raw = None
    try:
        raw = s.read_json(s.perday_path(family, day))
    except StoreError:
        raw = None
    fetched_at = parse_ts(s.daily_fetched_at(family, day))
    need_live = raw is None
    if raw is not None:
        if day >= today:
            need_live = fetched_at is None or _seconds_since(fetched_at) > TTL_SECONDS
        else:
            need_live = _day_stale(family, s, day, today)
    if not need_live:
        return raw
    data = _cached_call(("daily", family, day.isoformat()), family,
                        lambda d=day: _client().fetch_perday(family, d))
    s.write_perday(family, day, data)
    return data


def trend(family: str, weeks: int) -> dict[str, Any]:
    """§3.5 garmin_trend read path: the perday files over the window (the
    same weekly aggregation code as today); days missing or stale go through
    the containing canonical chunk (the ranged freshness edge, §3.5)."""
    tools = _tools()
    weeks = max(1, min(int(weeks), 52))
    end = dt.date.today()
    start = end - dt.timedelta(days=weeks * 7 - 1)
    s = _store()
    assert s is not None
    today = dt.datetime.now().astimezone().date()
    records: list[dict[str, Any]] = []
    day = start
    while day <= end:
        raw = None
        try:
            raw = s.read_json(s.perday_path(family, day))
        except StoreError:
            raw = None
        fetched_at = parse_ts(s.daily_fetched_at(family, day))
        need = raw is None or _day_stale(family, s, day, today) or (
            day >= today and (fetched_at is None
                              or _seconds_since(fetched_at) > TTL_SECONDS))
        if need:
            piece = _daily_ranged(family, day, day)
            data = piece["chunks"][0]["data"] if piece["chunks"] else None
        else:
            data = raw
        records.extend(tools._extract_records(data))
        day += dt.timedelta(days=1)
    return {
        "family": family,
        "weeks": weeks,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "buckets": tools._aggregate_weekly(records, weeks),
    }


def _client():
    from garmin_mcp import client

    return client


# ------------------------------------------------------------------- status


def status_block(now: dt.datetime | None = None) -> dict[str, Any] | None:
    """The additive ``store`` block for garmin_api_status (§5.3). None when
    the store is not configured at all."""
    s = _store()
    current_mode = mode()
    if s is None:
        if current_mode == MODE_OFF and not _DEGRADED:
            return None
        return {"enabled": False, "mode": MODE_OFF}
    block: dict[str, Any] = {"enabled": True, "mode": current_mode}
    try:
        activities, zips = s.counts()
        block["activities"] = activities
        block["zips"] = zips
        coverage: dict[str, Any] = {}
        for family_spec in families.RANGED_FAMILIES:
            coverage[family_spec.name] = s.coverage_summary(
                family_spec.name, family_spec.cap_days, _anchor(s), _today())
        for family in families.PERDAY_FAMILIES_ARCHIVE:
            days = s.daily_rows(family)
            if days:
                coverage[family] = {"chunks": len(days), "holes": 0,
                                    "range": [days[0][0], days[-1][0]]}
        block["coverage"] = coverage
        block["last_sync"] = s.last_sync_per_family()
        from garmin_mcp import sync as sync_mod

        engine = sync_mod.engine()
        if engine is not None:
            block["backfill"] = engine.backfill_progress()
            block["circuits"] = engine.open_circuits()
        else:
            state = _sync_state(s)
            block["backfill"] = _backfill_from_state(state)
            block["circuits"] = _circuits_from_state(state)
        block["degraded"] = _DEGRADED
        block["last_error"] = (_sync_state(s).get("last_error")
                               if engine is None else engine.state.get("last_error"))
    except StoreError as exc:
        # §5.3: when index.db itself is unreadable, render degraded with no
        # counts; reads fall back live per §5.2.
        block["degraded"] = "index unreadable"
        block["last_error"] = str(exc)
    block["off_host"] = _off_host(s, now)
    return block


def _today() -> dt.date:
    return dt.datetime.now().astimezone().date()


def _sync_state(s: Store) -> dict[str, Any]:
    try:
        raw = s.read_json(s.root / "state" / "sync-state.json")
        return raw if isinstance(raw, dict) else {}
    except StoreError:
        return {}


def _backfill_from_state(state: dict[str, Any]) -> dict[str, str]:
    backfill = state.get("backfill") or {}
    stages = len(backfill.get("stages_complete", []) or [])
    return {"state": str(backfill.get("state", "unknown")),
            "progress": f"{stages} of 5 stages"}


def _circuits_from_state(state: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    out = {}
    for family, entry in (state.get("circuits") or {}).items():
        until = parse_ts(entry.get("open_until"))
        if until and until.timestamp() > now:
            out[family] = dict(entry)
    return out


def _off_host(s: Store, now: dt.datetime | None = None) -> dict[str, Any]:
    """Rendered from state/backup-status.json (§5.3), AGE-AWARE (§6.1 step 9):
    missing, false, or stale (>~30 h) renders healthy:false with the reason,
    so a dead checker turns the alarm on within a day. age_hours is recomputed
    at render (now minus the file's last_upload)."""
    path = s.root / "state" / "backup-status.json"
    if not path.exists():
        return {"healthy": False, "reason": "state/backup-status.json missing",
                "last_upload": None, "newest_drive_item": None,
                "age_hours": None, "source": "state/backup-status.json"}
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return {"healthy": False, "reason": f"unreadable backup-status.json: {exc}",
                "last_upload": None, "newest_drive_item": None,
                "age_hours": None, "source": "state/backup-status.json"}
    last_upload = data.get("last_upload")
    newest = data.get("newest_drive_item")
    file_ok = bool(data.get("off_host_healthy"))
    reasons: list[str] = []
    if not file_ok:
        reasons.append(str(data.get("reason") or "checker reported unhealthy"))
    stale_after = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(hours=STATUS_STALE_HOURS)
    written_at = parse_ts(last_upload)
    try:
        file_mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        file_mtime = None
    if (file_mtime is not None and file_mtime < stale_after) or (
            written_at is not None and written_at < stale_after):
        reasons.append(f"status file stale (older than {STATUS_STALE_HOURS} h)")
    age_hours: int | None = None
    if written_at is not None:
        current = now or dt.datetime.now(dt.timezone.utc)
        age_hours = max(0, round((current - written_at).total_seconds() / 3600))
    healthy = not reasons
    out = {"healthy": healthy, "last_upload": last_upload,
           "newest_drive_item": newest, "age_hours": age_hours,
           "source": "state/backup-status.json"}
    if reasons:
        out["reason"] = "; ".join(reasons)
    return out
