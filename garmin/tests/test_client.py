"""Wrapper arg mapping to the official client (official client never touched)."""
import datetime as dt

import pytest

from garmin_mcp import client as gclient

D = dt.date


class FakeToken:
    def __init__(self, name="tok-1"):
        self.access_token = name
        self.expires_at = 4102444800.0  # far future


class FakeAuthenticator:
    instances = []

    def __init__(self, **kw):
        self.calls = 0
        FakeAuthenticator.instances.append(self)

    def obtain_token(self, key, *, force_refresh=False):
        self.calls += 1
        self.key = key
        return FakeToken(f"tok-{self.calls}")


class FakeApiClient:
    instances = []

    def __init__(self, bearer_token):
        self.bearer_token = bearer_token
        self.tokens = [bearer_token]
        FakeApiClient.instances.append(self)

    def update_bearer_token(self, token):
        self.bearer_token = token
        self.tokens.append(token)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("GARMIN_API_KEY", "test-key")
    gclient._reset_state()
    FakeAuthenticator.instances.clear()
    FakeApiClient.instances.clear()
    monkeypatch.setattr(gclient, "authenticator_cls", FakeAuthenticator)
    monkeypatch.setattr(gclient, "api_client_cls", FakeApiClient)
    yield
    gclient._reset_state()


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    with pytest.raises(gclient.GarminAuthError):
        gclient.obtain_bearer_token()


def test_token_exchange_uses_env_key():
    token = gclient.obtain_bearer_token()
    auth = FakeAuthenticator.instances[-1]
    assert auth.key == "test-key"
    assert token.access_token == "tok-1"


def test_api_client_reuses_and_renews_token():
    c1 = gclient.api_client()
    c2 = gclient.api_client()
    assert c1 is c2
    assert c1.tokens == ["tok-1", "tok-2"]


def test_search_activities_wrapper(monkeypatch):
    captured = {}

    class C(FakeApiClient):
        def search_activities(self, *, start, limit, start_date):
            captured.update(start=start, limit=limit, start_date=start_date)
            return [{"activityId": 1}]

    monkeypatch.setattr(gclient, "api_client_cls", C)
    out = gclient.search_activities(limit=7, since="2026-11-01")
    assert out == [{"activityId": 1}]
    assert captured == {"start": 0, "limit": 7, "start_date": "2026-11-01"}


def test_search_activities_blank_since_maps_to_none(monkeypatch):
    captured = {}

    class C(FakeApiClient):
        def search_activities(self, *, start, limit, start_date):
            captured["start_date"] = start_date
            return []

    monkeypatch.setattr(gclient, "api_client_cls", C)
    gclient.search_activities(limit=5, since="")
    assert captured["start_date"] is None


def test_activity_and_file_wrappers(monkeypatch):
    calls = []

    class C(FakeApiClient):
        def get_activity(self, activity_id):
            calls.append(("get_activity", activity_id))
            return {"activityId": 123}

        def download_activity(self, activity_id):
            calls.append(("download", activity_id))
            return b"ZIPBYTES"

        def get_activity_workouts(self, activity_id):
            calls.append(("workouts", activity_id))
            return [{"workoutId": 9}]

    monkeypatch.setattr(gclient, "api_client_cls", C)
    assert gclient.get_activity("123") == {"activityId": 123}
    assert gclient.download_activity("123") == b"ZIPBYTES"
    assert gclient.get_activity_workouts("123") == [{"workoutId": 9}]
    assert calls == [("get_activity", "123"), ("download", "123"),
                     ("workouts", "123")]


def test_ranged_family_dispatch(monkeypatch):
    calls = []

    class C(FakeApiClient):
        def get_daily_stress(self, s, e):
            calls.append(("stress", s, e))
            return [{"x": 1}]

        def get_daily_steps(self, s, e):
            calls.append(("steps", s, e))
            return []

        def get_daily_hydration(self, s, e):
            calls.append(("hydration", s, e))
            return []

        def get_weight_measurements(self, s, e):
            calls.append(("weight", s, e))
            return []

        def get_daily_sleep_summaries(self, s, e):
            calls.append(("sleep", s, e))
            return {}

        def get_daily_body_battery(self, s, e, *, chunks=None):
            calls.append(("body_battery", s, e, chunks))
            return []

        def get_daily_summaries(self, s, e):
            calls.append(("daily", s, e))
            return {"data": []}

        def get_daily_hrv_summaries(self, s, e):
            calls.append(("hrv", s, e))
            return []

    monkeypatch.setattr(gclient, "api_client_cls", C)
    s, e = D(2026, 11, 1), D(2026, 11, 7)
    for family in ("stress", "steps", "hydration", "weight", "sleep", "daily", "hrv"):
        gclient.fetch_ranged(family, s, e)
    gclient.fetch_ranged("body_battery", s, e, chunks=3)
    gclient.fetch_ranged("body_battery", s, e)
    assert calls == [
        ("stress", s, e), ("steps", s, e), ("hydration", s, e),
        ("weight", s, e), ("sleep", s, e), ("daily", s, e), ("hrv", s, e),
        ("body_battery", s, e, 3), ("body_battery", s, e, None),
    ]


def test_unknown_ranged_family_raises():
    with pytest.raises(ValueError):
        gclient.fetch_ranged("nutrition", D(2026, 11, 1), D(2026, 11, 2))


def test_perday_family_dispatch(monkeypatch):
    calls = []

    class C(FakeApiClient):
        def get_daily_spo2(self, day):
            calls.append(("spo2", day))
            return {}

        def get_fitness_age(self, day):
            calls.append(("fitness_age", day))
            return {}

        def get_daily_heart_rate(self, day):
            calls.append(("heart_rate", day))
            return {}

    monkeypatch.setattr(gclient, "api_client_cls", C)
    day = D(2026, 11, 7)
    for family in ("spo2", "fitness_age", "heart_rate"):
        gclient.fetch_perday(family, day)
    assert calls == [("spo2", day), ("fitness_age", day), ("heart_rate", day)]


def test_unknown_perday_family_raises():
    with pytest.raises(ValueError):
        gclient.fetch_perday("sleep", D(2026, 11, 7))


def test_family_error_recording():
    gclient.record_family_ok("steps")
    assert gclient.family_errors() == {}

    class HttpLike(Exception):
        status_code = 429

    gclient.record_family_error("steps", HttpLike("HTTP 429"))
    errs = gclient.family_errors()
    assert errs["steps"]["status_code"] == 429
    assert "ts" in errs["steps"] and errs["steps"]["error"] == "HTTP 429"
    gclient.record_family_ok("steps")
    assert gclient.family_errors() == {}
