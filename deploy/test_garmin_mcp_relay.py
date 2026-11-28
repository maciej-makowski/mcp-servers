#!/usr/bin/env python3
"""Local stub-server test for garmin-mcp-relay.py: exercises the full
handshake + tools/call against a canned streamable-http server, covering the
session id flow, SSE parsing, both result shapes (structuredContent-empty and
content-text), the confirm verbatim pass-through, and the gated refusal.
Run: python3 this file."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "garmin-mcp-relay.py")

calls = []
received = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode())
        calls.append(body.get("method"))
        n = body.get("id")
        method = body.get("method", "")
        if method == "initialize":
            payload = {"jsonrpc": "2.0", "id": n,
                       "result": {"protocolVersion": "2025-03-26",
                                  "serverInfo": {"name": "stub", "version": "0"},
                                  "_meta": {"mcp.sessionId": "sess-123"}}}
            hdrs = {"mcp-session-id": "sess-123"}
            self._answer(payload, hdrs, sse=True)
        elif method == "tools/call":
            assert self.headers.get("mcp-session-id") == "sess-123", "session id missing"
            received.append(body["params"]["arguments"])
            inner = {"action": "uploaded", "name": "garmin-archive-2026-11-19.age",
                     "size": 123456}
            # shape 1 (first tools/call): structuredContent with an EMPTY
            # content list — the gated dry-run path's shape on the real server;
            # shape 2 (second): the classic content-text carry.
            if calls.count("tools/call") == 1:
                payload = {"jsonrpc": "2.0", "id": n,
                           "result": {"structuredContent": inner,
                                      "content": [], "isError": False}}
            else:
                payload = {"jsonrpc": "2.0", "id": n,
                           "result": {"content": [{"type": "text", "text": json.dumps(inner)}],
                                      "isError": False}}
            self._answer(payload, sse=True)
        else:  # notifications/initialized: 202 empty
            self.send_response(202)
            self.end_headers()

    def _answer(self, payload, hdrs=None, sse=False):
        text = json.dumps(payload)
        if sse:
            wire = "event: message\ndata: %s\n\n" % text
            ctype = "text/event-stream"
        else:
            wire = text
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in (hdrs or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(wire.encode())))
        self.end_headers()
        self.wfile.write(wire.encode())

srv = HTTPServer(("127.0.0.1", 18010), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

env = dict(os.environ, MCP_RELAY_BEARER="stub-bearer",
           MCP_RELAY_URL="http://127.0.0.1:18010/mcp")
CALLER_ARGS = {"action": "upload",
               "params": {"path": "/srv/mcp_files/send/x.age",
                          "dir": "/garmin-backups",
                          "if_exists": "replace"}}

# gated action without confirm: the helper refuses client-side (rc 2, zero
# transport calls) — the silent-dry-run class is structurally impossible
nogate = json.dumps(CALLER_ARGS)
r0 = subprocess.run([sys.executable, RELAY, nogate], capture_output=True,
                    text=True, env=env)
assert r0.returncode == 2, r0.returncode
assert "confirm" in r0.stderr, r0.stderr
assert calls == [], calls

# confirm-carrying call, shape 1: structuredContent with an empty content list
args = json.dumps(dict(CALLER_ARGS, confirm=True))
r = subprocess.run([sys.executable, RELAY, args], capture_output=True, text=True, env=env)
out = r.stdout.strip()
print("rc:", r.returncode)
print("stdout:", out)
print("stderr:", r.stderr.strip())
assert r.returncode == 0, r.stderr
payload = json.loads(out)
assert payload["name"] == "garmin-archive-2026-11-19.age", payload
assert payload["size"] == 123456, payload

# confirm-carrying call, shape 2: the classic content-text carry
r = subprocess.run([sys.executable, RELAY, args], capture_output=True, text=True, env=env)
out = r.stdout.strip()
assert r.returncode == 0, r.stderr
payload = json.loads(out)
assert payload["name"] == "garmin-archive-2026-11-19.age", payload
assert payload["size"] == 123456, payload

# the relay forwards the caller's arguments verbatim (confirm key intact)
assert len(received) == 2, received
for got in received:
    assert got == dict(CALLER_ARGS, confirm=True), got

print("STUB TEST: all assertions PASS")
