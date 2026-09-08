"""OneDrive operations (Graph /me/drive). Content-type auto-detected by extension.

Simple PUT covers files < 4 MB (Graph limit); larger files use an upload
session with chunked PUTs streamed from disk.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
from urllib.parse import quote

import requests

from .graph import access_token, GRAPH, GraphError

log = logging.getLogger("mcp.outlook.onedrive")

SIMPLE_PUT_LIMIT = 4 * 1024 * 1024
CHUNK_SIZE = 5 * 1024 * 1024  # multiple of 320 KiB, within Graph's 1–60 MiB window

CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".flac": "audio/flac",
}


def _ctype(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(ext) or mimetypes.types_map.get(ext, "application/octet-stream")


def _auth_headers(content_type: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token()}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def upload(local_path: str, name: str | None = None) -> dict:
    name = name or os.path.basename(local_path)
    quoted = "/".join(quote(p) for p in name.split("/"))
    size = os.path.getsize(local_path)
    if size < SIMPLE_PUT_LIMIT:
        status, body = _simple_put(local_path, quoted)
    else:
        status, body = _session_upload(local_path, quoted, size)
    web_url = body.get("webUrl") if isinstance(body, dict) else None
    return {"status": status, "name": name, "webUrl": web_url}


def _simple_put(local_path: str, quoted_name: str) -> tuple[int, dict | str]:
    with open(local_path, "rb") as f:
        resp = requests.put(f"{GRAPH}/me/drive/root:/{quoted_name}:/content",
                            headers=_auth_headers(_ctype(local_path)), data=f,
                            timeout=120)
    if resp.status_code not in (200, 201):
        raise GraphError(resp.status_code, resp.text)
    return resp.status_code, resp.json()


def _session_upload(local_path: str, quoted_name: str, size: int) -> tuple[int, dict | str]:
    with open(local_path, "rb") as f:
        r = requests.post(f"{GRAPH}/me/drive/root:/{quoted_name}:/createUploadSession",
                          headers=_auth_headers(), json={}, timeout=30)
        if r.status_code != 200:
            raise GraphError(r.status_code, r.text)
        upload_url = r.json()["uploadUrl"]
    sent = 0
    status = 200
    body: dict | str = {}
    with open(local_path, "rb") as f:
        while sent < size:
            chunk = f.read(CHUNK_SIZE)
            headers = {"Content-Range": f"bytes {sent}-{sent + len(chunk) - 1}/{size}",
                       **_auth_headers()}
            resp = requests.put(upload_url, data=chunk, headers=headers, timeout=300)
            if resp.status_code >= 500 and sent + len(chunk) < size:
                raise GraphError(resp.status_code,
                                 f"session upload interrupted at {sent}/{size}: {resp.text[:300]}")
            if resp.status_code >= 400:
                raise GraphError(resp.status_code, resp.text)
            sent += len(chunk)
            status = resp.status_code
            if resp.content:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
    log.info("onedrive session upload done: %s (%d bytes)", quoted_name, size)
    return status, body
