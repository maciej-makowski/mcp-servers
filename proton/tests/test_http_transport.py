"""Tests for the HTTP transport (streamable-http) + bearer-token auth."""

import asyncio
import os

import pytest

from proton_mcp.server import (
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    BearerAuthMiddleware,
    make_http_asgi,
    make_server,
    resolve_http_config,
)


class TestResolveHttpConfig:
    def test_stdio_default_when_no_http_env(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER", raising=False)
        monkeypatch.delenv("MCP_PORT", raising=False)
        monkeypatch.delenv("MCP_HOST", raising=False)
        assert resolve_http_config() is None

    def test_bearer_selects_http_with_defaults(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "tok")
        monkeypatch.delenv("MCP_PORT", raising=False)
        monkeypatch.delenv("MCP_HOST", raising=False)
        cfg = resolve_http_config()
        assert cfg == {"host": DEFAULT_HTTP_HOST, "port": DEFAULT_HTTP_PORT, "bearer": "tok"}

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "tok")
        monkeypatch.setenv("MCP_PORT", "8123")
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        cfg = resolve_http_config()
        assert cfg == {"host": "127.0.0.1", "port": 8123, "bearer": "tok"}

    def test_port_alone_requires_bearer(self, monkeypatch):
        monkeypatch.delenv("MCP_BEARER", raising=False)
        monkeypatch.setenv("MCP_PORT", "8123")
        with pytest.raises(ValueError, match="MCP_BEARER"):
            resolve_http_config()

    def test_bad_port_raises(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER", "tok")
        monkeypatch.setenv("MCP_PORT", "not-a-port")
        with pytest.raises(ValueError, match="MCP_PORT"):
            resolve_http_config()


class TestBearerAuthMiddleware:
    @pytest.fixture()
    def middleware(self):
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"ok": true}',
                    "more_body": False,
                }
            )

        async def send(msg):
            pass

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [],
        }

        async def call(authorization=None):
            headers = [(b"content-type", b"application/json")]
            if authorization is not None:
                headers.append((b"authorization", authorization.encode()))
            seen = []

            async def send(msg):
                seen.append(msg)

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            m = BearerAuthMiddleware(downstream, "secret-token")
            await m(
                {**scope, "headers": headers},
                receive,
                send,
            )
            return seen[0]["status"]

        return call

    def test_valid_bearer_passes(self, middleware):
        assert asyncio.run(middleware("Bearer secret-token")) == 200

    def test_wrong_token_rejected_401(self, middleware):
        assert asyncio.run(middleware("Bearer wrong")) == 401

    def test_missing_header_rejected_401(self, middleware):
        assert asyncio.run(middleware(None)) == 401

    def test_wrong_scheme_rejected_401(self, middleware):
        assert asyncio.run(middleware("Basic secret-token")) == 401


class TestHttpAppEndToEnd:
    """Full request through the real ASGI app (middleware + session manager)."""

    @pytest.fixture()
    def client(self):
        from starlette.testclient import TestClient

        app = make_http_asgi(make_server(), "test-bearer")
        with TestClient(app) as c:
            yield c

    def test_init_without_token_401(self, client):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 401

    def test_init_with_token_succeeds(self, client):
        r = client.post(
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
        assert r.json()["result"]["serverInfo"]["name"] == "proton-mcp"
