"""Gmail operations."""
from __future__ import annotations

from .client import build_service, extract_message_body, headers_dict, mime_raw


def search(query: str, max_results: int = 20) -> list:
    """Single list call with metadata fields — no per-message get (N+1 avoided)."""
    service = build_service("gmail", "v1")
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results).execute()
    out = []
    for meta in results.get("messages", []):
        out.append({"id": meta["id"], "threadId": meta.get("threadId", ""),
                    "snippet": meta.get("snippet", "")})
    return out


def get(message_id: str) -> dict:
    service = build_service("gmail", "v1")
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full").execute()
    return {**{k: msg.get(k) for k in ("id", "threadId", "snippet", "labelIds")},
            "headers": headers_dict(msg), "body": extract_message_body(msg)}


def send(to: str, subject: str, body: str, html: bool = False) -> dict:
    service = build_service("gmail", "v1")
    result = service.users().messages().send(
        userId="me", body={"raw": mime_raw(to, subject, body, html)}).execute()
    return {"status": "sent", "id": result["id"],
            "threadId": result.get("threadId", "")}
