"""Tool behaviour with client call sites mocked at module level."""
import datetime as dt
import hashlib
import json
import time
import types

import pytest

from garmin_mcp import client, tools

NOW = dt.date(2026, 11, 7)  # Saturday


class FakeDate(dt.date):
    @classmethod
    def today(cls):
        return NOW


class FakeToken:
    def __init__(self, expires_at):
        self.access_token = "t"
        self.expires_at = expires_at


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    tools.CACHE.clear()
    monkeypatch.setattr(tools.CACHE, "_clock", lambda: 0.0)
    client._FAMILY_ERRORS.clear()
    import garmin_mcp.cache as cache_mod

    monkeypatch.setattr(cache_mod.time, "sleep", lambda s: None)  # no real backoff waits
    yield
    tools.CACHE.clear()
    client._FAMILY_ERRORS.clear()


def test_recent_activities_shape_and_mapping(monkeypatch):
    captured = {}

    def fake(limit, since):
        captured["limit"], captured["since"] = limit, since
        return [{"activityId": 1, "activityName": "Run"}]

    monkeypatch.setattr(client, "search_activities", fake)
    out = tools.recent_activities(limit=5, since="2026-11-01")
    assert captured == {"limit": 5, "since": "2026-11-01"}
    assert out == {"activities": [{"activityId": 1, "activityName": "Run"}]}
    json.dumps(out)


