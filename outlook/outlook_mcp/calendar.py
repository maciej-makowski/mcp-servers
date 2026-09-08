"""Calendar operations (Graph /me/calendars)."""
from __future__ import annotations

from typing import Any

from .graph import request


def list_calendars() -> Any:
    return request("GET", "/me/calendars")


def list_events(calendar_id: str = "primary", top: int = 10) -> Any:
    return request("GET", f"/me/calendars/{calendar_id}/events",
                   params={"$top": top, "$orderby": "start/dateTime"})


def create_event(calendar_id: str, subject: str, start: str, end: str,
                 body_html: str = "", online: bool = False) -> Any:
    event: dict = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
    }
    if body_html:
        event["body"] = {"contentType": "HTML", "content": body_html}
    if online:
        event["isOnlineMeeting"] = True
        event["onlineMeetingProvider"] = "teamsForBusiness"
    return request("POST", f"/me/calendars/{calendar_id}/events", json_body=event)


def delete_event(calendar_id: str, event_id: str) -> Any:
    return request("DELETE", f"/me/calendars/{calendar_id}/events/{event_id}", ok=(204,))
