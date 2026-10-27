"""Outlook MCP server tests. Graph calls mocked; audit logging + redaction covered."""
import json
import logging
import os
import time

import pytest

from outlook_mcp import audit, graph  # noqa: E402


def test_mail_search_uses_dollar_search(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update(kw.get("params") or {})
        return {"value": []}

    monkeypatch.setattr("outlook_mcp.mail.request", fake)
    import outlook_mcp.mail as mail
    mail.search_messages("gift aid")
    assert any("$search" in k for k in seen)


def test_send_message_posts_sendmail(monkeypatch):
    calls = {}

    def fake(method, path, **kw):
        calls["m"], calls["p"], calls["j"] = method, path, kw.get("json_body")
        return {}

    monkeypatch.setattr("outlook_mcp.mail.request", fake)
    import outlook_mcp.mail as mail
    mail.send_message("a@b.c", "Hi", "<p>x</p>")
    assert (calls["m"], calls["p"]) == ("POST", "/me/sendMail")
    assert calls["j"]["message"]["toRecipients"][0]["emailAddress"]["address"] == "a@b.c"


def test_calendar_create_event_shape(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update(kw.get("json_body") or {})
        return {"id": "e1"}

    monkeypatch.setattr("outlook_mcp.calendar.request", fake)
    import outlook_mcp.calendar as cal
    cal.create_event("primary", "T", "2026-01-01T10:00:00", "2026-01-01T11:00:00")
    assert seen["start"]["dateTime"] == "2026-01-01T10:00:00"


def test_tasks_create_with_due(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update(kw.get("json_body") or {})
        return {"id": "t1"}

    monkeypatch.setattr("outlook_mcp.tasks.request", fake)
    import outlook_mcp.tasks as tasks
    tasks.create_task("lid", "Do it", due="2026-02-01T09:00:00")
    assert seen["dueDateTime"]["dateTime"] == "2026-02-01T09:00:00"


def test_token_refresh_flow(monkeypatch, tmp_path):
    tfile = os.path.join(tmp_path, "tokens.json")
    with open(tfile, "w") as f:
        json.dump({"access_token": "old", "expires_at": 0,
                   "token_url": "https://login.test/token",
                   "client_id": "cid", "refresh_token": "rt",
                   "scope": "s"}, f)
    monkeypatch.setattr(graph, "TOKEN_FILE", tfile)

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
            self.ok = True
        def json(self):
            return self._p

    monkeypatch.setattr("outlook_mcp.graph.requests.post",
                        lambda url, data, timeout: FakeResp(
                            {"access_token": "NEW", "refresh_token": "rt2",
                             "expires_in": 3600}))
    token = graph.access_token()
    assert token == "NEW"
    saved = json.load(open(tfile))
    assert saved["access_token"] == "NEW" and saved["refresh_token"] == "rt2"
    assert saved.get("expires_at", 0) > 0
    import stat
    assert stat.S_IMODE(os.stat(tfile).st_mode) & 0o777 == 0o600


def test_audit_redacts_secrets():
    out = audit.redact({"Authorization": "Bearer abcdef", "query": "x ntn_abc123"})
    assert out["Authorization"] == "***"
    assert "ntn_" not in out["query"]


def _reset_audit_loggers():
    """Drop cached handlers so tests don't accumulate stale file handles."""
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("mcp."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                h.close()


def test_audit_log_written(tmp_path, monkeypatch):
    logdir = os.path.join(str(tmp_path), "mcp-logs", "testsrv")
    _reset_audit_loggers()
    monkeypatch.setattr(audit, "_LOGS_DIR", os.path.join(str(tmp_path), "mcp-logs"))

    @audit.log_call("testsrv", "t1")
    def ok_fn() -> str:
        return "fine"

    assert ok_fn() == "fine"
    with open(os.path.join(logdir, "t1.json")) as f:
        entry = json.loads(f.readline())
    assert entry["args"] == {} and entry["output"] == "fine"
    assert "CALL t1" in open(os.path.join(logdir, "server.log")).read()
    _reset_audit_loggers()


def test_audit_fail_logged(tmp_path, monkeypatch):
    logdir = os.path.join(str(tmp_path), "mcp-logs", "testsrv")
    _reset_audit_loggers()
    monkeypatch.setattr(audit, "_LOGS_DIR", os.path.join(str(tmp_path), "mcp-logs"))

    @audit.log_call("testsrv", "boom")
    def bad():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        bad()
    assert "FAIL boom" in open(os.path.join(logdir, "server.log")).read()
    _reset_audit_loggers()


def _token_file(tmp_path):
    """Self-contained token file so access_token() short-circuits without
    reading the real ~/.config/office365/tokens.json (absent on CI runners)."""
    tfile = os.path.join(str(tmp_path), "tokens.json")
    with open(tfile, "w") as f:
        json.dump({"access_token": "tok", "expires_at": time.time() + 9999,
                   "refresh_token": "rt"}, f)
    return tfile


def test_request_accepts_201(monkeypatch, tmp_path):
    import outlook_mcp.graph as g
    monkeypatch.setattr(graph, "TOKEN_FILE", _token_file(tmp_path))
    resp = type("R", (), {"status_code": 201,
                          "content": b'{"id": "e1"}',
                          "json": lambda self: {"id": "e1"},
                          "text": "{}"})()
    monkeypatch.setattr("outlook_mcp.graph.requests.request",
                        lambda *a, **kw: resp)
    assert graph.request("POST", "/x", json_body={}) == {"id": "e1"}


def test_request_rejects_4xx(monkeypatch, tmp_path):
    import outlook_mcp.graph as g
    monkeypatch.setattr(graph, "TOKEN_FILE", _token_file(tmp_path))
    resp = type("R", (), {"status_code": 403, "content": b"", "text": "denied"})()
    monkeypatch.setattr("outlook_mcp.graph.requests.request",
                        lambda *a, **kw: resp)
    with pytest.raises(graph.GraphError) as exc:
        graph.request("GET", "/x")
    assert exc.value.status == 403


def test_token_refresh_failure_raises(monkeypatch, tmp_path):
    tfile = tmp_path / "tokens.json"
    tfile.write_text(json.dumps({"access_token": "old", "expires_at": 0,
                                 "token_url": "https://login.test/token",
                                 "client_id": "cid", "refresh_token": "rt",
                                 "scope": "s"}))
    monkeypatch.setattr(graph, "TOKEN_FILE", str(tfile))
    monkeypatch.setattr(graph, "_load_tokens", lambda: json.load(open(tfile)))

    resp = type("R", (), {"ok": False, "status_code": 400,
                          "text": '{"error":"invalid_grant"}'})()
    monkeypatch.setattr("outlook_mcp.graph.requests.post", lambda *a, **kw: resp)
    with pytest.raises(graph.GraphError) as exc:
        graph.access_token()
    assert "invalid_grant" in str(exc.value)


def test_onedrive_upload_quotes_name(monkeypatch, tmp_path):
    import outlook_mcp.onedrive as od

    seen = {}

    def fake_put(url, **kw):
        seen["url"] = url
        return type("R", (), {"status_code": 201,
                              "json": lambda self: {"webUrl": "http://x"}})()

    monkeypatch.setattr(od, "access_token", lambda: "t")
    monkeypatch.setattr(od.requests, "put", fake_put)
    p = tmp_path / "f.txt"
    p.write_text("hi")
    result = od.upload(str(p), "weird name #1.txt")
    assert "%23" in seen["url"] and result["webUrl"] == "http://x"
    assert result["status"] == 201


def test_request_2xx_json_parse_fallback(monkeypatch, tmp_path):
    import outlook_mcp.graph as g
    monkeypatch.setattr(graph, "TOKEN_FILE", _token_file(tmp_path))
    resp = type("R", (), {"status_code": 200, "content": b"not-json",
                          "json": lambda self: (_ for _ in ()).throw(ValueError())})()
    monkeypatch.setattr("outlook_mcp.graph.requests.request",
                        lambda *a, **kw: resp)
    assert graph.request("GET", "/x") == {}


def test_audit_logs_dir_env_override(monkeypatch):
    """MCP_LOGS_DIR overrides the hardcoded ~/.hermes/mcp-logs default."""
    import importlib
    from outlook_mcp import audit
    monkeypatch.setenv("MCP_LOGS_DIR", "/app/logs")
    reloaded = importlib.reload(audit)
    assert reloaded._LOGS_DIR == "/app/logs"
    monkeypatch.delenv("MCP_LOGS_DIR")
    assert importlib.reload(audit)._LOGS_DIR.endswith("mcp-logs")
    importlib.reload(audit)  # restore module state for other tests
