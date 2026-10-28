"""HTTP transport + idle-refresh tests for the outlook MCP server.

Mirrors google/test_http_idle.py. Covers: http config resolution (stdio
default / HTTP via env / default port 8012), bearer-auth end-to-end through
the streamable-http ASGI app, and the idle-refresh age gate (fresh store ->
no refresh, stale -> exactly one refresh, failing refresh_fn -> no crash).
"""
import asyncio
import json
import os
import time

import pytest


def _run_gate(fn, token_path, max_age_s=7 * 86400, interval_s=0.01, run_s=0.05):
    """Run the idle-refresh gate inside an event loop for `run_s` seconds."""
    from outlook_mcp import idle_refresh

    async def main():
        task = idle_refresh.start_idle_refresh(
            fn, token_path, max_age_s=max_age_s, interval_s=interval_s)
        await asyncio.sleep(run_s)
        task.cancel()

    asyncio.run(main())


# --- http config resolution ------------------------------------------------

def test_resolve_http_config_defaults_loopback_and_port():
    from mcp_auth import resolve_http_config
    with pytest.MonkeyPatch.context() as mp:
        for var in ("MCP_HOST", "MCP_PORT"):
            mp.delenv(var, raising=False)
        assert resolve_http_config(8012) == ("127.0.0.1", 8012)


def test_resolve_http_config_env_override(monkeypatch):
    from mcp_auth import resolve_http_config
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9999")
    assert resolve_http_config(8012) == ("0.0.0.0", 9999)


# --- bearer auth through the real ASGI app ---------------------------------

@pytest.fixture(scope="module")
def http_client():
    """A live TestClient over the bearer-wrapped streamable-http app.

    Module-scoped: FastMCP's streamable-http session manager is a singleton
    that can only .run() once, so the client (which owns the lifespan) must
    be shared. Env is set/restored manually (monkeypatch is function-scoped).
    """
    import contextlib

    import server
    old_bearer = os.environ.get("MCP_BEARER")
    # The idle-refresh loop is NOT started by the Starlette app lifespan:
    # FastMCP's streamable_http_app lifespan only runs session_manager.run(),
    # while server._lifespan starts a REAL idle-refresh loop per MCP session
    # (at each initialize request, on the test portal loop). Stubbing
    # idle_refresh.start_idle_refresh around app startup therefore misses it:
    # on a CI runner with no token file the loop's to_thread(
    # refresh_credentials) worker can execute inside a LATER test, racing
    # that test's monkeypatched graph.TOKEN_FILE (CI: assert 2 == 1).
    # Kill it at the seam the SDK actually calls: the lowlevel Server's
    # per-session lifespan instance attribute (Server.run does
    # self.lifespan(self)), restored at module teardown so the _run_gate
    # tests below exercise the real factory directly.
    orig_lowlevel_lifespan = server.mcp._mcp_server.lifespan

    @contextlib.asynccontextmanager
    async def _no_lifespan(app):
        yield None

    server.mcp._mcp_server.lifespan = _no_lifespan
    os.environ["MCP_BEARER"] = "test-token-123"
    try:
        from mcp_auth import require_bearer
        from starlette.testclient import TestClient

        app = require_bearer("MCP_BEARER")(server.mcp.streamable_http_app())
        with TestClient(app, base_url="http://localhost:8012") as client:
            yield client
    finally:
        server.mcp._mcp_server.lifespan = orig_lowlevel_lifespan
        if old_bearer is None:
            os.environ.pop("MCP_BEARER", None)
        else:
            os.environ["MCP_BEARER"] = old_bearer


_INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def test_http_initialize_with_valid_bearer(http_client):
    headers = {**_HEADERS, "Authorization": "Bearer test-token-123"}
    resp = http_client.post("/mcp", json=_INIT_BODY, headers=headers)
    assert resp.status_code == 200


def test_http_initialize_missing_bearer_is_401(http_client):
    resp = http_client.post("/mcp", json=_INIT_BODY, headers=_HEADERS)
    assert resp.status_code == 401


def test_http_initialize_wrong_bearer_is_401(http_client):
    headers = {**_HEADERS, "Authorization": "Bearer nope"}
    resp = http_client.post("/mcp", json=_INIT_BODY, headers=headers)
    assert resp.status_code == 401


