#!/usr/bin/env python3
"""Google Workspace MCP server (FastMCP).

Transport: stdio by default. HTTP (streamable-http) is enabled when MCP_BEARER
or MCP_PORT is set — HTTP requires MCP_BEARER (fail closed if missing).
Host/port come from mcp_auth.resolve_http_config (MCP_HOST defaults to
127.0.0.1; containers set MCP_HOST=0.0.0.0 explicitly to accept forwarded
connections — publishing stays host-loopback).

Audit log: ~/.hermes/mcp-logs/google/ (args redacted, outputs captured).
"""
import contextlib
import json
import os

from mcp.server.fastmcp import FastMCP

from google_mcp import audit, client, gmail, gsuite, idle_refresh
from google_mcp.client import TOKEN_PATH

DEFAULT_HTTP_PORT = 8011
_S = "google"


@contextlib.asynccontextmanager
async def _lifespan(server: FastMCP):
    """Idle-refresh loop alongside the server (Task 6c)."""
    task = idle_refresh.start_idle_refresh(
        client.refresh_credentials, TOKEN_PATH)
    try:
        yield
    finally:
        task.cancel()


mcp = FastMCP("google", lifespan=_lifespan)


@mcp.tool()
@audit.log_call(_S, "gmail_search")
def gmail_search(query: str, max_results: int = 20) -> str:
    """Search Gmail messages. Read-only."""
    return json.dumps(gmail.search(query, max_results), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "gmail_get")
def gmail_get(message_id: str) -> str:
    """Read one Gmail message (headers + body). Read-only."""
    return json.dumps(gmail.get(message_id), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "gmail_send")
def gmail_send(to: str, subject: str, body: str, html: bool = False) -> str:
    """Send a Gmail message. WRITE operation."""
    return json.dumps(gmail.send(to, subject, body, html), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "calendar_list_events")
def calendar_list_events(max_results: int = 10, start: str = "", end: str = "") -> str:
    """List upcoming calendar events (next 7 days by default). Read-only."""
    return json.dumps(gsuite.list_events(max_results, start, end), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "calendar_create_event")
def calendar_create_event(subject: str, start: str, end: str,
                          description: str = "", location: str = "") -> str:
    """Create a calendar event on the primary calendar. WRITE operation."""
    return json.dumps(gsuite.create_event(subject, start, end, description, location),
                      ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "drive_search")
def drive_search(query: str, max_results: int = 10) -> str:
    """Search Drive files by name. Read-only."""
    return json.dumps(gsuite.drive_search(query, max_results), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "sheets_get")
def sheets_get(sheet_id: str, range_name: str) -> str:
    """Read a range from a Google Sheet. Read-only."""
    return json.dumps(gsuite.sheets_get(sheet_id, range_name), ensure_ascii=False)


@mcp.tool()
@audit.log_call(_S, "sheets_update")
def sheets_update(sheet_id: str, range_name: str, values_json: str) -> str:
    """Write values (JSON array of arrays) to a Google Sheet. WRITE operation."""
    return json.dumps(gsuite.sheets_update(sheet_id, range_name,
                                           json.loads(values_json)),
                      ensure_ascii=False)


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
