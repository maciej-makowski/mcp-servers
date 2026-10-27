"""Shared Graph API client for the outlook MCP server.

Owns the access token lifecycle (tokens at ~/.config/office365/tokens.json,
transparent refresh via the stored refresh_token) and every HTTP call. All
tool modules route requests through here so logging and redaction are uniform.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from typing import Any

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_FILE = os.path.expanduser("~/.config/office365/tokens.json")

log = logging.getLogger("mcp.outlook.graph")


class GraphError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Graph HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def _load_tokens() -> dict:
    with open(TOKEN_FILE) as f:
        return json.load(f)


def _save_tokens(tokens: dict) -> None:
    # Atomic: crash mid-write must not destroy the only refresh-token copy.
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def access_token() -> str:
    tokens = _load_tokens()
    if tokens.get("access_token") and time.time() < tokens.get("expires_at", 0):
        return tokens["access_token"]
    rt = tokens.get("refresh_token")
    if not rt:
        raise GraphError(0, "no refresh_token in tokens.json — re-run device-code auth")
    # Serialize refresh: Entra rotates refresh_token on use; two racing
    # refreshes would let the loser overwrite the winner's rotated token.
    lock_path = TOKEN_FILE + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    tokens = _load_tokens()
                except (OSError, json.JSONDecodeError) as exc:
                    raise GraphError(0, f"tokens.json unreadable: {exc}") from exc
                if tokens.get("access_token") and time.time() < tokens.get("expires_at", 0):
                    return tokens["access_token"]
                resp = requests.post(tokens["token_url"], data={
                    "client_id": tokens.get("client_id"),
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.get("refresh_token", ""),
                    "scope": tokens.get("scope", ""),
                }, timeout=20)
                if not resp.ok:
                    raise GraphError(resp.status_code, f"token refresh failed: {resp.text[:300]}")
                r = resp.json()
                tokens["access_token"] = r["access_token"]
                if r.get("refresh_token"):
                    tokens["refresh_token"] = r["refresh_token"]
                tokens["expires_at"] = int(time.time()) + int(r.get("expires_in", 3600)) - 120
                _save_tokens(tokens)
                log.info("graph token refreshed, expires_at=%s", tokens["expires_at"])
                return tokens["access_token"]
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def refresh_credentials() -> None:
    """Force ONE token refresh through the existing flocked+atomic-write path.

    Used by the idle-refresh loop: drops the cached access_token so the
    unexpired short-circuit in access_token() can't fire, then lets its
    flock-serialized refresh + atomic save do the work.
    """
    tokens = _load_tokens()
    tokens.pop("access_token", None)
    _save_tokens(tokens)
    access_token()
    log.info("graph token force-refreshed")


def request(method: str, path: str, *, params: dict | None = None,
            json_body: dict | None = None, ok: tuple = ()) -> Any:
    """Graph call returning parsed JSON (or {} for 204). Raises GraphError on failure."""
    url = f"{GRAPH}{path}"
    headers = {"Authorization": f"Bearer {access_token()}"}
    resp = requests.request(method, url, headers=headers, params=params, json=json_body,
                            timeout=30)
    if 200 <= resp.status_code < 300 or resp.status_code in ok:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            log.warning("graph %s %s: non-JSON body on %s", method, path, resp.status_code)
            return {}
    raise GraphError(resp.status_code, resp.text)
