"""Mail operations (Graph /me/messages)."""
from __future__ import annotations

from typing import Any

from .graph import request


def list_messages(top: int = 10) -> Any:
    return request("GET", "/me/messages",
                   params={"$top": top, "$orderby": "receivedDateTime desc"})


def search_messages(query: str, top: int = 10) -> Any:
    # $search takes a KQL phrase; embedded quotes must be doubled or Graph 400s.
    # body: is slower than subject: but catches matches $filter cannot express.
    safe = query.replace('"', '""')
    return request("GET", "/me/messages", params={
        "$search": f'"subject:{safe} OR body:{safe}"', "$top": top})


def get_message(message_id: str) -> Any:
    return request("GET", f"/me/messages/{message_id}")


def send_message(to: str, subject: str, body_html: str) -> Any:
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
    }
    return request("POST", "/me/sendMail", json_body=message, ok=(202,))
