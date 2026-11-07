"""Token store + refresh for the Strava v3 API (design sections 3.2, 3.3).

The store lives at /app/data/token.json in the container (the quadlet mounts
the mcp-owned host directory /home/mcp/strava-token there). Every write is
atomic (temp file + os.replace) and single-flight (fcntl.flock on a lock
file beside the store): a refresh rotates the refresh token and invalidates
the previous one (W1), so a mid-write crash must never destroy the store.

Client credentials come from the environment (STRAVA_CLIENT_ID,
STRAVA_CLIENT_SECRET in the container env file). No token value is ever
written to a log; the audit layer's redaction covers token-shaped strings,
and nothing here prints token values.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from typing import Any

import httpx

TOKEN_PATH = "/app/data/token.json"
TOKEN_URL = "https://www.strava.com/oauth/token"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
SCOPES = "read,profile:read_all,activity:read_all"  # D3
TOKEN_TIMEOUT_S = 30.0


class TokenError(RuntimeError):
    """Token store or token-endpoint failure surfaced to the tool layer."""


def load_store(path: str | None = None) -> dict[str, Any]:
    path = path or TOKEN_PATH
    try:
        with open(path, encoding="utf-8") as f:
            store = json.load(f)
    except FileNotFoundError as exc:
        raise TokenError(
            f"token store missing at {path}: run "
            "`uv run --no-sync python -m strava_mcp.authorize` in the container first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TokenError(f"token store at {path} is not valid JSON") from exc
    if not isinstance(store, dict) or not store.get("access_token"):
        raise TokenError(f"token store at {path} has no access_token: re-authorize")
    return store


def access_token(store: dict[str, Any]) -> str:
    return store["access_token"]


def athlete_id(store: dict[str, Any]) -> int | None:
    """The authenticated athlete's id, stored with the exchange payload (W3)."""
    athlete = store.get("athlete")
    if isinstance(athlete, dict):
        return athlete.get("id")
    return None


def seconds_left(store: dict[str, Any], now: float | None = None) -> float:
    """Seconds of life left on the stored access token; -1.0 when unknown."""
    expires_at = store.get("expires_at")
    if expires_at is None:
        return -1.0
    return float(expires_at) - (now if now is not None else time.time())


def _client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise TokenError(
            "STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET missing from the environment"
        )
    return client_id, client_secret


def _token_http(data: dict[str, str]) -> dict[str, Any]:
    """POST the token endpoint (form-encoded). Tests monkeypatch this helper."""
    resp = httpx.post(TOKEN_URL, data=data, timeout=TOKEN_TIMEOUT_S)
    if resp.status_code != 200:
        raise TokenError(
            f"token endpoint returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


def exchange_code(code: str, path: str | None = None) -> dict[str, Any]:
    """Exchange an authorization code (grant_type=authorization_code, W1)."""
    client_id, client_secret = _client_credentials()
    payload = _token_http(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise TokenError("token endpoint response carries no access_token")
    _write_store_atomic(payload, path or TOKEN_PATH)
    return payload


def refresh_access_token(path: str | None = None) -> dict[str, Any]:
    """One refresh (grant_type=refresh_token, W1); rewrites the store atomically."""
    store = load_store(path)
    if not store.get("refresh_token"):
        raise TokenError("token store has no refresh_token: re-authorize")
    client_id, client_secret = _client_credentials()
    payload = _token_http(
        {
            "grant_type": "refresh_token",
            "refresh_token": store["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise TokenError("refresh response carries no access_token")
    # Merge so fields the token endpoint omits (the athlete object from the
    # exchange) survive every rotation. A new refresh token replaces the old.
    merged = dict(store)
    merged.update(payload)
    merged.setdefault("client_id", client_id)
    _write_store_atomic(merged, path or TOKEN_PATH)
    return merged


def _write_store_atomic(payload: dict[str, Any], path: str) -> None:
    """Temp-file + os.replace under fcntl.flock (W1, section 3.2)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
