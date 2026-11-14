"""MCP tool layer: read-only action tables (design section 5) + the local
store's sync tool (strava-local-store design D9, section 5).

The action tables here drive both dispatch and the server's JSON schemas
(server.py builds the schemas from these tables), so the listed and
callable actions cannot drift (section 3.2). Everything is a read (F1: no
writes of any kind); streams, zones and laps ride inside
strava_activities per section 5. The strava_sync tool is the local store's
manual surface: status makes no API call, trigger enqueues a tier for the
next scheduler tick (L2).

strava_activities gains an optional source parameter (D9): "api" (default)
keeps the live v3 path, "store" serves list/get/streams from the local
store with the same response shapes and makes zero API requests. A store
request on an empty or missing store returns a structured error naming
strava_sync - no silent fallback to live reads, because the caller asked
for the store.

Upstream failures come back as structured dicts (success=false with the
upstream status) instead of raised exceptions, so the agent sees Strava's
error fields as-is (F3) and the audit layer captures them. Validation
failures are tagged (validation=true) and never reach the network.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from . import audit, client, oauth, rate, store as store_mod, sync as sync_mod

TOOL_NAME = "strava"

# W9: the full inline-enum key set the streams endpoint allows. The tool
# always requests this whole set (keys is required, key_by_type must be
# true); there is no caller-chosen subset in v1. The keys ride as one CSV
# string (the list form under-fetched: the API answered only two series,
# the design's under-fetch defect, fixed here).
STREAM_KEYS = (
    "time",
    "latlng",
    "distance",
    "altitude",
    "velocity_smooth",
    "heartrate",
    "cadence",
    "watts",
    "temp",
    "moving",
    "grade_smooth",
)

# List reshaping: compact summary dicts per section 5 (sport_type only, the
# deprecated type field is dropped), page clamp, and the server-side page
# cap that keeps one call from quietly spending the daily budget.
SUMMARY_FIELDS = (
    "id",
    "name",
    "sport_type",
    "start_date",
    "distance",
    "moving_time",
    "total_elevation_gain",
    "average_speed",
    "max_speed",
    "private",
)
DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100  # our clamp, not an API claim (section 2.1)
MAX_PAGES_PER_CALL = 4  # server-side page cap (section 5)
DEFAULT_MAX_POINTS = 1000  # streams downsample target (section 5)
RAW_WARN_TOTAL_POINTS = 50000  # raw=true warns past this many total points

# Shared param table: every listed parameter exists, every dispatched
# parameter is listed (schemas are built from this table in server.py).
ATHLETE_PARAMS: dict[str, Any] = {
    "id": {
        "type": "integer",
        "description": "Athlete id (stats action); defaults to the authenticated athlete from the token store",
    },
}
ACTIVITIES_PARAMS: dict[str, Any] = {
    "id": {
        "type": "integer",
        "description": "Activity id (get, streams, zones, laps)",
    },
    "after": {
        "type": "integer",
        "description": "Epoch seconds: only activities after this (list)",
    },
    "before": {
        "type": "integer",
        "description": "Epoch seconds: only activities before this (list)",
    },
    "page": {
        "type": "integer",
        "description": "Starting page, from 1 (list; default 1)",
    },
    "per_page": {
        "type": "integer",
        "description": "Page size, clamped to 100 (list; default 30)",
    },
    "pages": {
        "type": "integer",
        "description": "Consecutive pages to fetch in this call, clamped to 4 (list; default 1)",
    },
    "include_all_efforts": {
        "type": "boolean",
        "description": "Include all segment efforts (get)",
    },
    "raw": {
        "type": "boolean",
        "description": "Return full-resolution streams without downsampling; warns when the payload is large (streams)",
    },
    "max_points": {
        "type": "integer",
        "description": "Uniform-stride downsample target per series (streams; default 1000)",
    },
    "source": {
        "type": "string",
        "enum": ["api", "store"],
        "description": "Where list/get/streams serve from: api (default) hits the v3 API, store reads the local store with zero API requests",
    },
}
SYNC_PARAMS: dict[str, Any] = {
    "tier": {
        "type": "string",
        "enum": ["frequent", "slow", "sweep", "backfill"],
        "description": "Which tier to enqueue (trigger); the batch starts at the next scheduler tick, behind anything running",
    },
}


def _validation_error(msg: str) -> dict[str, Any]:
    """House shape for a local validation failure (no request was sent)."""
    return {"success": False, "validation": True, "error": msg}


def _as_int(value: Any, what: str) -> int | None:
    """Coerce an id-like parameter to int; None means it is unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_epoch(value: Any, what: str) -> int | None:
    epoch = _as_int(value, what)
    if epoch is None or epoch < 0:
        return None
    return epoch