def test_recent_activities_empty_since_maps_to_empty(monkeypatch):
    captured = {}

    def fake(limit, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(client, "search_activities", fake)
    assert tools.recent_activities() == {"activities": []}
    assert captured["since"] == ""


def test_recent_activities_limit_clamped(monkeypatch):
    captured = {}

    def fake(limit, since):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(client, "search_activities", fake)
    tools.recent_activities(limit=500)
    assert captured["limit"] == 100


def test_cache_ttl_expiry_through_tools(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(tools.CACHE, "_clock", lambda: now[0])
    calls = []

    def fake(limit, since):
        calls.append((limit, since))
        return [{"id": 1}]

    monkeypatch.setattr(client, "search_activities", fake)
    tools.recent_activities(limit=5)
    tools.recent_activities(limit=5)
    assert len(calls) == 1  # second call served from cache
    now[0] += 601  # past the 600s TTL
    tools.recent_activities(limit=5)
    assert len(calls) == 2  # expired -> refetched


def test_activity_tool(monkeypatch):
    monkeypatch.setattr(client, "get_activity", lambda aid: {"activityId": int(aid)})
    assert tools.activity("42") == {"activity": {"activityId": 42}}


def test_activity_file_summary_only(monkeypatch):
    blob = b"PK\x03\x04-fake-zip-bytes"
    monkeypatch.setattr(client, "download_activity", lambda aid: blob)
    out = tools.activity_file("12345")
    assert out["activity_id"] == "12345"
    assert out["size_bytes"] == len(blob)
    assert out["sha256"] == hashlib.sha256(blob).hexdigest()
    assert out["content_type"] == "application/zip"
    assert "data" not in out and "bytes" not in out  # never returns the payload


def test_activity_file_rejects_bad_id():
    with pytest.raises(ValueError):
        tools.activity_file("not-an-id")


def test_workout_usage(monkeypatch):
    monkeypatch.setattr(client, "get_activity_workouts", lambda aid: [{"workoutId": 9}])
    assert tools.workout_usage("42") == {"workouts": [{"workoutId": 9}]}


def test_daily_splits_ranged_families_by_cap(monkeypatch):
    calls = []

    def fake_ranged(family, start, end, *, chunks=None):
        calls.append((family, start.isoformat(), end.isoformat(), chunks))
        return [{"calendarDate": start.isoformat(), "value": 1}]

    monkeypatch.setattr(client, "fetch_ranged", fake_ranged)
    out = tools.daily("2026-10-01", "2026-11-15", families="steps")
    steps = out["families"]["steps"]
    assert [(c["start"], c["end"]) for c in steps["chunks"]] == [
        ("2026-10-01", "2026-10-28"),
        ("2026-10-29", "2026-11-15"),
    ]
    assert steps["present"] is True
    assert out["errors"] == {}


def test_daily_perday_fanout_one_call_per_day(monkeypatch):
    calls = []

    def fake_perday(family, day):
        calls.append((family, day.isoformat()))
        return {"date": day.isoformat()}

    monkeypatch.setattr(client, "fetch_perday", fake_perday)
    out = tools.daily("2026-11-01", "2026-11-03", families="spo2,heart_rate,fitness_age")
    assert out["families"]["spo2"]["days"][0]["date"] == "2026-11-01"
    assert out["families"]["heart_rate"]["present"] is True
    assert len(calls) == 9  # 3 families x 3 days
    assert out["errors"] == {}


def test_daily_perday_fanout_guard(monkeypatch):
    out = tools.daily("2026-01-01", "2026-05-01", families="spo2")
    assert "spo2" in out["errors"]
    assert out["families"]["spo2"] == {"present": False}


def test_daily_body_battery_chunks_passthrough(monkeypatch):
    captured = {}

    def fake_ranged(family, start, end, *, chunks=None):
        captured["chunks"] = chunks
        return []

    monkeypatch.setattr(client, "fetch_ranged", fake_ranged)
    tools.daily("2026-11-01", "2026-11-07", families="body_battery", body_battery_chunks=3)
    assert captured["chunks"] == 3
    tools.daily("2026-11-01", "2026-11-07", families="body_battery")
    assert captured["chunks"] is None


def test_daily_unknown_family_rejected():
    with pytest.raises(ValueError):
        tools.daily("2026-11-01", "2026-11-02", families="nutrition")


def test_daily_inverted_range_rejected():
    with pytest.raises(ValueError):
        tools.daily("2026-11-05", "2026-11-01")


def test_daily_one_family_error_does_not_sink_the_rest(monkeypatch):
    class ApiDown(Exception):
        status_code = 500

    def fake_ranged(family, start, end, *, chunks=None):
        if family == "steps":
            raise ApiDown("HTTP 500")
        return [{"calendarDate": start.isoformat()}]

    monkeypatch.setattr(client, "fetch_ranged", fake_ranged)
    out = tools.daily("2026-11-01", "2026-11-03", families="steps,hrv")
    assert "steps" in out["errors"]
    assert out["families"]["hrv"]["present"] is True
    errs = client.family_errors()
    assert errs["steps"]["status_code"] == 500
    assert "hrv" not in errs


def test_daily_all_families_covers_every_endpoint(monkeypatch):
    ranged, perday = [], []

    def fake_ranged(family, start, end, *, chunks=None):
        ranged.append(family)
        return []

    def fake_perday(family, day):
        perday.append((family, day.isoformat()))
        return {}

    monkeypatch.setattr(client, "fetch_ranged", fake_ranged)
    monkeypatch.setattr(client, "fetch_perday", fake_perday)
    out = tools.daily("2026-11-01", "2026-11-02")
    assert sorted(ranged) == sorted(
        ["stress", "steps", "hydration", "weight", "sleep", "body_battery", "daily", "hrv"])
    assert sorted(set(f for f, _ in perday)) == sorted(
        ["spo2", "fitness_age", "heart_rate"])
    assert out["errors"] == {}
    assert len(perday) == 6  # 3 per-day families x 2 days


def test_trend_weekly_aggregation(monkeypatch):
    monkeypatch.setattr(tools, "dt", types.SimpleNamespace(date=FakeDate, timedelta=dt.timedelta))

    def fake_ranged(family, start, end, *, chunks=None):
        assert family == "stress"
        return [
            {"calendarDate": "2026-11-02", "stressLevel": 10, "note": "x"},
            {"calendarDate": "2026-11-03", "stressLevel": 20},
            {"calendarDate": "2026-10-26", "stressLevel": 5},
        ]

    monkeypatch.setattr(client, "fetch_ranged", fake_ranged)
    out = tools.trend("stress", weeks=2)
    assert out["family"] == "stress" and out["weeks"] == 2
    buckets = out["buckets"]
    assert [b["week_start"] for b in buckets] == ["2026-10-26", "2026-11-02"]
    assert buckets[0]["mean"]["stressLevel"] == 5.0
    assert buckets[1]["mean"]["stressLevel"] == 15.0
    assert buckets[1]["days"] == 2
    assert "note" not in buckets[1]["mean"]  # non-numeric fields skipped


def test_trend_rejects_perday_family():
    with pytest.raises(ValueError):
        tools.trend("heart_rate", weeks=2)


def test_api_status_reports_token_expiry(monkeypatch):
    monkeypatch.setenv("GARMIN_API_KEY", "k")
    expires = time.time() + 3600
    monkeypatch.setattr(client, "obtain_bearer_token", lambda: FakeToken(expires))
    out = tools.api_status()
    assert out["api_key_configured"] is True
    assert out["token"]["valid"] is True
    assert 3500 <= out["token"]["seconds_remaining"] <= 3600
    assert out["families"] == {}
    assert out["cache"]["ttl_seconds"] == 600
    assert "sync" not in json.dumps(out)  # no mirror in v1: nothing sync-flavoured


def test_api_status_missing_key(monkeypatch):
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    out = tools.api_status()
    assert out["api_key_configured"] is False
    assert out["token"]["valid"] is False


def test_api_status_reports_family_errors(monkeypatch):
    monkeypatch.setenv("GARMIN_API_KEY", "k")
    client.record_family_error("sleep", RuntimeError("boom"))
    try:
        monkeypatch.setattr(
            client, "obtain_bearer_token", lambda: FakeToken(time.time() + 60))
        out = tools.api_status()
        assert out["families"]["sleep"]["error"] == "boom"
    finally:
        client.record_family_ok("sleep")
