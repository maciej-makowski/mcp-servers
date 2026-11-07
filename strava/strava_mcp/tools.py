"""MCP tool layer: read-only action tables for the three v1 tools (D6).

The action tables here drive both dispatch and the server's JSON schemas
(server.py builds the schemas from these tables), so the listed and
callable actions cannot drift (section 3.2). Everything is a read (F1: no
writes of any kind in v1); streams, zones and laps ride inside
strava_activities per section 5.

Upstream failures come back as structured dicts (success=false with the
upstream status) instead of raised exceptions, so the agent sees Strava's
error fields as-is (F3) and the audit layer captures them. Validation
failures are tagged (validation=true) and never reach the network.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from . import audit, client, oauth, rate

TOOL_NAME = "strava"

# W9: the full inline-enum key set the streams endpoint allows. The tool
# always requests this whole set (keys is required, key_by_type must be
# true); there is no caller-chosen subset in v1.
STREAM_KEYS = (
    "time",
    "latlng",
    "distance",
    "altitude",
    "velocity_smooth",
    "hearthrate",
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


class _BaseTool:
    """Dispatch + error shaping shared by the three tools (reads only)."""

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
        except httpx.HTTPError as exc:
            return {"success": False, "error": f"transport error: {exc}"}


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

    def _do_list(self, params: dict) -> dict[str, Any]:
        per_page = _as_int(params.get("per_page"), "per_page")
        per_page = DEFAULT_PER_PAGE if per_page is None else max(1, min(per_page, MAX_PER_PAGE))
        pages = _as_int(params.get("pages"), "pages")
        pages = 1 if pages is None else max(1, min(pages, MAX_PAGES_PER_CALL))
        page = _as_int(params.get("page"), "page")
        page = 1 if page is None else max(1, page)
        query: dict[str, Any] = {"per_page": per_page}
        for key in ("after", "before"):
            epoch = _as_epoch(params.get(key), key)
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

    def _do_get(self, params: dict) -> dict[str, Any]:
        activity_id = self._activity_id(params)
        if activity_id is None:
            return _validation_error("get requires an integer 'id'")
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
        payload = client.api_get(
            f"/activities/{activity_id}/streams",
            params={"keys": list(STREAM_KEYS), "key_by_type": "true"},
        )
        if not isinstance(payload, dict):
            return {"success": False, "error": "unexpected streams response shape"}
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


def _reshape_summary(activity: Any) -> dict[str, Any]:
    """Compact summary dict per section 5 (sport_type only, no deprecated type)."""
    if not isinstance(activity, dict):
        return {"raw": activity}
    return {field: activity.get(field) for field in SUMMARY_FIELDS}


ATHLETE_TOOL = AthleteTool()
ACTIVITIES_TOOL = ActivitiesTool()
RATE_STATUS_TOOL = RateStatusTool()

# Section 3.5: every tool call goes through the house log_call decorator
# (redacted args, duration, outcome; per-tool JSONL under MCP_LOGS_DIR).
ATHLETE_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_athlete")(ATHLETE_TOOL.dispatch)
ACTIVITIES_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_activities")(ACTIVITIES_TOOL.dispatch)
RATE_STATUS_TOOL.dispatch = audit.log_call(TOOL_NAME, "strava_rate_status")(RATE_STATUS_TOOL.dispatch)