def _downsample(data: list, max_points: int) -> list:
    """Uniform stride downsample (section 5): keep every Nth sample."""
    n = len(data)
    if n <= max_points:
        return data
    stride = math.ceil(n / max_points)
    return data[::stride]


def _shape_streams(payload: dict[str, Any], activity_id: int, raw: bool,
                   max_points: int) -> dict[str, Any]:
    """Shared streams shaping for both sources (same shapes, D9)."""
    series: dict[str, Any] = {}
    total_points = 0
    for key, record in payload.items():
        data = record.get("data") if isinstance(record, dict) else None
        if data is None:
            series[key] = record  # passthrough: unexpected shapes stay visible
            continue
        total_points += len(data)
        series[key] = {
            **record,
            "data": data if raw else _downsample(data, max_points),
        }
    out: dict[str, Any] = {
        "success": True,
        "activity_id": activity_id,
        "raw": raw,
        "max_points": max_points,
        "streams": series,
    }
    if raw and total_points > RAW_WARN_TOTAL_POINTS:
        out["warning"] = (
            f"raw payload is large ({total_points} total points across series); "
            "consider downsampling via max_points"
        )
    return out


class _BaseTool:
    """Dispatch + error shaping shared by the tools (reads only)."""

    ACTIONS: tuple[str, ...] = ()

    def dispatch(self, action: str, params: dict | None = None) -> dict[str, Any]:
        params = dict(params or {})
        if action not in self.ACTIONS:
            return _validation_error(
                f"unknown action {action!r} for {type(self).__name__}; "
                f"expected one of: {', '.join(self.ACTIONS)}"
            )
        try:
            return getattr(self, f"_do_{action}")(params)
        except rate.RateGuardError as exc:
            # Section 3.4: the guard refuses (or Strava 429s) and returns the
            # wait to the caller instead of retrying internally.
            return {"success": False, "rate_limited": True, "error": str(exc), "wait": exc.wait}
        except client.StravaAPIError as exc:
            return {"success": False, "status": exc.status, "error": str(exc)}
        except oauth.TokenError as exc:
            return {"success": False, "error": str(exc)}
        except store_mod.StoreError as exc:
            return {"success": False, "error": str(exc)}
        except httpx.HTTPError as exc:
            return {"success": False, "error": f"transport error: {exc}"}


def _store_miss(activity_id: int, what: str) -> dict[str, Any]:
    """D9: a store read for an id the store has not fetched yet."""
    return {
        "success": False,
        "store_miss": True,
        "error": (
            f"the store has no {what} for activity {activity_id}; "
            "run strava_sync (action status, then trigger backfill)"
        ),
    }


def _store_empty() -> dict[str, Any]:
    """D9: a store read while the store has no activities at all."""
    return {
        "success": False,
        "store_empty": True,
        "error": (
            "the local store is empty: run strava_sync (action status, then "
            "trigger backfill) to populate it; the first backfill is "
            "trigger-only and waits for the operator's go"
        ),
    }


class AthleteTool(_BaseTool):
    """strava_athlete: profile, zones, stats (section 5, tool 1)."""

    ACTIONS = ("get", "zones", "stats")

    def _do_get(self, params: dict) -> dict[str, Any]:
        return client.api_get("/athlete")

    def _do_zones(self, params: dict) -> dict[str, Any]:
        return client.api_get("/athlete/zones")

    def _do_stats(self, params: dict) -> dict[str, Any]:
        athlete_id = _as_int(params.get("id"), "id")
        if athlete_id is None:
            # Default to the authenticated athlete: the exchange payload
            # carries the athlete object (W3), so the token store has it.
            athlete_id = oauth.athlete_id(oauth.load_store())
            if athlete_id is None:
                return _validation_error(
                    "stats needs an 'id': the token store has no athlete id "
                    "(re-authorize) and none was passed"
                )
        return client.api_get(f"/athletes/{athlete_id}/stats")


