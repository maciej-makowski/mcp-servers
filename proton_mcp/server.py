"""MCP stdio server exposing Proton Mail, Calendar and Drive as 3 tools.

Built on the `mcp` SDK's low-level `Server` (v2 API — FastMCP was renamed to
MCPServer and the v1 decorator API is gone; the low-level Server with
on_list_tools / on_call_tool handlers is the version-stable surface).

Schemas are built from the tool layer's own action tables, so reads and gated
(mutating) actions can never drift apart. Gated tools carry an explicit
`confirm` boolean (default false, mirroring the tool layer's gating):
confirm=false -> the CLI runs with --dry-run and returns a preview envelope;
confirm=true -> the action runs for real.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import mcp_types as types
from mcp.server import lowlevel
from mcp.server.stdio import stdio_server

from . import tools

SERVER_NAME = "proton-mcp"

GATING_DOC = (
    "Read actions run directly. Gated (mutating) actions are double-keyed: "
    "call with confirm=false (default) first — the CLI runs with --dry-run and "
    "returns a preview envelope {applied:false, dry_run:true, preview, "
    "next_step}; show the preview to the user, and only re-call with "
    "confirm=true after explicit approval to run it for real."
)

# Action tables: gated lists are DERIVED from the tool layer's GATED_ACTIONS
# so they can never drift apart from the real gating; reads stay declarative.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "proton_mail": {
        "tool": tools.MAIL_TOOL,
        "description": (
            "Proton Mail via proton-cli: reads status/list/search/get, gated "
            "send/move/label/unlabel/mark/trash (bulk-capable via filter). "
            "Note: status is a count per folder and the CLI's count verb maps "
            "only the filter's folder key — other filter keys apply to "
            "list/search, not status. "
            + GATING_DOC
        ),
        "reads": ("status", "list", "search", "get"),
        "gated": tuple(sorted(tools.MAIL_TOOL.GATED_ACTIONS)),
        "filter": True,
        "extra_params": {
            "query": {"type": "string", "description": "Search query (search action)"},
            "id": {"type": "string", "description": "Message id (get action)"},
            "to": {"type": "string", "description": "Recipient address (send)"},
            "subject": {"type": "string", "description": "Subject line (send)"},
            "body": {"type": "string", "description": "Body text, piped over stdin (send)"},
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Explicit message ids for a bulk gated action (exclusive with filter)",
            },
            "into": {"type": "string", "description": "Destination folder (move)"},
            "label": {"type": "string", "description": "Label name (label/unlabel)"},
            "read": {"type": "boolean", "description": "mark: set read (mark only, true required)"},
            "unread": {"type": "boolean", "description": "mark: set unread (mark only, true required)"},
        },
    },
    "proton_calendar": {
        "tool": tools.CALENDAR_TOOL,
        "description": (
            "Proton Calendar via proton-cli: reads calendars/events_list/"
            "events_get, gated events_create/events_delete. " + GATING_DOC
        ),
        "reads": ("calendars", "events_list", "events_get"),
        "gated": tuple(sorted(tools.CALENDAR_TOOL.GATED_ACTIONS)),
        "filter": False,
        "extra_params": {
            "id": {"type": "string", "description": "Event reference (events_get/events_delete)"},
            "title": {"type": "string", "description": "Event title (events_create)"},
            "start": {"type": "string", "description": "Start, e.g. 2026-04-16T14:00 (events_create)"},
            "end": {"type": "string", "description": "End, e.g. 2026-04-16T15:00 (events_create)"},
            "description": {"type": "string", "description": "Event description (events_create)"},
            "calendar": {"type": "string", "description": "Calendar by name or ID (events_list)"},
            "after": {"type": "string", "description": "First day to include, YYYY-MM-DD (events_list)"},
            "before": {"type": "string", "description": "Last day to include, YYYY-MM-DD (events_list)"},
            "keyword": {"type": "string", "description": "Text match in title/location/description (events_list)"},
            "limit": {"type": "integer", "description": "Per-page count; 0 for all (events_list/calendars)"},
            "page": {"type": "integer", "description": "Page number, from zero (events_list/calendars)"},
        },
    },
    "proton_drive": {
        "tool": tools.DRIVE_TOOL,
        "description": (
            "Proton Drive via proton-cli: reads list/get/download, gated "
            "upload/trash. " + GATING_DOC
        ),
        "reads": ("list", "get", "download"),
        "gated": tuple(sorted(tools.DRIVE_TOOL.GATED_ACTIONS)),
        "filter": False,
        "extra_params": {
            "id": {"type": "string", "description": "Item id (get)"},
            "dir": {"type": "string", "description": "Drive directory (list) or destination dir (upload)"},
            "path": {"type": "string", "description": "Drive item path (download) or local file (upload)"},
            "dest_dir": {"type": "string", "description": "Local directory to download into"},
            "recursive": {"type": "boolean", "description": "Download directories recursively"},
            "force": {"type": "boolean", "description": "Overwrite existing local files"},
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Item ids to trash",
            },
        },
    },
    "proton_photos": {
        "tool": tools.PHOTOS_TOOL,
        "description": (
            "Proton Photos via proton-cli: reads list/albums/download, gated "
            "trash/delete/favorite/unfavorite. NOTE: photos have NO filename — "
            "address them by link_id (from list results) or by capture_time via a "
            "list query; there is no per-photo metadata verb. download is a read "
            "but writes locally — only into a caller-named dest_dir. delete is "
            "PERMANENT (photos are not recoverable from trash). " + GATING_DOC
        ),
        "reads": ("list", "albums", "download"),
        "gated": tuple(sorted(tools.PHOTOS_TOOL.GATED_ACTIONS)),
        "filter": False,
        "extra_params": {
            "album": {"type": "string", "description": "Album name or ID (list)"},
            "tag": {
                "type": "string",
                "description": (
                    "Tag filter (list): favorites, screenshots, videos, "
                    "live-photos, motion-photos, selfies, portraits, bursts, "
                    "panoramas, raw"
                ),
            },
            "limit": {"type": "integer", "description": "Per-page count; 0 for all (list/albums)"},
            "page": {"type": "integer", "description": "Page number, from zero (list/albums)"},
            "sort": {
                "type": "string",
                "description": "Order by: captured (list, default) or name/photos (albums)",
            },
            "desc": {"type": "boolean", "description": "Reverse the order (list/albums)"},
            "ref": {
                "type": "string",
                "description": "Photo link_id (download; single-photo form of refs)",
            },
            "dest_dir": {
                "type": "string",
                "description": "Local directory to download into (required for download)",
            },
            "force": {"type": "boolean", "description": "Overwrite existing local files (download)"},
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Photo link_ids for a bulk gated action (exclusive with ref)",
            },
        },
    },
}

# Filter object schema: mirrors the mail filter keys exactly (13 value flags
# + booleans + the bulk rails' limit/all).
_FILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Filter scope for list/search/bulk gated actions. Booleans are bare "
        "switches (truthy only). Bulk gated actions are always limited: "
        "default --limit 150 unless an integer limit or all=true (all also "
        "needs confirm=true)."
    ),
    "properties": {
        "folder": {"type": "string"},
        "from": {"type": "string"},
        "to": {"type": "string"},
        "subject": {"type": "string"},
        "keyword": {"type": "string"},
        "after": {"type": "string"},
        "before": {"type": "string"},
        "older_than": {"type": "string"},
        "newer_than": {"type": "string"},
        "has_attachments": {"type": "boolean"},
        "read": {"type": "boolean"},
        "unread": {"type": "boolean"},
        "starred": {"type": "boolean"},
        "limit": {"type": "integer", "description": "Bulk rail cap; default 150 unless given"},
        "all": {
            "type": "boolean",
            "description": "Explicit unbounded bulk run; also requires confirm=true",
        },
    },
}


def build_input_schema(spec_name: str) -> dict[str, Any]:
    """Build the JSON schema for one registered tool from its action tables."""
    spec = TOOL_SPECS[spec_name]
    props: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": [*spec["reads"], *spec["gated"]],
            "description": "One of the actions listed in the enum; reads and gated actions are separate.",
        },
        "params": {
            "type": "object",
            "additionalProperties": True,
            "description": "Action-specific parameters (see each action's needs).",
            "properties": (
                {"filter": _FILTER_SCHEMA, **spec["extra_params"]}
                if spec["filter"]
                else spec["extra_params"]
            ),
        },
    }
    if spec["gated"]:
        # confirm lives ONLY on gated tools' schemas and defaults to false.
        props["confirm"] = {
            "type": "boolean",
            "default": False,
            "description": (
                "false (default): gated actions run as a --dry-run preview, nothing changes. "
                "true: run for real — only after the user explicitly approved the preview."
            ),
        }
    return {"type": "object", "properties": props, "required": ["action"]}


INPUT_SCHEMAS = {name: build_input_schema(name) for name in TOOL_SPECS}


async def call_tool_impl(name: str, arguments: dict[str, Any]) -> dict:
    """Async MCP entry point: dispatch off the event loop via to_thread."""
    return await asyncio.to_thread(call_tool_sync, name, arguments)


def call_tool_sync(name: str, arguments: dict[str, Any]) -> dict:
    """Dispatch an MCP tool call to the tool layer (testable without stdio).

    Runs synchronously so the blocking subprocess call never blocks the
    event loop; call_tool_impl awaits it via asyncio.to_thread.
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
    if "confirm" in INPUT_SCHEMAS[name]["properties"]:
        # confirm is a top-level tool argument; the tool layer expects it
        # merged into params. Precedence: an explicit params.confirm from the
        # client wins over the top-level argument (fail-safe: absence -> false,
        # which is the dry-run default either way).
        params.setdefault("confirm", bool(arguments.get("confirm", False)))
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
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Entry point: serve the three tools over MCP stdio until stdin EOF."""
    # Update-check off by default for this server's CLI subprocesses (the CLI's
    # self-update path cannot be verified offline; updates are manual). An env
    # var set in the MCP config would also work, but Hermes config env values
    # round-trip as YAML ints, which the stdio schema rejects.
    os.environ.setdefault("PROTON_NO_UPDATE_CHECK", "1")
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
