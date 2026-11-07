"""Token store tests: atomic writes, refresh, idle-refresh condition (W1).

The token endpoint is mocked (no network); stores live in tmp_path.
"""

import asyncio
import contextlib
import json
import os
import time

import httpx
import pytest

from strava_mcp import idle_refresh, oauth


STORE = {
    "token_type": "Bearer",
    "expires_at": 1762600000,
    "expires_in": 21600,
    "access_token": "st-access-1",
    "refresh_token": "st-refresh-1",
    "athlete": {"id": 42},
}


@pytest.fixture()
def store_path(tmp_path):
    return str(tmp_path / "token.json")


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", oauth.TOKEN_URL)
    )


def test_load_store_missing_raises(store_path):
    with pytest.raises(oauth.TokenError, match="missing"):
        oauth.load_store(store_path)


def test_load_store_rejects_garbage(store_path):
    with open(store_path, "w") as f:
        f.write("not json")
    with pytest.raises(oauth.TokenError, match="not valid JSON"):
        oauth.load_store(store_path)


def test_load_store_rejects_empty_token(store_path):
    oauth._write_store_atomic({"access_token": ""}, store_path)
    with pytest.raises(oauth.TokenError, match="no access_token"):
        oauth.load_store(store_path)


def test_write_store_atomic(store_path):
    oauth._write_store_atomic(dict(STORE), store_path)
    with open(store_path, encoding="utf-8") as f:
        assert json.load(f)["access_token"] == "st-access-1"
    assert (os.stat(store_path).st_mode & 0o777) == 0o600


def test_exchange_code_writes_store(store_path, monkeypatch):
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured.update(url=url, data=data)
        return _response(dict(STORE))

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "sec")

    store = oauth.exchange_code("thecode", path=store_path)

    assert captured["url"] == oauth.TOKEN_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "thecode"
    assert captured["data"]["client_id"] == "cid"
    assert store["access_token"] == "st-access-1"
    assert oauth.load_store(store_path)["refresh_token"] == "st-refresh-1"


def test_exchange_rejects_bad_payload(store_path, monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "sec")
    monkeypatch.setattr(
        oauth.httpx, "post", lambda url, data=None, timeout=None: _response({"x": 1})
    )
    with pytest.raises(oauth.TokenError, match="no access_token"):
        oauth.exchange_code("thecode", path=store_path)


def test_refresh_updates_store_and_keeps_athlete(store_path, monkeypatch):
    oauth._write_store_atomic(dict(STORE), store_path)
    payload = dict(
        STORE,
        access_token="st-access-2",
        refresh_token="st-refresh-2",
        expires_at=int(time.time()) + 21600,
    )
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured.update(data=data)
        return _response(payload)

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "sec")

    store = oauth.refresh_access_token(store_path)

    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "st-refresh-1"
    assert store["access_token"] == "st-access-2"
    assert store["refresh_token"] == "st-refresh-2"
    assert store["athlete"]["id"] == 42  # merged: the athlete survives rotation
    on_disk = oauth.load_store(store_path)
    assert on_disk["refresh_token"] == "st-refresh-2"


def test_refresh_requires_env_credentials(store_path):
    oauth._write_store_atomic(dict(STORE), store_path)
    with pytest.raises(oauth.TokenError, match="STRAVA_CLIENT_ID"):
        oauth.refresh_access_token(store_path)


def test_refresh_rejects_store_without_refresh_token(store_path, monkeypatch):
    oauth._write_store_atomic({"access_token": "st-access-1"}, store_path)
    with pytest.raises(oauth.TokenError, match="no refresh_token"):
        oauth.refresh_access_token(store_path)


def test_athlete_id_and_seconds_left(store_path):
    oauth._write_store_atomic(dict(STORE), store_path)
    store = oauth.load_store(store_path)
    assert oauth.athlete_id(store) == 42
    assert oauth.seconds_left(store, now=STORE["expires_at"] - 10) == 10
    assert oauth.seconds_left({}, now=0) == -1.0


def test_refresh_due_condition():
    now = 1000.0
    assert idle_refresh.refresh_due({"expires_at": now + 7200}, now=now) is False
    assert idle_refresh.refresh_due({"expires_at": now + 1800}, now=now) is True
    # Exactly one hour of life: W1 says the server only refreshes when the
    # token is UNDER one hour from expiry.
    assert idle_refresh.refresh_due({"expires_at": now + 3600}, now=now) is False
    # Unknown expiry: treated as due (a failed refresh is retried next tick).
    assert idle_refresh.refresh_due({}, now=now) is True


def test_idle_loop_refreshes_when_due(store_path, monkeypatch):
    now = time.time()
    oauth._write_store_atomic(
        dict(STORE, expires_at=int(now) + 1800), store_path
    )
    calls = []

    def fake_refresh(path=None):
        calls.append(path)
        oauth._write_store_atomic(dict(STORE, expires_at=int(now) + 99999), store_path)
        return dict(STORE)

    monkeypatch.setattr(oauth, "refresh_access_token", fake_refresh)

    async def run():
        task = idle_refresh.start_idle_refresh(token_path=store_path, interval_s=0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(calls) == 1  # refreshed once, then the store is far from expiry


def test_idle_loop_survives_refresh_failure(store_path, monkeypatch):
    now = time.time()
    oauth._write_store_atomic(dict(STORE, expires_at=int(now) + 100), store_path)

    def boom(path=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(oauth, "refresh_access_token", boom)

    async def run():
        task = idle_refresh.start_idle_refresh(token_path=store_path, interval_s=0.01)
        await asyncio.sleep(0.05)
        assert not task.done()  # the loop kept running despite the failure
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
