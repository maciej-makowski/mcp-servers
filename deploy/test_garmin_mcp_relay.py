#!/usr/bin/env python3
"""Local stub-server test for garmin-mcp-relay.py: exercises the full
handshake + tools/call against a canned streamable-http server, including the
SSE response shape and the session id flow. Run: python3 this file."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "garmin-mcp-relay.py")

calls = []

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
            args = body["params"]["arguments"]
            payload = {"jsonrpc": "2.0", "id": n,
                       "result": {"content": [{"type": "text", "text": json.dumps(
                           {"action": "uploaded", "name": "garmin-archive-2026-11-19.age",
                            "size": 123456})}], "isError": False}}
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
args = json.dumps({"action": "upload",
                   "params": {"path": "/srv/mcp_files/send/x.age",
                              "dir": "/garmin-backups", "if_exists": "replace"}})
r = subprocess.run([sys.executable, RELAY, args], capture_output=True, text=True, env=env)
out = r.stdout.strip()
print("rc:", r.returncode)
print("stdout:", out)
print("stderr:", r.stderr.strip())
assert r.returncode == 0, r.stderr
assert calls == ["initialize", "notifications/initialized", "tools/call"], calls
payload = json.loads(out)
assert payload["name"] == "garmin-archive-2026-11-19.age", payload
assert payload["size"] == 123456, payload

# missing-bearer path: exit 2, no transport call
calls.clear()
env2 = dict(os.environ, MCP_RELAY_BEARER="", MCP_RELAY_URL="http://127.0.0.1:18010/mcp")
r2 = subprocess.run([sys.executable, RELAY, args], capture_output=True, text=True, env=env2)
assert r2.returncode == 2, r2.returncode
assert calls == [], calls
print("STUB TEST: all assertions PASS")
