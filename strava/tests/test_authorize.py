"""Authorize helper tests: URL shape, exchange flow, no token echo (D7)."""

import json

import httpx
import pytest

from strava_mcp import authorize, oauth


STORE = {
    "token_type": "Bearer",
    "expires_at": 9999999999,
    "expires_in": 21600,
    "access_token": "st-access-1",
    "refresh_token": "st-refresh-1",
    "athlete": {"id": 42},
}


def test_build_auth_url_shape():
    url = authorize.build_auth_url("12345")
    assert url.startswith(oauth.AUTHORIZE_URL + "?")
    assert "client_id=12345" in url
    assert "response_type=code" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%2Fexchange_token" in url
    # D3 scope list, comma-delimited per W1.
    assert "scope=read%2Cprofile%3Aread_all%2Cactivity%3Aread_all" in url


def test_scope_constant_matches_d3():
    assert oauth.SCOPES == "read,profile:read_all,activity:read_all"
    assert authorize.SCOPES == oauth.SCOPES


def test_main_exchange_flow_writes_store_without_echoing_tokens(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "csec")
    path = str(tmp_path / "token.json")
    monkeypatch.setattr(oauth, "TOKEN_PATH", path)
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured.update(url=url, data=data)
        return httpx.Response(
            200, json=dict(STORE), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    monkeypatch.setattr("builtins.input", lambda *a: "goodcode")

    authorize.main()

    out = capsys.readouterr().out
    assert "OK" in out
    assert "42" in out  # athlete id is fine to print; tokens are not
    assert "st-access-1" not in out and "st-refresh-1" not in out
    assert captured["data"]["code"] == "goodcode"
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["access_token"] == "st-access-1"


def test_main_aborts_on_empty_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "csec")
    monkeypatch.setattr("builtins.input", lambda *a: "   ")
    with pytest.raises(SystemExit) as exc:
        authorize.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "st-access-1" not in out
    assert not (tmp_path / "token.json").exists()


def test_main_missing_env_credentials_exits(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        authorize.main()
    assert exc.value.code == 1


def test_main_exchange_failure_exits(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "csec")
    monkeypatch.setattr(oauth, "TOKEN_PATH", str(tmp_path / "token.json"))

    def bad_post(url, data=None, timeout=None):
        return httpx.Response(
            400,
            json={"error": "bad_authorization_code"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(oauth.httpx, "post", bad_post)
    monkeypatch.setattr("builtins.input", lambda *a: "expired-code")
    with pytest.raises(SystemExit) as exc:
        authorize.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "exchange failed" in err
    assert "st-access-1" not in err
