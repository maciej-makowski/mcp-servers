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
