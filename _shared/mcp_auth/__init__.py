"""Shared bearer-token auth + HTTP config helpers for the local MCP servers.

Used by the google / outlook / proton MCP servers as a uv path dependency:

    dependencies = ["mcp-auth", ...]
    [tool.uv.sources]
    mcp-auth = { path = "../_shared/mcp_auth", editable = true }

- ``require_bearer(env_var)`` returns a Starlette-compatible ASGI middleware
  (usable both as ``Middleware(require_bearer("MCP_BEARER"))`` and as direct
  ``auth = require_bearer("MCP_BEARER")(app)`` wrapping). Compares the
  ``Authorization`` header in constant time; 401 JSON on missing/mismatch;
  non-HTTP scopes (e.g. stdio/lifespan) pass through unchanged.
- ``resolve_http_config(default_port)`` resolves ``(host, port)`` from
  ``MCP_HOST`` (default ``127.0.0.1``) and ``MCP_PORT``.
"""
import hmac
import os
from typing import Optional

__all__ = ["require_bearer", "resolve_http_config"]


def require_bearer(env_var: str):
    """Build a bearer-token ASGI middleware that reads the token from ``env_var``.

    Raises at construction if the env var is unset (fail closed: the server
    never starts with auth silently disabled).
    """
    expected = os.environ.get(env_var)
    if expected is None:
        raise RuntimeError(f"require_bearer: environment variable {env_var!r} is not set")

    class BearerAuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            if not self._authorized(auth):
                resp = _unauthorized()
                return await resp(scope, receive, send)
            await self.app(scope, receive, send)

        def _authorized(self, auth: bytes) -> bool:
            scheme, _, token = auth.partition(b" ")
            # Scheme comparison is case-insensitive; the token itself is
            # compared verbatim, in constant time.
            if not hmac.compare_digest(scheme.lower(), b"bearer"):
                return False
            return hmac.compare_digest(token.strip(), expected.encode())

    return BearerAuthMiddleware


def _unauthorized():
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "unauthorized"}, status_code=401)


def resolve_http_config(default_port: int) -> tuple[str, int]:
    """Resolve ``(host, port)`` for `mcp.run(transport="http", ...)`.

    ``MCP_HOST`` defaults to ``127.0.0.1`` (loopback; containers bind 0.0.0.0
    via the env, publishing stays host-loopback). ``MCP_PORT`` falls back to
    the per-server ``default_port``.
    """
    host: Optional[str] = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", default_port))
    return host, port
