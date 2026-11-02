#!/usr/bin/env python3
"""Read-only probe of the live google MCP server on 127.0.0.1:8011.

Reads the bearer from ~/.hermes/.env at runtime; never prints or echoes it.
Sequence: initialize -> notifications/initialized -> tools/list -> one tiny
read-only sheets_get (A1:C3) to prove the Google-side token works today.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8011/mcp"
HDRS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# --- bearer: read at runtime, never echoed ---
bearer = None
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        m = re.match(r"^GOOGLE_MCP_BEARER=(.*)$", line)
        if m:
            bearer = m.group(1).strip().strip('"').strip("'")
            break
if not bearer:
    print("FATAL: GOOGLE_MCP_BEARER not found in ~/.hermes/.env")
    sys.exit(1)

def post(body: dict, session: str | None, auth=bearer):
    headers = dict(HDRS)
    headers["Authorization"] = "Bearer " + auth
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()

def parse_sse(text: str):
    """Extract the first JSON-RPC result from an SSE stream (or plain JSON)."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"^data: (.*)$", block, re.M)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None

# 1) initialize
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "design-probe", "version": "0"}}}
st, hdrs, body = post(init, None)
session = hdrs.get("mcp-session-id")
res = parse_sse(body)
info = (res or {}).get("result", {}).get("serverInfo", {})
print(f"initialize: status={st} session_header={'yes' if session else 'no'} "
      f"protocol={(res or {}).get('result', {}).get('protocolVersion')} "
      f"server={info.get('name')}/{info.get('version')}")
if st != 200 or not session:
    print("body(head):", body[:300])
    sys.exit(2)

# 2) notifications/initialized
st2, _, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                 session)
print(f"initialized notification: status={st2}")

# 3) tools/list
st3, _, body3 = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                      "params": {}}, session)
res3 = parse_sse(body3)
tools = (res3 or {}).get("result", {}).get("tools", [])
print(f"tools/list: status={st3} count={len(tools)}")
for t in tools:
    schema = t.get("inputSchema", {})
    props = list((schema.get("properties") or {}).keys())
    req = schema.get("required", [])
    print(f"  - {t['name']}({', '.join(props)}) required={req}")

# 4) wrong bearer -> expect 401
st4, _, _ = post(init, None, auth="definitely-wrong-token")
print(f"wrong bearer initialize: status={st4}")

# 5) one tiny READ-ONLY sheets_get (A1:C3) against the live sheet
SHEET_ID = "1uelXnm7OQodI0XOAFVrMiUu659ykFlshm8wsDyYHHxw"
st5, _, body5 = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "sheets_get", "arguments": {
                          "sheet_id": SHEET_ID, "range_name": "Sheet1!A1:C3"}}},
                     session)
res5 = parse_sse(body5)
inner = res5.get("result") if res5 else None
if inner and inner.get("content"):
    txt = inner["content"][0].get("text", "")
    rows = json.loads(txt)
    print(f"sheets_get Sheet1!A1:C3: status={st5} isError={inner.get('isError')} "
          f"rows={len(rows)} first_row={rows[0] if rows else None}")
else:
    print(f"sheets_get: status={st5} raw(head)={body5[:300]}")
