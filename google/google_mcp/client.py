"""Shared Google API client: credentials from ~/.hermes/google_token.json
(authorized-user flow, transparent refresh), service construction, helpers.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from email.mime.text import MIMEText

HERMES_HOME = os.path.expanduser("~/.hermes")
TOKEN_PATH = os.path.join(HERMES_HOME, "google_token.json")

# gmail.modify supersedes readonly+send; no redundant narrower scopes.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

log = logging.getLogger("mcp.google.client")


class GoogleAPIError(RuntimeError):
    pass


def _ensure_token_file() -> None:
    if not os.path.exists(TOKEN_PATH):
        raise GoogleAPIError(
            "google_token.json missing — run the google-workspace setup once")


def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    _ensure_token_file()
    with open(TOKEN_PATH) as f:
        raw = json.load(f)
    scopes = raw.get("scopes") or SCOPES
    creds = Credentials.from_authorized_user_info(raw, scopes)
    if creds.expired and creds.refresh_token:
        log.info("google token expired, refreshing")
        creds.refresh(Request())
        payload = json.loads(creds.to_json())
        payload.setdefault("type", "authorized_user")
        _write_token_atomic(payload)
        log.info("google token refreshed")
    if not creds.valid:
        raise GoogleAPIError("google token invalid — re-run setup flow")
    return creds


def _write_token_atomic(payload: dict) -> None:
    """Temp-file + replace so a mid-write crash can't destroy the refresh token."""
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_PATH)


def build_service(api: str, version: str):
    from googleapiclient.discovery import build

    return build(api, version, credentials=get_credentials())


def headers_dict(msg: dict) -> dict:
    return {h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])}


def _part_body(part: dict) -> str | None:
    data = part.get("body", {}).get("data")
    if not data:
        return None
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def extract_message_body(msg: dict, prefer_html: bool = False) -> str:
    """Depth-first over nested multipart; prefers text/plain (else html) across
    the WHOLE tree before falling back to the other type."""
    order = ("text/html", "text/plain") if prefer_html else ("text/plain", "text/html")
    parts = []

    def walk(part: dict) -> None:
        parts.append(part)
        for child in part.get("parts", []):
            walk(child)

    walk(msg.get("payload", {}))
    for mime in order:
        for part in parts:
            if part.get("mimeType") == mime:
                text = _part_body(part)
                if text:
                    return text
    return ""


def mime_raw(to: str, subject: str, body: str, html: bool = False) -> str:
    message = MIMEText(body, "html" if html else "plain")
    message["To"] = to
    message["Subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode()
