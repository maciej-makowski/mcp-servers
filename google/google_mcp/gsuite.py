"""Calendar + Drive + Sheets operations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .client import build_service

TZ = "Europe/London"


def list_events(max_results: int = 10, start: str = "", end: str = "") -> list:
    now = datetime.now(timezone.utc)
    service = build_service("calendar", "v3")
    results = service.events().list(
        calendarId="primary",
        timeMin=start or now.isoformat(),
        timeMax=end or (now + timedelta(days=7)).isoformat(),
        maxResults=max_results, singleEvents=True, orderBy="startTime",
    ).execute()
    return [{
        "id": e["id"], "summary": e.get("summary", "(no title)"),
        "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
        "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
        "location": e.get("location", ""), "description": e.get("description", ""),
        "htmlLink": e.get("htmlLink", ""),
    } for e in results.get("items", [])]


def create_event(subject: str, start: str, end: str, description: str = "",
                 location: str = "") -> dict:
    event: dict = {"summary": subject,
                   "start": {"dateTime": start, "timeZone": TZ},
                   "end": {"dateTime": end, "timeZone": TZ}}
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    result = build_service("calendar", "v3").events().insert(
        calendarId="primary", body=event).execute()
    return {"status": "created", "id": result["id"],
            "htmlLink": result.get("htmlLink", "")}


def drive_search(query: str, max_results: int = 10) -> list:
    service = build_service("drive", "v3")
    safe = query.replace("'", "\\'")
    results = service.files().list(
        q=f"name contains '{safe}'", pageSize=max_results,
        fields="files(id,name,mimeType,modifiedTime,size)").execute()
    return results.get("files", [])


def sheets_get(sheet_id: str, range_name: str) -> list:
    result = build_service("sheets", "v4").spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_name).execute()
    return result.get("values", [])


def sheets_update(sheet_id: str, range_name: str, values: list) -> dict:
    result = build_service("sheets", "v4").spreadsheets().values().update(
        spreadsheetId=sheet_id, range=range_name,
        valueInputOption="USER_ENTERED", body={"values": values}).execute()
    return {"updatedCells": result.get("updatedCells", 0),
            "updatedRange": result.get("updatedRange", "")}
