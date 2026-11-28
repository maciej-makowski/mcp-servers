#!/usr/bin/env python3
"""garmin-mcp-relay.py — loopback MCP relay for the garmin archive scripts.

Drives the proton MCP server (127.0.0.1:8010/mcp) host-side: the MCP
handshake (initialize -> notifications/initialized), then one tools/call to
proton_drive. Implements garmin-local-archive-design-final.md Rev 9 M9 (the
hermes-side proton CLI was offboarded 2026-11-05; root scripts reach Proton
through the MCP server, never through a CLI).

The bearer arrives as MCP_RELAY_BEARER in the environment (the caller reads
it from the proton env file as root); the helper never echoes it.

Gated actions (upload, trash) MUST carry confirm=true as a top-level key of
the arguments object (sibling of action/params) — the server's gate runs the
CLI with --dry-run otherwise and moves nothing; this helper refuses that
shape loudly before any transport.

Usage: garmin-mcp-relay.py '<proton_drive arguments as JSON>'
       e.g. '{"action": "upload", "params": {"path": "/srv/mcp_files/send/x.age",
             "dir": "/garmin-backups", "if_exists": "replace"}}'

stdout: the tool result's payload (the proton_drive JSON) — the
structuredContent envelope when the server carries it there, else the
content text.
Exit 0 on success; 1 on a tool-level error envelope; 2 on transport, auth,
or handshake failure; 2 also when a gated action (upload, trash) arrives
without confirm=true (the server would dry-run and move nothing — refused
here so a silent no-op can never pass as success). stderr carries the
diagnostics.

MCP_RELAY_URL overrides the endpoint (tests point it at a stub; production
leaves it unset for the loopback default).
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("MCP_RELAY_URL", "http://127.0.0.1:8010/mcp")

GATED_ACTIONS = {"upload", "trash"}


def _post(payload, session=None, bearer=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=1800) as resp:
        body = resp.read().decode("utf-8", "replace")
        sid = resp.headers.get("mcp-session-id")
        return sid, body


def _payload(body):
    """streamable-http answers application/json or an SSE stream; both carry
    the JSON-RPC envelope (SSE lines look like 'data: {...}')."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                try:
                    return json.loads(chunk)
                except ValueError:
                    continue
    raise ValueError("no JSON payload in the relay response")


def main():
    bearer = os.environ.get("MCP_RELAY_BEARER", "").strip()
    if not bearer:
        print("relay: MCP_RELAY_BEARER not set", file=sys.stderr)
        return 2
    try:
        arguments = json.loads(sys.argv[1])
    except IndexError:
        print("relay: missing arguments (one JSON argv expected)", file=sys.stderr)
        return 1
    except ValueError as exc:
        print("relay: arguments are not JSON: %s" % exc, file=sys.stderr)
        return 1
    # The proton_drive tool gates its mutating actions: without confirm=true
    # it runs the CLI with --dry-run and returns a preview envelope, moving
    # nothing. A silent dry-run would strand a nightly backup behind an "ok"
    # relay exit, so the refusal here is loud and structural (the 2026-11-20
    # to 22 uploads stranded exactly this way: rc 0, three dry-runs).
    action = arguments.get("action")
    if action in GATED_ACTIONS and arguments.get("confirm") is not True:
        print("relay: gated action %r needs confirm=true "
              "(without it the server dry-runs and moves nothing)" % action,
              file=sys.stderr)
        return 2

    try:
        _sid, body = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "garmin-archive-relay", "version": "1"}},
        }, bearer=bearer)
        init = _payload(body)
        session = init.get("result", {}).get("_meta", {}).get("mcp.sessionId") \
            if isinstance(init, dict) else None
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"},
              session=session, bearer=bearer)
        _sid2, body = _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "proton_drive", "arguments": arguments},
        }, session=session, bearer=bearer)
        resp = _payload(body)
    except Exception as exc:  # transport, auth, handshake, parse
        print("relay: transport failure: %s" % exc, file=sys.stderr)
        return 2

    if isinstance(resp, dict) and resp.get("error"):
        print(json.dumps(resp["error"], indent=1), file=sys.stderr)
        return 2
    result = resp.get("result") or {}
    # The server may carry the envelope in structuredContent with an EMPTY
    # content list (the gated dry-run path does exactly that): prefer
    # structuredContent, fall back to the content text. Printing neither
    # leaves the caller's verification walking a void (the 2026-11-20 to 22
    # uploads failed this way on top of the missing confirm).
    structured = result.get("structuredContent")
    if structured is not None:
        print(json.dumps(structured, indent=1))
        return 0
    if result.get("isError"):
        text = "\n".join(c.get("text", "") for c in result.get("content", [])
                         if isinstance(c, dict))
        print(text or json.dumps(result, indent=1), file=sys.stderr)
        return 1
    for c in result.get("content", []):
        if isinstance(c, dict) and c.get("type") == "text":
            print(c.get("text", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
