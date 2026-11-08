#!/usr/bin/env python3
"""Garmin Connect MCP server (FastMCP).

Transport: stdio by default. HTTP (streamable-http) is enabled when MCP_BEARER
or MCP_PORT is set — HTTP requires MCP_BEARER (fail closed if missing).
Host/port come from mcp_auth.resolve_http_config (MCP_HOST defaults to
127.0.0.1; containers set MCP_HOST=0.0.0.0 explicitly to accept forwarded
connections — publishing stays host-loopback).

Live reads via the official Garmin Connect Personal API: the bearer token is
derived from GARMIN_API_KEY by the official client (24h token, auto-renewed
in-process). No SQLite mirror and no sync job in v1 (Maciek's decision,
2026-11-07).
Audit log: $MCP_LOGS_DIR/garmin/ (args redacted, outputs captured).
"""
import contextlib
import json
import os

from mcp.server.fastmcp import FastMCP

from garmin_mcp import audit, tools

DEFAULT_HTTP_PORT = 8017
_S = "garmin"


mcp = FastMCP("garmin")


@mcp.tool()
@audit.log_call(_S, "garmin_recent_activities")
def garmin_recent_activities(limit: int = 20, since: str = "") -> str:
    """List recent activities, optional since date (YYYY-MM-DD). Read-only."""
    return json.dumps(tools.recent_activities(limit=limit, since=since),
                      ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "garmin_activity")
def garmin_activity(activity_id: str) -> str:
    """Read one activity's details by ID. Read-only."""
    return json.dumps(tools.activity(activity_id), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "garmin_activity_file")
def garmin_activity_file(activity_id: str) -> str:
    """Summarise the activity file ZIP (size, sha256, type); never attaches it.
    Read-only."""
    return json.dumps(tools.activity_file(activity_id), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "garmin_daily")
def garmin_daily(start_date: str, end_date: str, families: str = "all",
                 body_battery_chunks: int = 0) -> str:
    """Read daily families over an inclusive date range: stress, steps,
    hydration, weight, sleep, body battery (optional chunks), daily summaries,
    HRV, SpO2, fitness age, heart rate. One call per family per split chunk.
    Read-only."""
    return json.dumps(tools.daily(start_date, end_date, families,
                                  body_battery_chunks), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "garmin_trend")
def garmin_trend(family: str, weeks: int = 8) -> str:
    """Weekly aggregates for one ranged family over the trailing N weeks.
    Read-only."""
    return json.dumps(tools.trend(family, weeks), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "garmin_workout_usage")
def garmin_workout_usage(activity_id: str) -> str:
    """List the structured workouts attached to an activity. Read-only."""
    return json.dumps(tools.workout_usage(activity_id), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "garmin_api_status")
def garmin_api_status() -> str:
    """Bearer-token validity/expiry, per-family last errors, cache stats.
    Read-only."""
    return json.dumps(tools.api_status(), ensure_ascii=False)


def _resolve_run_mode() -> tuple:
    """Return ('stdio',) or ('http', host, port).

    stdio stays the default when neither MCP_BEARER nor MCP_PORT is set.
    HTTP requires MCP_BEARER (fail closed: never serve HTTP unauthenticated).
    """
    from mcp_auth import resolve_http_config

    bearer = os.environ.get("MCP_BEARER")
    if bearer is None and os.environ.get("MCP_PORT") is None:
        return ("stdio",)
    if bearer is None:
        raise RuntimeError(
            "HTTP transport requested (MCP_PORT set) but MCP_BEARER is not set")
    host, port = resolve_http_config(DEFAULT_HTTP_PORT)
    return ("http", host, port)


if __name__ == "__main__":
    mode = _resolve_run_mode()
    if mode[0] == "stdio":
        mcp.run(transport="stdio")
    else:
        _, host, port = mode
        from mcp_auth import require_bearer

        import uvicorn

        app = require_bearer("MCP_BEARER")(mcp.streamable_http_app())
        uvicorn.run(app, host=host, port=port)
