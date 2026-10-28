"""Microsoft To-Do operations (Graph /me/todo)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .graph import request


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def list_task_lists() -> Any:
    return request("GET", "/me/todo/lists")


def list_tasks(list_id: str, completed: bool | None = None) -> Any:
    all_tasks = request("GET", f"/me/todo/lists/{list_id}/tasks")["value"]
    if completed is None:
        return {"value": all_tasks}
    return {"value": [
        t for t in all_tasks
        if bool(t.get("completedDateTime", {}).get("dateTime")) is bool(completed)
    ]}


def create_task(list_id: str, subject: str, due: str | None = None,
                importance: str = "normal", completed: bool = False) -> Any:
    task: dict = {"title": subject, "importance": importance}
    if completed:
        task["completedDateTime"] = {"dateTime": _now_utc(), "timeZone": "UTC"}
    if due:
        task["dueDateTime"] = {"dateTime": due, "timeZone": "UTC"}
    return request("POST", f"/me/todo/lists/{list_id}/tasks", json_body=task)


def update_task(list_id: str, task_id: str, *, subject: str | None = None,
                due: str | None = None, importance: str | None = None,
                completed: bool | None = None) -> Any:
    patch: dict = {}
    if subject is not None:
        patch["title"] = subject
    if due is not None:
        patch["dueDateTime"] = {"dateTime": due, "timeZone": "UTC"}
    if importance is not None:
        patch["importance"] = importance
    if completed is not None:
        if completed:
            patch["completedDateTime"] = {"dateTime": _now_utc(), "timeZone": "UTC"}
            patch["status"] = "completed"
        else:
            patch["completedDateTime"] = None
            patch["status"] = "notStarted"
    return request("PATCH", f"/me/todo/lists/{list_id}/tasks/{task_id}",
                   json_body=patch)


def delete_task(list_id: str, task_id: str) -> Any:
    return request("DELETE", f"/me/todo/lists/{list_id}/tasks/{task_id}", ok=(204,))
