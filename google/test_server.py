"""Google MCP server tests. googleapiclient mocked; audit + MIME building covered."""
import json

import pytest


def test_mime_raw_builds_rfc822():
    from google_mcp.client import mime_raw
    raw = mime_raw("a@b.c", "Hi", "<p>yo</p>", html=True)
    import base64
    text = base64.urlsafe_b64decode(raw).decode()
    assert "To: a@b.c" in text and "Subject: Hi" in text


def test_extract_message_body_prefers_plain():
    from google_mcp.client import extract_message_body
    import base64
    plain = base64.urlsafe_b64encode(b"hello").decode()
    msg = {"payload": {"parts": [
        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<b>x</b>").decode()}},
        {"mimeType": "text/plain", "body": {"data": plain}},
    ]}}
    assert extract_message_body(msg) == "hello"


def test_extract_message_body_recurses_nested_parts():
    from google_mcp.client import extract_message_body
    import base64
    inner_plain = base64.urlsafe_b64encode(b"nested text").decode()
    msg = {"payload": {"parts": [
        {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": inner_plain}},
            ]},
        ]},
    ]}}
    assert extract_message_body(msg) == "nested text"


def test_gmail_search_shape(monkeypatch):
    import google_mcp.gmail as g

    class R:
        def __init__(self, val):
            self._v = val
        def execute(self):
            return self._v

    class Msgs:
        def __call__(self):
            return self
        def list(self, **kw):
            assert kw["q"] == "test"
            return R({"messages": [{"id": "m1", "snippet": "s",
                                    "threadId": "t"}]})
        def get(self, **kw):
            return R({"id": "m1", "threadId": "t", "snippet": "s",
                      "payload": {"headers": [
                          {"name": "From", "value": "x@y.z"},
                          {"name": "Subject", "value": "S"}]}})

    class FakeSvc:
        def __init__(self):
            self.users = self.U()
        class U:
            def __call__(self):
                return self
            def __init__(self):
                self.messages = Msgs()

    monkeypatch.setattr(g, "build_service", lambda api, v: FakeSvc())
    out = g.search("test", 5)
    assert out[0]["snippet"] == "s" and out[0]["threadId"] == "t"


def test_sheets_update_calls_user_entered(monkeypatch):
    import google_mcp.gsuite as gs
    captured = {}

    def update(**kw):
        captured.update(kw)
        return {"updatedCells": 3}

    class FakeSvc:
        class spreadsheets:
            class values:
                @staticmethod
                def update(**kw):
                    return type("R", (), {"execute": lambda self: update(**kw)})()

    monkeypatch.setattr(gs, "build_service", lambda api, v: FakeSvc())
    result = gs.sheets_update("sid", "A1:B2", [["a", "b"]])
    assert captured["valueInputOption"] == "USER_ENTERED"
    assert result["updatedCells"] == 3


def test_calendar_create_stamps_timezone(monkeypatch):
    import google_mcp.gsuite as gs
    captured = {}

    def insert(**kw):
        captured.update(kw)
        return {"id": "e1", "htmlLink": "http://x"}

    class FakeSvc:
        class events:
            @staticmethod
            def insert(**kw):
                captured.update(kw)
                return type("R", (), {"execute": lambda self: {"id": "e1", "htmlLink": "h"}})()

    monkeypatch.setattr(gs, "build_service", lambda api, v: FakeSvc())
    result = gs.create_event("T", "2026-01-01T10:00:00", "2026-01-01T11:00:00")
    assert captured["body"]["start"]["timeZone"] == "Europe/London"
    assert result["id"] == "e1"


def test_drive_search_escapes_quotes(monkeypatch):
    import google_mcp.gsuite as gs
    captured = {}

    class FakeSvc:
        class files:
            @staticmethod
            def list(**kw):
                captured["q"] = kw["q"]
                return type("R", (), {"execute": lambda self: {"files": []}})()

    monkeypatch.setattr(gs, "build_service", lambda api, v: FakeSvc())
    gs.drive_search("it's")
    escaped = captured["q"].split("contains")[1]
    assert "\\'" in escaped or "it\\\\'s" in captured["q"]


def test_audit_redaction_shared():
    from google_mcp import audit
    out = audit.redact({"Authorization": "Bearer zz", "q": "sk-abc123 x"})
    assert out["Authorization"] == "***" and "sk-" not in out["q"]


def test_audit_logs_dir_env_override(monkeypatch):
    """MCP_LOGS_DIR overrides the hardcoded ~/.hermes/mcp-logs default."""
    import importlib
    from google_mcp import audit
    monkeypatch.setenv("MCP_LOGS_DIR", "/app/logs")
    reloaded = importlib.reload(audit)
    assert reloaded._LOGS_DIR == "/app/logs"
    monkeypatch.delenv("MCP_LOGS_DIR")
    assert importlib.reload(audit)._LOGS_DIR.endswith("mcp-logs")
    importlib.reload(audit)  # restore module state for other tests
