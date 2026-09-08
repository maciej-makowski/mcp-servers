#!/usr/bin/env python3
"""Google Workspace MCP server (FastMCP, stdio).

Source of truth for Google API operations — absorbed from the
google-workspace skill script. Tool docstrings mark WRITE ops.
Audit log: ~/.hermes/mcp-logs/google/ (args redacted, outputs captured).
"""
import json

from mcp.server.fastmcp import FastMCP

from google_mcp import audit, gmail, gsuite

mcp = FastMCP("google")
_S = "google"


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
