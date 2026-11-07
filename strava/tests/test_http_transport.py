"""HTTP transport tests: run-mode resolution, bearer middleware, end-to-end."""

import asyncio

import pytest

from strava_mcp.server import (
    DEFAULT_HTTP_PORT,
    _resolve_run_mode,
    make_http_asgi,
    make_server,
)


class TestResolveRunMode:
    def test_stdio_default(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER", raising=False)
        monkeypatch.delenv("MCP_PORT", raising=False)
        assert _resolve_run_mode() == ("stdio",)

    def test_bearer_selects_http_with_default_port(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "tok")
        monkeypatch.delenv("MCP_PORT", raising=False)
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        assert _resolve_run_mode() == ("http", "0.0.0.0", DEFAULT_HTTP_PORT)

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "tok")
        monkeypatch.setenv("MCP_PORT", "8123")
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        assert _resolve_run_mode() == ("http", "127.0.0.1", 8123)

    def test_port_alone_requires_bearer(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER", raising=False)
        monkeypatch.setenv("MCP_PORT", "8123")
        with pytest.raises(RuntimeError, match="MCP_BEARER"):
            _resolve_run_mode()

    def test_bad_port_raises(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "tok")
        monkeypatch.setenv("MCP_PORT", "not-a-port")
        with pytest.raises(ValueError):
            _resolve_run_mode()


class TestBearerMiddleware:
    """The shared middleware from _shared/mcp_auth (fail-closed)."""

    @pytest.fixture()
    def call(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "secret-token")
        from mcp_auth import require_bearer

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        app = require_bearer("MCP_BEARER")(downstream)

        async def call(authorization=None):
            headers = [(b"content-type", b"application/json")]
            if authorization is not None:
                headers.append((b"authorization", authorization.encode()))
            seen = []

            async def send(msg):
                seen.append(msg)

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            await app(
                {"type": "http", "method": "POST", "path": "/mcp", "headers": headers},
                receive,
                send,
            )
            return seen[0]["status"]

        return call

    def test_valid_bearer_passes(self, call):
        assert asyncio.run(call("Bearer secret-token")) == 200

    def test_wrong_token_rejected_401(self, call):
        assert asyncio.run(call("Bearer wrong")) == 401

    def test_missing_header_rejected_401(self, call):
        assert asyncio.run(call(None)) == 401

    def test_wrong_scheme_rejected_401(self, call):
        assert asyncio.run(call("Basic secret-token")) == 401


class TestHttpAppEndToEnd:
    """Full request through the real ASGI app (middleware + session manager)."""

    @pytest.fixture()
    def client_fx(self, monkeypatch):
        from starlette.testclient import TestClient

        monkeypatch.setenv("MCP_BEARER", "test-bearer")
        app = make_http_asgi(make_server())
        with TestClient(app) as c:
            yield c

    def test_init_without_token_401(self, client_fx):
        r = client_fx.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 401

    def test_init_with_token_succeeds(self, client_fx):
        r = client_fx.post(
            "/mcp",
            headers={"Authorization": "Bearer test-bearer"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert r.status_code == 200
        assert r.json()["result"]["serverInfo"]["name"] == "strava-mcp"

    def test_tool_list_shows_three_tools(self, client_fx):
        r = client_fx.post(
            "/mcp",
            headers={"Authorization": "Bearer test-bearer"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert r.status_code == 200
        names = [t["name"] for t in r.json()["result"]["tools"]]
        assert names == ["strava_athlete", "strava_activities", "strava_rate_status"]
