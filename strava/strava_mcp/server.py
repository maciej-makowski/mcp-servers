"""MCP server exposing Strava v3 reads: strava_athlete, strava_activities,
strava_rate_status, and the local store's strava_sync (strava-local-store
design D9).

The proton skeleton (low-level `mcp` Server with on_list_tools /
on_call_tool handlers) with the shared bearer middleware from
_shared/mcp_auth: stdio stays the local dev default; HTTP (streamable-http)
is enabled when MCP_BEARER or MCP_PORT is set and fails closed without a
bearer token. The quadlet always runs HTTP on 8016.

Schemas are built from the tool layer's own action tables, so listed and
callable actions cannot drift (section 3.2). Idle token refresh and the
tiered local-store sync run alongside the server in both transports
(section 3.2 and the sync design's A4: one asyncio task next to the
idle-refresh task; audit logging rides on the tool layer, section 3.5).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import mcp_types as types
from mcp.server import lowlevel
from mcp.server.stdio import stdio_server

from . import idle_refresh, oauth, sync, tools

SERVER_NAME = "strava-mcp"
DEFAULT_HTTP_PORT = 8016

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "strava_athlete": {
        "tool": tools.ATHLETE_TOOL,
        "description": (
            "Strava athlete via the v3 API (read-only): actions get (full "
            "profile record), zones (heart-rate and power zones), stats "
            "(activity stats; id optional and defaults to the authenticated "
            "athlete's id from the token store)."
        ),
        "params": tools.ATHLETE_PARAMS,
    },
    "strava_activities": {
        "tool": tools.ACTIVITIES_TOOL,
        "description": (
            "Strava activities via the v3 API (read-only): actions list "
            "(after/before epoch seconds, page, per_page default 30 clamped "
            "to 100, pages cap of 4 per call), get (detailed record, "
            "optional include_all_efforts), streams (full key whitelist as "
            "one CSV with key_by_type=true, downsampled to max_points "
            "unless raw=true), zones, laps. Optional source=store serves "
            "list/get/streams from the local store with zero API requests "
            "(D9)."
        ),
        "params": tools.ACTIVITIES_PARAMS,
    },
    "strava_rate_status": {
        "tool": tools.RATE_STATUS_TOOL,
        "description": (
            "Strava rate budget (no API call): action status reports the "
            "last-seen 15-minute and daily budgets, the reserves in force "
            "(D5: 10 of 100 per 15 minutes, 100 of 1,000 per day), and the "
            "next reset times."
        ),
        "params": {},
    },
    "strava_sync": {
        "tool": tools.SYNC_TOOL,
        "description": (
            "Local Strava store sync (strava_sync, D9): action status "
            "reports the per-tier schedule and last runs, the watermark, "
            "the backfill cursor and remaining count, store counts, "
            "scope_blocked flags, and the current rate view in one result "
            "(no API call); action trigger enqueues a tier "
            "(frequent|slow|sweep|backfill) for the next scheduler tick. "
            "The first backfill is trigger-only and waits for the "
            "operator's go (STRAVA_SYNC_ENABLED)."
        ),
        "params": tools.SYNC_PARAMS,
    },
}


def build_input_schema(spec_name: str) -> dict[str, Any]:
    """Build the JSON schema for one registered tool from its action tables."""
    spec = TOOL_SPECS[spec_name]
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(spec["tool"].ACTIONS),
                "description": "One of the actions in the enum.",
            },
            "params": {
                "type": "object",
                "additionalProperties": False,
                "properties": spec["params"],
                "description": "Action-specific parameters (see each action's needs).",
            },
        },
        "required": ["action"],
    }


INPUT_SCHEMAS = {name: build_input_schema(name) for name in TOOL_SPECS}


async def call_tool_impl(name: str, arguments: dict[str, Any]) -> dict:
    """Async MCP entry point: dispatch off the event loop via to_thread."""
    return await asyncio.to_thread(call_tool_sync, name, arguments)


def call_tool_sync(name: str, arguments: dict[str, Any]) -> dict:
    """Dispatch an MCP tool call to the tool layer (testable without stdio).

    Runs synchronously so the blocking HTTP call never blocks the event
    loop; call_tool_impl awaits it via asyncio.to_thread.
    """
    if name not in TOOL_SPECS:
        raise ValueError(f"unknown tool {name!r}")
    arguments = dict(arguments or {})
    action = arguments.get("action")
    if not isinstance(action, str):
        raise ValueError("params 'action' (string) is required")
    params = dict(arguments.get("params") or {})
    if not isinstance(params, dict):
        raise ValueError("'params' must be an object")
    return TOOL_SPECS[name]["tool"].dispatch(action, params)


def make_server() -> lowlevel.Server:
    """Build the low-level MCP Server with list/call tool handlers."""

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=TOOL_SPECS[name]["description"],
                    input_schema=INPUT_SCHEMAS[name],
                )
                for name in TOOL_SPECS
            ]
        )

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        name = params.name
        try:
            result = await call_tool_impl(name, dict(params.arguments or {}))
        except ValueError as exc:
            raise ValueError(f"invalid arguments for {name}: {exc}") from exc
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))]
        )

    return lowlevel.Server(
        SERVER_NAME,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _run_stdio() -> None:
    server = make_server()
    refresh_task = idle_refresh.start_idle_refresh()
    sync_task = sync.start_sync()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
    finally:
        refresh_task.cancel()
        sync_task.cancel()


# ---------------------------------------------------------------------------
# HTTP transport (streamable-http) with the shared bearer middleware
# ---------------------------------------------------------------------------
# Selected when MCP_BEARER or MCP_PORT is set; stdio stays the default. HTTP
# requires MCP_BEARER (fail closed: an unauthenticated HTTP listener cannot
# be started by accident). Host/port come from
# _shared/mcp_auth.resolve_http_config(8016); the quadlet bakes
# MCP_HOST=0.0.0.0 and MCP_PORT=8016 into the image and publishes
# host-loopback only.


def _resolve_run_mode() -> tuple:
    """Return ('stdio',) or ('http', host, port)."""
    from mcp_auth import resolve_http_config

    bearer = os.environ.get("MCP_BEARER")
    if bearer is None and os.environ.get("MCP_PORT") is None:
        return ("stdio",)
    if not bearer:
        raise RuntimeError(
            "HTTP transport requested (MCP_PORT set) but MCP_BEARER is not set"
        )
    host, port = resolve_http_config(DEFAULT_HTTP_PORT)
    return ("http", host, port)


def make_http_asgi(server: Any) -> Any:
    """Build the Starlette app serving the server over streamable-http at /mcp."""
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp_auth import require_bearer
    from starlette.applications import Starlette
    from starlette.routing import Route

    # stateless=True: each HTTP request is a self-contained MCP exchange, so
    # the ASGI process holds no per-client session state.
    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True
    )

    async def handle_streamable_http(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app: Any):
        refresh_task = idle_refresh.start_idle_refresh()
        sync_task = sync.start_sync()
        try:
            async with manager.run():
                yield
        finally:
            refresh_task.cancel()
            sync_task.cancel()

    # Explicit Route on /mcp (not Mount, which 307-redirects /mcp -> /mcp/
    # before the auth middleware can see the request). The middleware comes
    # from _shared/mcp_auth (fail-closed: it refuses to build without
    # MCP_BEARER in the environment).
    app = Starlette(
        routes=[
            Route(
                "/mcp",
                require_bearer("MCP_BEARER")(handle_streamable_http),
                methods=["POST", "GET", "DELETE"],
            )
        ],
        lifespan=lifespan,
    )
    return app


async def _run_http(host: str, port: int) -> None:
    """Serve the MCP server over streamable-http with bearer auth."""
    import uvicorn

    config = uvicorn.Config(
        make_http_asgi(make_server()),
        host=host,
        port=port,
        log_level="info",
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    """Entry point: MCP stdio by default; HTTP when MCP_BEARER/MCP_PORT set."""
    mode = _resolve_run_mode()
    if mode[0] == "http":
        _, host, port = mode
        asyncio.run(_run_http(host, port))
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