def test_http_mode_requires_bearer_env(monkeypatch):
    """HTTP requested (MCP_PORT set) without MCP_BEARER must fail closed."""
    import server
    monkeypatch.delenv("MCP_BEARER", raising=False)
    monkeypatch.setenv("MCP_PORT", "8012")
    with pytest.raises(RuntimeError):
        server._resolve_run_mode()


# --- idle refresh ------------------------------------------------------------

def test_idle_refresh_fresh_store_skips_refresh(tmp_path):
    """Token store younger than max_age -> refresh_fn never called."""
    token = tmp_path / "tokens.json"
    token.write_text("{}")
    os.utime(token, (time.time() - 100, time.time() - 100))  # 100s old
    calls = []
    _run_gate(lambda: calls.append(1), str(token))
    assert calls == []


def test_idle_refresh_stale_store_refreshes_once(tmp_path):
    """Token store older than max_age -> exactly one refresh_fn call."""
    token = tmp_path / "tokens.json"
    token.write_text("{}")
    old = time.time() - 8 * 86400
    os.utime(token, (old, old))
    calls = []

    def refresh_like_real():
        # mirrors the real refresh: rewrites the store -> mtime is now
        calls.append(1)
        os.utime(token, (time.time(), time.time()))

    _run_gate(refresh_like_real, str(token))
    assert calls == [1]


def test_idle_refresh_failing_refresh_fn_does_not_crash(tmp_path):
    """refresh_fn raising is logged, loop keeps running, retried next interval."""
    token = tmp_path / "tokens.json"
    token.write_text("{}")
    old = time.time() - 8 * 86400
    os.utime(token, (old, old))
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("refresh failed")

    _run_gate(boom, str(token), run_s=0.08)
    assert len(calls) >= 2  # failed, loop survived, retried


def test_idle_refresh_missing_store_attempts_refresh(tmp_path):
    """No token file at all -> treated as stale, refresh attempted (and fails
    inside refresh_fn without crashing the loop)."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("missing")

    _run_gate(boom, str(tmp_path / "nope.json"))
    assert len(calls) >= 1


# --- outlook token refresh entrypoint ----------------------------------------

def test_refresh_credentials_forces_flocked_refresh(tmp_path, monkeypatch):
    """refresh_credentials() drops the cached access_token and runs the
    flocked refresh path (requests.post called, store rewritten atomically)."""
    from outlook_mcp import graph

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "r",
        "token_url": "https://fake/token", "client_id": "c", "scope": "s",
        "expires_at": int(time.time()) + 9999,  # would short-circuit if kept
    }))
    monkeypatch.setattr(graph, "TOKEN_FILE", str(token_file))

    posted = []

    class FakeResp:
        ok = True

        def json(self):
            return {"access_token": "new", "refresh_token": "r2",
                    "expires_in": 3600}

    def fake_post(url, data=None, timeout=None):
        posted.append(url)
        assert data["refresh_token"] == "r"
        return FakeResp()

    monkeypatch.setattr(graph.requests, "post", fake_post)

    graph.refresh_credentials()

    assert len(posted) == 1
    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "new"
    assert saved["refresh_token"] == "r2"
    assert not list(tmp_path.glob("*.tmp*"))


def test_refresh_credentials_concurrent_writers_no_collision(
        tmp_path, monkeypatch):
    """Two concurrent refresh_credentials() calls (per-request + idle loop)
    must not share a tmp file — both complete and the store stays valid."""
    import concurrent.futures

    from outlook_mcp import graph

    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "r",
        "token_url": "https://fake/token", "client_id": "c", "scope": "s",
        "expires_at": 0,  # force refresh on each call
    }))
    monkeypatch.setattr(graph, "TOKEN_FILE", str(token_file))

    class FakeResp:
        ok = True

        def json(self):
            return {"access_token": "new", "refresh_token": "r2",
                    "expires_in": 3600}

    def fake_post(url, data=None, timeout=None):
        return FakeResp()

    monkeypatch.setattr(graph.requests, "post", fake_post)

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(graph.refresh_credentials) for _ in range(2)]
        for f in futures:
            f.result()  # raises if either writer collided/failed

    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "new"
    assert not list(tmp_path.glob("*.tmp*"))