class ActivitiesTool(_BaseTool):
    """strava_activities: list, get, streams, zones, laps (section 5, tool 2)."""

    ACTIONS = ("list", "get", "streams", "zones", "laps")

    def _activity_id(self, params: dict) -> int | None:
        return _as_int(params.get("id"), "id")

    @staticmethod
    def _use_store(params: dict) -> bool:
        return params.get("source") == "store"

    def _do_list(self, params: dict) -> dict[str, Any]:
        per_page = _as_int(params.get("per_page"), "per_page")
        per_page = DEFAULT_PER_PAGE if per_page is None else max(1, min(per_page, MAX_PER_PAGE))
        pages = _as_int(params.get("pages"), "pages")
        pages = 1 if pages is None else max(1, min(pages, MAX_PAGES_PER_CALL))
        page = _as_int(params.get("page"), "page")
        page = 1 if page is None else max(1, page)
        after = _as_epoch(params.get("after"), "after")
        before = _as_epoch(params.get("before"), "before")
        if self._use_store(params):
            return self._store_list(page, per_page, pages, after, before)
        query: dict[str, Any] = {"per_page": per_page}
        for key, epoch in (("after", after), ("before", before)):
            if epoch is not None:
                query[key] = epoch
        records: list[Any] = []
        fetched_pages = 0
        for current in range(page, page + pages):
            batch = client.api_get(
                "/athlete/activities", params={**query, "page": current}
            )
            if not isinstance(batch, list):
                return {"success": False, "error": "unexpected list response shape"}
            records.extend(_reshape_summary(a) for a in batch)
            fetched_pages += 1
            if len(batch) < per_page:
                break  # last page reached: no pointless extra request
        return {
            "success": True,
            "activities": records,
            "page": page,
            "pages": fetched_pages,
            "count": len(records),
        }

    def _store_list(self, page: int, per_page: int, pages: int,
                    after: int | None, before: int | None) -> dict[str, Any]:
        """D9 store path: the index + the raw summary files, zero API calls."""
        store = store_mod.Store()
        if store.counts()["activities"] == 0:
            return _store_empty()
        records: list[Any] = []
        fetched_pages = 0
        for current in range(page, page + pages):
            rows = store.list_summaries(page=current, per_page=per_page,
                                        after=after, before=before)
            for row in rows:
                raw = store.summary_raw(row["id"])
                records.append(_reshape_summary(raw) if raw else row)
            fetched_pages += 1
            if len(rows) < per_page:
                break
        return {
            "success": True,
            "source": "store",
            "activities": records,
            "page": page,
            "pages": fetched_pages,
            "count": len(records),
        }

    def _do_get(self, params: dict) -> dict[str, Any]:
        activity_id = self._activity_id(params)
        if activity_id is None:
            return _validation_error("get requires an integer 'id'")
        if self._use_store(params):
            payload = store_mod.Store().detail_payload(activity_id)
            if payload is None:
                return _store_miss(activity_id, "detail")
            return payload
        query = None
        if params.get("include_all_efforts") is True:
            query = {"include_all_efforts": "true"}
        return client.api_get(f"/activities/{activity_id}", params=query)

    def _do_streams(self, params: dict) -> dict[str, Any]:
        activity_id = self._activity_id(params)
        if activity_id is None:
            return _validation_error("streams requires an integer 'id'")
        raw = params.get("raw") is True
        max_points = _as_int(params.get("max_points"), "max_points")
        max_points = DEFAULT_MAX_POINTS if max_points is None else max_points
        if max_points < 1:
            return _validation_error("'max_points' must be a positive integer")
        if self._use_store(params):
            payload = store_mod.Store().streams_payload(activity_id)
            if payload is None:
                return _store_miss(activity_id, "streams")
            return _shape_streams(payload, activity_id, raw, max_points)
        payload = client.api_get(
            f"/activities/{activity_id}/streams",
            params={"keys": ",".join(STREAM_KEYS), "key_by_type": "true"},
        )
        if not isinstance(payload, dict):
            return {"success": False, "error": "unexpected streams response shape"}
        return _shape_streams(payload, activity_id, raw, max_points)

    def _do_zones(self, params: dict) -> dict[str, Any]:
        activity_id = self._activity_id(params)
        if activity_id is None:
            return _validation_error("zones requires an integer 'id'")
        return client.api_get(f"/activities/{activity_id}/zones")

    def _do_laps(self, params: dict) -> dict[str, Any]:
        activity_id = self._activity_id(params)
        if activity_id is None:
            return _validation_error("laps requires an integer 'id'")
        return client.api_get(f"/activities/{activity_id}/laps")


