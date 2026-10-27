"""Tests for the shared mcp_auth shim (bearer middleware + HTTP config)."""
import os

import pytest


def _env(tmp_path, name="MCP_BEARER", token="s3cret"):
    """Run a block with a named env var set."""
    old = os.environ.get(name)
    os.environ[name] = token
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("MCP_BEARER", "s3cret")
    return "s3cret"


def make_call(mw, headers, scope_type="http"):
    """Drive the middleware with a fake scope; return (app_called, response_sent)."""
    import asyncio
    from mcp_auth import require_bearer

    app_called = []
    sent = {}

    async def app(scope, receive, send):
        app_called.append(scope)

    async def run():
        scope = {"type": scope_type, "headers": headers}
        scope = {**scope, "headers": [(k.lower(), v) for k, v in headers]}
        await mw(app)(scope, None, None)
        return app_called, sent

    return asyncio.get_event_loop_policy(), run  # placeholder, replaced below


def drive(mw, headers, scope_type="http"):
    import asyncio
    from mcp_auth import require_bearer

    app_called = []

    async def app(scope, receive, send):
        app_called.append(scope)

    async def recv():
        return {"type": "http.request"}

    sent = {}

    async def send(msg):
        sent.update(msg)

    scope = {"type": scope_type, "headers": [(k.lower(), v) for k, v in headers],
             "method": "POST", "path": "/"}
    asyncio.run(mw(app)(scope, recv, send))
    return app_called, sent


def _headers(token_env, scheme=b"Bearer", token=None):
    if token is None:
        token = token_env.encode()
    return [(b"authorization", scheme + b" " + token)] if token is not None else []


def test_valid_token_passes(token_env):
    from mcp_auth import require_bearer

    mw = require_bearer("MCP_BEARER")
    app_called, sent = drive(mw, _headers(token_env))
    assert len(app_called) == 1
    assert sent == {}


def test_wrong_token_401(token_env):
    from mcp_auth import require_bearer

    mw = require_bearer("MCP_BEARER")
    app_called, sent = drive(mw, _headers(token_env, token=b"wrong"))
    assert app_called == []
    assert sent["status"] == 401


def test_missing_header_401(token_env):
    from mcp_auth import require_bearer

    mw = require_bearer("MCP_BEARER")
    app_called, sent = drive(mw, [])
    assert app_called == []
    assert sent["status"] == 401


def test_scheme_case_insensitive_and_token_verbatim(monkeypatch):
    monkeypatch.setenv("MCP_BEARER", "ToKeN")
    from mcp_auth import require_bearer

    mw = require_bearer("MCP_BEARER")
    # lowercase scheme still accepted
    app_called, sent = drive(mw, [(b"authorization", b"bearer ToKeN")])
    assert len(app_called) == 1
    # token case is NOT normalized — mismatch
    app_called, sent = drive(mw, [(b"authorization", b"Bearer token")])
    assert app_called == [] and sent["status"] == 401


def test_non_http_scope_passes_through(token_env):
    from mcp_auth import require_bearer

    mw = require_bearer("MCP_BEARER")
    app_called, sent = drive(mw, [], scope_type="lifespan")
    assert len(app_called) == 1


def test_missing_env_fails_closed():
    from mcp_auth import require_bearer

    os.environ.pop("MCP_AUTH_TEST_UNSET", None)
    with pytest.raises(RuntimeError, match="MCP_AUTH_TEST_UNSET"):
        require_bearer("MCP_AUTH_TEST_UNSET")


def test_resolve_http_config_defaults(monkeypatch):
    from mcp_auth import resolve_http_config

    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.delenv("MCP_PORT", raising=False)
    assert resolve_http_config(8011) == ("127.0.0.1", 8011)


def test_resolve_http_config_env_overrides(monkeypatch):
    from mcp_auth import resolve_http_config

    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9999")
    assert resolve_http_config(8011) == ("0.0.0.0", 9999)
