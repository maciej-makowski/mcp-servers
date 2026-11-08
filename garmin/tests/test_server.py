"""Server wiring: the seven registered tools, bearer gate, run-mode config."""
import asyncio
import json
import runpy
import sys
from pathlib import Path

import pytest

GARMIN_DIR = Path(__file__).resolve().parent.parent


def test_seven_tools_registered():
    import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "garmin_recent_activities",
        "garmin_activity",
        "garmin_activity_file",
        "garmin_daily",
        "garmin_trend",
        "garmin_workout_usage",
        "garmin_api_status",
    }


def test_daily_tool_schema():
    import server

    tools = asyncio.run(server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert by_name["garmin_daily"].inputSchema["required"] == ["start_date", "end_date"]
    assert by_name["garmin_activity"].inputSchema["required"] == ["activity_id"]
    assert by_name["garmin_api_status"].inputSchema.get("required", []) == []


def test_bearer_middleware_401_missing_wrong_and_passthrough(monkeypatch):
    from mcp_auth import require_bearer

    monkeypatch.setenv("MCP_BEARER", "sekrit")
    served = []

    async def app(scope, receive, send):
        served.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = require_bearer("MCP_BEARER")(app)

    async def request(auth_header):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            sent.append(msg)

        headers = [] if auth_header is None else [(b"authorization", auth_header)]
        await wrapped(
            {"type": "http", "method": "GET", "path": "/mcp", "headers": headers},
            receive,
            send,
        )
        return sent[0]["status"]

    assert asyncio.run(request(None)) == 401                # missing bearer
    assert asyncio.run(request(b"Bearer wrong")) == 401     # wrong bearer
    assert asyncio.run(request(b"bearer sekrit")) == 204    # valid, scheme case-insensitive
    assert asyncio.run(request(b"Basic sekrit")) == 401     # wrong scheme
    assert len(served) == 1                                 # only the valid one reached the app


def test_require_bearer_fail_closed(monkeypatch):
    from mcp_auth import require_bearer

    monkeypatch.delenv("MCP_BEARER", raising=False)
    with pytest.raises(RuntimeError):
        require_bearer("MCP_BEARER")


def test_resolve_run_mode_stdio_default(monkeypatch):
    monkeypatch.delenv("MCP_BEARER", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)
    import server

    assert server._resolve_run_mode() == ("stdio",)


def test_resolve_run_mode_http_requires_bearer(monkeypatch):
    monkeypatch.delenv("MCP_BEARER", raising=False)
    monkeypatch.setenv("MCP_PORT", "8016")
    import server

    with pytest.raises(RuntimeError):
        server._resolve_run_mode()


def test_resolve_run_mode_port_8016(monkeypatch):
    monkeypatch.setenv("MCP_BEARER", "x")
    monkeypatch.setenv("MCP_PORT", "8016")
    monkeypatch.delenv("MCP_HOST", raising=False)
    import server

    assert server._resolve_run_mode() == ("http", "127.0.0.1", 8016)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    assert server._resolve_run_mode() == ("http", "0.0.0.0", 8016)
    monkeypatch.setenv("MCP_PORT", "9999")
    assert server._resolve_run_mode() == ("http", "0.0.0.0", 9999)


def test_main_wires_bearer_middleware_and_uvicorn(monkeypatch):
    """The __main__ block wraps the app in require_bearer and serves host/port."""
    monkeypatch.setenv("MCP_BEARER", "x")
    monkeypatch.setenv("MCP_PORT", "8016")
    monkeypatch.delenv("MCP_HOST", raising=False)
    if str(GARMIN_DIR) not in sys.path:
        sys.path.insert(0, str(GARMIN_DIR))

    captured = {}

    def fake_require_bearer(env_var):
        assert env_var == "MCP_BEARER"
        return lambda app: captured.setdefault("wrapped", app)

    def fake_uvicorn_run(app, host, port):
        captured["host"] = host
        captured["port"] = port

    import mcp_auth
    import uvicorn

    monkeypatch.setattr(mcp_auth, "require_bearer", fake_require_bearer)
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)

    runpy.run_path(str(GARMIN_DIR / "server.py"), run_name="__main__")
    assert "wrapped" in captured  # require_bearer wrapped the streamable app
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8016
