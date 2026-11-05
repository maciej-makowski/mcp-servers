"""Calendar + Drive + Sheets operations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import build_service

TZ = "Europe/London"

# The only host directory shared with the agent side (Design B spool, see
# deploy/README.md's Shared spool section). drive_upload refuses paths
# outside it: an arbitrary-local-path Drive upload from this container would
# be an exfiltration surface. Constant so tests can point it at a tmp tree.
SPOOL_SEND_ROOT = Path("/srv/mcp_files/send")


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


def drive_upload(local_path: str, name: str = "") -> dict:
    """Upload a file into Drive from the shared spool only.

    local_path must resolve under SPOOL_SEND_ROOT (/srv/mcp_files/send) —
    the one directory the agent side shares with this container; anything
    else is refused. Resumable chunked upload: any file size.
    """
    from googleapiclient.http import MediaFileUpload

    spool_root = SPOOL_SEND_ROOT.resolve()
    candidate = Path(local_path).resolve()
    if candidate != spool_root and spool_root not in candidate.parents:
        raise ValueError(
            "drive_upload only accepts files under /srv/mcp_files/send")
    if not candidate.is_file():
        raise ValueError(f"local file not found: {local_path}")
    target_name = name or candidate.name
    service = build_service("drive", "v3")
    media = MediaFileUpload(str(candidate), resumable=True,
                            chunksize=1024 * 1024)
    request = service.files().create(
        body={"name": target_name}, media_body=media,
        fields="id,name,size,mimeType,webViewLink")
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return {k: response.get(k) for k in
            ("id", "name", "size", "mimeType", "webViewLink")}


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


def sheets_batch_update(sheet_id: str, requests: list) -> dict:
    result = build_service("sheets", "v4").spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}).execute()
    return {"spreadsheetId": result.get("spreadsheetId", ""),
            "replies": result.get("replies", [])}


def sheets_metadata_get(sheet_id: str, fields: str = "") -> dict:
    kw = {"spreadsheetId": sheet_id}
    if fields:
        kw["fields"] = fields
    return build_service("sheets", "v4").spreadsheets().get(**kw).execute()


def sheets_create(title: str) -> dict:
    result = build_service("sheets", "v4").spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId,properties(title),spreadsheetUrl").execute()
    return {"spreadsheetId": result.get("spreadsheetId", ""),
            "spreadsheetUrl": result.get("spreadsheetUrl", "")}
