#!/usr/bin/env python3
"""Outlook / M365 MCP server (FastMCP).

Transport: stdio by default. HTTP (streamable-http) is enabled when MCP_BEARER
or MCP_PORT is set — HTTP requires MCP_BEARER (fail closed if missing).
Host/port come from mcp_auth.resolve_http_config (MCP_HOST defaults to
127.0.0.1; containers set MCP_HOST=0.0.0.0 explicitly to accept forwarded
connections — publishing stays host-loopback).

The source of truth for Graph operations lives here — the office365 skill
scripts were absorbed into outlook_mcp/. Tool docstrings mark WRITE ops.
Audit log: ~/.hermes/mcp-logs/outlook/ (args redacted, outputs captured).
"""
import contextlib
import json
import os

from mcp.server.fastmcp import FastMCP

from outlook_mcp import audit, calendar, graph, idle_refresh, mail, onedrive, tasks

DEFAULT_HTTP_PORT = 8012
_S = "outlook"


@contextlib.asynccontextmanager
async def _lifespan(server: FastMCP):
    """Idle-refresh loop alongside the server (Task 6c)."""
    task = idle_refresh.start_idle_refresh(
        graph.refresh_credentials, graph.TOKEN_FILE)
    try:
        yield
    finally:
        task.cancel()


mcp = FastMCP("outlook", lifespan=_lifespan)


@mcp.tool()
@audit.log_call(_S, "mail_list")
def mail_list(top: int = 10) -> str:
    """List recent inbox messages. Read-only."""
    return json.dumps(mail.list_messages(top=top))


@mcp.tool()
@audit.log_call(_S, "mail_search")
def mail_search(query: str, top: int = 10) -> str:
    """Search mailbox messages (Graph $search KQL). Read-only."""
    return json.dumps(mail.search_messages(query, top=top))


@mcp.tool()
@audit.log_call(_S, "mail_read")
def mail_read(message_id: str) -> str:
    """Read one message by id. Read-only."""
    return json.dumps(mail.get_message(message_id))


@mcp.tool()
@audit.log_call(_S, "mail_send")
def mail_send(to: str, subject: str, body_html: str) -> str:
    """Send an email (HTML body). WRITE operation."""
    return json.dumps(mail.send_message(to, subject, body_html))


@mcp.tool()
@audit.log_call(_S, "calendar_list")
def calendar_list() -> str:
    """List available calendars. Read-only."""
    return json.dumps(calendar.list_calendars())


@mcp.tool()
@audit.log_call(_S, "calendar_list_events")
def calendar_list_events(calendar_id: str = "primary", top: int = 10) -> str:
    """List calendar events. Read-only."""
    return json.dumps(calendar.list_events(calendar_id, top))


@mcp.tool()
@audit.log_call(_S, "calendar_create_event")
def calendar_create_event(calendar_id: str, subject: str, start: str, end: str,
                          body_html: str = "", online: bool = False) -> str:
    """Create a calendar event (ISO datetimes, UTC). WRITE operation."""
    return json.dumps(calendar.create_event(calendar_id, subject, start, end,
                                            body_html, online))


@mcp.tool()
@audit.log_call(_S, "calendar_delete_event")
def calendar_delete_event(calendar_id: str, event_id: str) -> str:
    """Delete a calendar event. DESTRUCTIVE operation."""
    return json.dumps(calendar.delete_event(calendar_id, event_id))


@mcp.tool()
@audit.log_call(_S, "tasks_list_lists")
def tasks_list_lists() -> str:
    """List To-Do task lists. Read-only."""
    return json.dumps(tasks.list_task_lists())


@mcp.tool()
@audit.log_call(_S, "tasks_list")
def tasks_list(list_id: str, completed: bool | None = None) -> str:
    """List tasks in a To-Do list. completed=None lists all. Read-only."""
    return json.dumps(tasks.list_tasks(list_id, completed))


@mcp.tool()
@audit.log_call(_S, "tasks_create")
def tasks_create(list_id: str, subject: str, due: str = "",
                 importance: str = "normal") -> str:
    """Create a To-Do task. WRITE operation."""
    return json.dumps(tasks.create_task(list_id, subject, due or None, importance))


@mcp.tool()
@audit.log_call(_S, "tasks_update")
def tasks_update(list_id: str, task_id: str, subject: str = "", due: str = "",
                 importance: str = "", completed: bool | None = None) -> str:
    """Update a To-Do task (only provided fields). WRITE operation."""
    return json.dumps(tasks.update_task(
        list_id, task_id, subject=subject or None, due=due or None,
        importance=importance or None, completed=completed))


@mcp.tool()
@audit.log_call(_S, "tasks_delete")
def tasks_delete(list_id: str, task_id: str) -> str:
    """Delete a To-Do task. DESTRUCTIVE operation."""
    return json.dumps(tasks.delete_task(list_id, task_id))


@mcp.tool()
@audit.log_call(_S, "onedrive_upload")
def onedrive_upload(local_path: str, name: str = "") -> str:
    """Upload a file to OneDrive root (content-type auto-detected). WRITE operation."""
    return json.dumps(onedrive.upload(local_path, name or None))


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
