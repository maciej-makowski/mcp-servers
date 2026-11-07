"""Client tests: guard, bearer, header ingestion, 401 retry, 429 wait.

No network: httpx.MockTransport stands in for the Strava API (house rule).
"""

import httpx
import pytest

from strava_mcp import client, oauth, rate


STORE = {
    "token_type": "Bearer",
    "expires_at": 9999999999,
    "expires_in": 21600,
    "access_token": "st-access-1",
    "refresh_token": "st-refresh-1",
    "athlete": {"id": 42},
}


def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def fake_client():
        return httpx.Client(base_url=client.BASE_URL, timeout=5, transport=transport)

    monkeypatch.setattr(client, "_client", fake_client)


def _install_store(monkeypatch, payload):
    monkeypatch.setattr(oauth, "load_store", lambda path=None: payload)


def test_guard_refuses_before_any_request(monkeypatch):
    made = []

    def handler(request):
        made.append(request)
        return httpx.Response(200, json={}, request=request)

    _install_transport(monkeypatch, handler)
    _install_store(monkeypatch, STORE)
    # 90 of 100 used: the D5 reserve leaves 10 remaining -> refuse.
    rate.STATE.update_from_headers(httpx.Headers({"X-ReadRateLimit-Usage": "90,500"}))
    with pytest.raises(rate.RateGuardError):
        client.api_get("/athlete")
    assert made == []  # nothing was spent


def test_get_attaches_bearer_and_ingests_headers(monkeypatch):
    def handler(request):
        assert request.headers["authorization"] == "Bearer st-access-1"
        return httpx.Response(
            200,
            json={"id": 1, "firstname": "Maciek"},
            headers={
                "X-ReadRateLimit-Usage": "5,60",
                "X-ReadRateLimit-Limit": "100,1000",
            },
            request=request,
        )

    _install_transport(monkeypatch, handler)
    _install_store(monkeypatch, STORE)
    result = client.api_get("/athlete")
    assert result["id"] == 1
    assert rate.STATE.short == {"used": 5, "limit": 100}
    assert rate.STATE.daily == {"used": 60, "limit": 1000}


def test_401_refreshes_once_and_retries(monkeypatch):
    seen_tokens = []

    def handler(request):
        seen_tokens.append(request.headers["authorization"])
        if len(seen_tokens) == 1:
            return httpx.Response(401, json={"message": "Unauthorized"}, request=request)
        return httpx.Response(
            200,
            json={"id": 1},
            headers={"X-ReadRateLimit-Usage": "1,10"},
            request=request,
        )

    _install_transport(monkeypatch, handler)
    refreshed = {}

    def fake_refresh(path=None):
        refreshed["called"] = True
        return dict(STORE, access_token="st-access-2")

    monkeypatch.setattr(oauth, "refresh_access_token", fake_refresh)
    _install_store(monkeypatch, STORE)

    result = client.api_get("/athlete")
    assert refreshed["called"] is True
    assert seen_tokens == [
        "Bearer st-access-1",
        "Bearer st-access-2",
    ]
    assert result["id"] == 1


def test_second_401_surfaces_as_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"message": "Unauthorized"}, request=request)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(
        oauth, "refresh_access_token", lambda path=None: dict(STORE)
    )
    _install_store(monkeypatch, STORE)
    with pytest.raises(client.StravaAPIError) as exc:
        client.api_get("/athlete")
    assert exc.value.status == 401


def test_429_returns_wait_instead_of_retrying(monkeypatch):
    made = []

    def handler(request):
        made.append(request)
        return httpx.Response(
            429, json={"message": "Too Many Requests"}, request=request
        )

    _install_transport(monkeypatch, handler)
    _install_store(monkeypatch, STORE)
    with pytest.raises(rate.RateGuardError) as exc:
        client.api_get("/athlete")
    assert len(made) == 1  # no internal retry: the agent decides (section 3.4)
    assert "quarter_hour_seconds" in exc.value.wait
    assert "daily_seconds" in exc.value.wait


def test_upstream_4xx_carries_status_and_body(monkeypatch):
    def handler(request):
        return httpx.Response(
            403, json={"message": "Forbidden", "errors": []}, request=request
        )

    _install_transport(monkeypatch, handler)
    _install_store(monkeypatch, STORE)
    with pytest.raises(client.StravaAPIError) as exc:
        client.api_get("/athlete")
    assert exc.value.status == 403
    assert "Forbidden" in exc.value.body