class RateStatusTool(_BaseTool):
    """strava_rate_status: tracked budgets, no API call (section 5, tool 3)."""

    ACTIONS = ("status",)

    def _do_status(self, params: dict) -> dict[str, Any]:
        info = rate.STATE.status()
        info["success"] = True
        info["api_call"] = False
        return info


class SyncTool(_BaseTool):
    """strava_sync: the local store's manual surface (D9, section 5).

    status: no API call. Reports the per-tier schedule and last runs, the
    watermark, the backfill cursor and remaining count, the store counts,
    the scope_blocked flags, and the current rate view, so one call answers
    "is the sync healthy and what does the budget look like".

    trigger: enqueues a tier for the next scheduler tick. The scheduler
    never cold-starts the backfill and the frequent tier no-ops on an empty
    store, so only this action can start the first population (the
    operator's go flips STRAVA_SYNC_ENABLED first, rollout step 6).
    """

    ACTIONS = ("status", "trigger")

    def _do_status(self, params: dict) -> dict[str, Any]:
        engine = sync_mod.SCHEDULER
        out: dict[str, Any] = {
            "success": True,
            "api_call": False,
            "sync_enabled": sync_mod.enabled(),
            "intervals": {
                "frequent_hours": engine.intervals()["frequent"] / 3600.0,
                "slow_hours": engine.intervals()["slow"] / 3600.0,
                "sweep_days": engine.intervals()["sweep"] / 86400.0,
            },
            "bulk_cap_per_quarter": engine.bulk_cap(),
            "queue": list(engine._queue),
            "scope_blocked": {
                "segments": not sync_mod.read_all_granted(),
                "routes": not sync_mod.read_all_granted(),
            },
            "rate": rate.STATE.status(),
        }
        try:
            store = store_mod.Store()
            out["store_counts"] = store.counts()
            out["last_runs"] = store.last_run_per_tier()
            out["watermark"] = store.get_state("frequent_watermark")
            cursor = store.get_state("backfill_cursor")
            out["backfill_cursor"] = int(cursor) if cursor else None
            out["backfill_remaining"] = len(store.summaries_missing_backfill())
            out["recent_batches"] = store.last_batches(limit=5)
        except store_mod.StoreError as exc:
            out["store_error"] = str(exc)
        return out

    def _do_trigger(self, params: dict) -> dict[str, Any]:
        tier = params.get("tier")
        if not isinstance(tier, str) or tier not in sync_mod.TIERS:
            return _validation_error(
                f"trigger requires a 'tier' of: {', '.join(sync_mod.TIERS)}"
            )
        try:
            queued = sync_mod.SCHEDULER.enqueue(tier)
        except ValueError as exc:
            return _validation_error(str(exc))
        out = {
            "success": True,
            "api_call": False,
            "tier": tier,
            **queued,
            "note": "the batch starts at the next scheduler tick, behind anything running",
        }
        if not sync_mod.enabled():
            out["sync_enabled"] = False
            out["warning"] = (
                "STRAVA_SYNC_ENABLED is false: nothing will run until the "
                "operator flips it (rollout step 6)"
            )
        return out


def _reshape_summary(activity: Any) -> dict[str, Any]:
    """Compact summary dict per section 5 (sport_type only, no deprecated type)."""
    if not isinstance(activity, dict):
        return {"raw": activity}
    return {field: activity.get(field) for field in SUMMARY_FIELDS}


ATHLETE_TOOL = AthleteTool()
ACTIVITIES_TOOL = ActivitiesTool()
RATE_STATUS_TOOL = RateStatusTool()
SYNC_TOOL = SyncTool()

# Section 3.5: every tool call goes through the house log_call decorator
# (redacted args, duration, outcome; per-tool JSONL under MCP_LOGS_DIR).
ATHLETE_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_athlete")(ATHLETE_TOOL.dispatch)
ACTIVITIES_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_activities")(ACTIVITIES_TOOL.dispatch)
RATE_STATUS_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_rate_status")(RATE_STATUS_TOOL.dispatch)
SYNC_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_sync")(SYNC_TOOL.dispatch)
