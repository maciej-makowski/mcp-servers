"""Sync engine mechanics (strava-local-store A4): pacing, tiers, the
trigger-only backfill, guard stop-and-resume, the reserve under
interleaving, and the strava_sync tool surface. No network in tests
(house rule): the Strava client and the CDN photo fetch are faked."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from strava_mcp import client, oauth, rate, store as store_mod, sync as sync_mod, tools
from strava_mcp.sync import Pacer, SyncEngine
from strava_mcp.store import Store, utc_now_iso


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    root = str(tmp_path / "strava-store")
    monkeypatch.setattr(store_mod, "STORE_DIR", root)
    return root


@pytest.fixture
def engine(store_root):
    return SyncEngine(store_root)


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(sync_mod, "SLEEP_FN", lambda s: sleeps.append(s))
    return sleeps


@pytest.fixture
def no_photo_net(monkeypatch):
    class _Resp:
        status_code = 200
        content = b"photo-bytes"

    monkeypatch.setattr(sync_mod.httpx, "get", lambda *a, **kw: _Resp())


def _enable(monkeypatch, value="true"):
    monkeypatch.setenv("STRAVA_SYNC_ENABLED", value)


def _summary(aid, name="Run", start="2026-11-07T10:57:57+00:00", **over):
    base = {
        "id": aid, "name": name, "sport_type": "Run", "start_date": start,
        "start_date_local": start, "distance": 2742.0, "moving_time": 324,
        "elapsed_time": 341, "total_elevation_gain": 12.0,
        "average_speed": 8.4, "max_speed": 9.9, "private": False,
        "gear_id": "b1", "total_photo_count": 0, "kudos_count": 0,
        "comment_count": 0, "achievement_count": 0, "workout_type": None,
        "external_id": None, "upload_id": None,
    }
    base.update(over)
    return base


def _streams_payload():
    return {
        k: {"type": k, "data": [0, 1, 2]} for k in (
            "time", "latlng", "distance", "altitude", "velocity_smooth",
            "heartrate", "cadence", "watts", "temp", "moving", "grade_smooth")
    }


def _detail(aid):
    return {"id": aid, "name": "Run", "visibility": "everyone",
            "photos": {"primary": {"urls": {"600": "https://cdn/x.jpg"}}}}


class FakeStrava:
    """Canned api_get/api_get_bytes by path prefix; records every call."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.routes: dict[str, object] = {}
        monkeypatch.setattr(client, "api_get", self)
        monkeypatch.setattr(client, "api_get_bytes", self)

    def add(self, prefix, fn):
        self.routes[prefix] = fn

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        for prefix, fn in self.routes.items():
            if path.startswith(prefix):
                result = fn(params) if callable(fn) else fn
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected path {path!r}")


def test_enabled_default_false(monkeypatch):
    assert sync_mod.enabled() is False
    _enable(monkeypatch)
    assert sync_mod.enabled() is True
    monkeypatch.setenv("STRAVA_SYNC_ENABLED", "0")
    assert sync_mod.enabled() is False


def test_pacer_caps_per_quarter(no_sleep):
    pacer = Pacer(2)
    pacer.gate()
    pacer.gate()
    assert no_sleep == []
    pacer.gate()  # third request in the same quarter: sleeps to the boundary
    assert len(no_sleep) == 1 and no_sleep[0] > 0
    assert pacer.count == 1  # reset after the boundary sleep


def test_scheduler_never_cold_starts_backfill(engine, monkeypatch):
    _enable(monkeypatch)
    fake = FakeStrava(monkeypatch)  # nothing registered: any Strava call fails
    result = engine.tick()
    assert result["tier"] == "frequent"
    assert result["outcome"] == "no_op"  # empty store: the frequent tier no-ops
    assert fake.calls == []  # the backfill was never started on its own


def test_trigger_enqueues_backfill_and_completes(engine, monkeypatch, no_sleep,
                                                 no_photo_net):
    _enable(monkeypatch)
    fake = FakeStrava(monkeypatch)
    fake.add("/athlete/activities", lambda params: [_summary(1), _summary(2)])
    fake.add("/athlete", lambda params: {"id": 42})
    fake.add("/athletes/42/stats", lambda params: {"recent_run_totals": {}})

    def detail_or_streams(params=None):
        return _detail(1)

    fake.add("/activities/1/streams", lambda params: _streams_payload())
    fake.add("/activities/2/streams", lambda params: _streams_payload())
    fake.add("/activities/", lambda params: _detail(1))

    queued = engine.enqueue("backfill")
    assert queued["queued"] is True
    result = engine.tick()
    assert result["tier"] == "backfill"
    assert result["outcome"] == "done"
    assert result["added"] == 2
    store = Store()
    counts = store.counts()
    assert counts["activities"] == 2
    assert counts["with_details"] == 2 and counts["with_streams"] == 2
    assert counts["with_photos"] == 2
    assert store.get_state("backfill_cursor") == ""  # complete: cursor cleared
    assert store.get_state("backfill_walk_complete") == "1"
    assert store.get_state("frequent_watermark") == _summary(2)["start_date"]


def test_backfill_rate_stop_persists_and_resumes(engine, monkeypatch, no_sleep,
                                                 no_photo_net):
    _enable(monkeypatch)
    fake = FakeStrava(monkeypatch)
    fake.add("/athlete/activities", lambda params: [_summary(1), _summary(2)])
    fake.add("/activities/2/streams", lambda params: _streams_payload())
    fail = {"on": False}

    def detail_router(params):
        if fail["on"]:
            return rate.RateGuardError(
                "strava rate budget at reserve",
                wait={"quarter_hour_seconds": 1, "daily_seconds": 1,
                      "quarter_hour_reset": "x", "daily_reset": "y"},
            )
        return _detail(1)

    fake.add("/activities/", detail_router)

    fail["on"] = True  # the reserve trips on the very first detail fetch
    first = engine.run_batch("backfill")
    assert first["outcome"] == "rate_stopped"
    store = Store()
    assert store.counts()["with_details"] == 0  # nothing processed yet
    assert store.get_state("backfill_cursor") is None
    assert store.get_state("backfill_walk_complete") == "1"  # the walk survived

    fail["on"] = False  # the next slot: same engine, no manual re-trigger
    second = engine.run_batch("backfill")
    assert second["outcome"] == "done"
    counts = store.counts()
    assert counts["with_details"] == 2 and counts["with_streams"] == 2
    assert store.get_state("backfill_cursor") == ""


def test_bulk_cap_sleeps_mid_batch(engine, monkeypatch, no_sleep, no_photo_net):
    _enable(monkeypatch)
    monkeypatch.setenv("STRAVA_SYNC_BULK_CAP", "2")
    fake = FakeStrava(monkeypatch)
    fake.add("/athlete/activities",
             lambda params: [_summary(1), _summary(2), _summary(3)])
    fake.add("/athlete", lambda params: {"id": 42})
    fake.add("/athletes/42/stats", lambda params: {})
    fake.add("/activities/1/streams", lambda params: _streams_payload())
    fake.add("/activities/2/streams", lambda params: _streams_payload())
    fake.add("/activities/3/streams", lambda params: _streams_payload())
    fake.add("/activities/", lambda params: _detail(1))
    result = engine.run_batch("backfill")
    assert result["outcome"] == "done"
    assert len(no_sleep) >= 3  # 7 requests against a cap of 2: boundary sleeps
    assert result["api_requests"] == 7  # walk + 3x(detail+streams); no athlete/stats in the backfill tier


def test_frequent_walk_stops_at_known_unchanged(engine, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(oauth, "load_store",
                        lambda path=None: {"access_token": "t", "athlete": {"id": 42}})
    fake = FakeStrava(monkeypatch)
    fake.add("/athlete/activities", lambda params: [_summary(1), _summary(2)])
    fake.add("/athlete", lambda params: {"id": 42})
    fake.add("/athletes/42/stats", lambda params: {})
    store = Store()
    store.init_schema()
    now = utc_now_iso()
    for aid in (1, 2):
        store.upsert_activity_summary(_summary(aid), now=now)
        store.save_detail(aid, _detail(aid), now=now)
        store.save_streams(aid, _streams_payload(), now=now)
    result = engine.run_batch("frequent")
    assert result["outcome"] == "done"
    list_calls = [c for c in fake.calls if c[0].startswith("/athlete/activities")]
    assert len(list_calls) == 1  # known-unchanged page: the walk stops
    detail_calls = [c for c in fake.calls if c[0].startswith("/activities/")]
    assert detail_calls == []  # nothing new to fetch
    assert store.get_state("frequent_watermark") == _summary(2)["start_date"]


def test_sweep_marks_deletion_and_refetches_drift(engine, monkeypatch,
                                                  no_photo_net):
    _enable(monkeypatch)
    fake = FakeStrava(monkeypatch)
    fake.add("/athlete/activities",
             lambda params: [_summary(1, name="Renamed upstream")])
    fake.add("/activities/1/streams", lambda params: _streams_payload())
    fake.add("/activities/", lambda params: _detail(1))
    store = Store()
    store.init_schema()
    now = utc_now_iso()
    for aid in (1, 2):
        store.upsert_activity_summary(_summary(aid), now=now)
        store.save_detail(aid, _detail(aid), now=now)
        store.save_streams(aid, _streams_payload(), now=now)
    result = engine.run_batch("sweep")
    assert result["outcome"] == "done"
    assert result["deleted"] == 1
    assert store.activity_row(2)["deleted_at"] is not None
    assert os.path.isfile(os.path.join(store.root, "activities", "2",
                                       "summary.json"))  # D10: file kept
    refetched = [c for c in fake.calls if c[0].startswith("/activities/1")]
    assert len(refetched) >= 2  # the drifted summary was refetched
    assert store.activity_row(1)["deleted_at"] is None


def test_slow_skips_scope_without_read_all(engine, monkeypatch):
    _enable(monkeypatch)
    fake = FakeStrava(monkeypatch)
    fake.add("/athlete/zones", lambda params: {"heart_rate": {}})
    fake.add("/gear/", lambda params: {"id": "b1", "name": "Voodoo"})
    store = Store()
    store.init_schema()
    store.upsert_activity_summary(_summary(1, gear_id="b1"))
    result = engine.run_batch("slow")
    assert result["outcome"] == "done"
    paths = [c[0] for c in fake.calls]
    assert "/athlete/zones" in paths and "/gear/b1" in paths
    assert not any("/segments" in p or "/routes" in p for p in paths)


def test_sync_status_disabled_and_empty(monkeypatch, store_root):
    Store().init_schema()
    result = tools.SYNC_TOOL.dispatch("status", {})
    assert result["success"] is True and result["api_call"] is False
    assert result["sync_enabled"] is False  # rollout default (A1)
    assert result["store_counts"]["activities"] == 0
    assert result["rate"]["15_minute"]["reserve"] == 10


def test_sync_trigger_warns_while_disabled(store_root):
    result = tools.SYNC_TOOL.dispatch("trigger", {"tier": "backfill"})
    assert result["success"] is True and result["queued"] is True
    assert result["sync_enabled"] is False
    assert "STRAVA_SYNC_ENABLED" in result["warning"]


def test_streams_request_carries_csv_with_fixed_heartrate(monkeypatch):
    seen: dict = {}

    def fake_api_get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return _streams_payload()

    monkeypatch.setattr(client, "api_get", fake_api_get)
    result = tools.ACTIVITIES_TOOL.dispatch("streams", {"id": 5})
    assert result["success"] is True
    assert seen["params"]["keys"] == (
        "time,latlng,distance,altitude,velocity_smooth,heartrate,cadence,"
        "watts,temp,moving,grade_smooth"
    )  # CSV string, heartrate spelled right (the under-fetch fix)
    assert set(result["streams"]) == set(sync_mod.tools_stream_keys())


def test_store_source_paths(monkeypatch, store_root):
    store = Store()
    store.init_schema()
    store.upsert_activity_summary(_summary(1))
    store.save_detail(1, _detail(1))
    store.save_streams(1, _streams_payload())
    get_result = tools.ACTIVITIES_TOOL.dispatch(
        "get", {"id": 1, "source": "store"})
    assert get_result == _detail(1)  # the stored payload, zero API calls
    miss = tools.ACTIVITIES_TOOL.dispatch("get", {"id": 9, "source": "store"})
    assert miss["success"] is False and miss["store_miss"] is True
    streams = tools.ACTIVITIES_TOOL.dispatch(
        "streams", {"id": 1, "source": "store"})
    assert streams["success"] is True and streams["max_points"] == 1000
    listing = tools.ACTIVITIES_TOOL.dispatch("list", {"source": "store"})
    assert listing["source"] == "store" and listing["count"] == 1


def test_store_source_empty_store(monkeypatch, store_root):
    Store().init_schema()
    result = tools.ACTIVITIES_TOOL.dispatch("list", {"source": "store"})
    assert result["success"] is False and result["store_empty"] is True
    assert "strava_sync" in result["error"]


def test_reserve_stops_batch_and_tools_refuse(engine, monkeypatch, no_sleep,
                                              no_photo_net):
    """The guard's reserve under interleaving: the sync stops mid-batch, the
    structured rate_limited shape reaches the tool layer (A4 rule 4)."""
    _enable(monkeypatch)
    monkeypatch.setattr(oauth, "load_store",
                        lambda path=None: {"access_token": "t", "athlete": {"id": 42}})
    usage = {"short": 80, "daily": 0}
    rate.STATE.short = {"used": 80, "limit": 100}
    rate.STATE.daily = {"used": 0, "limit": 1000}

    def handler(request: httpx.Request) -> httpx.Response:
        usage["short"] += 3
        usage["daily"] += 3
        path = request.url.path
        headers = {
            "X-ReadRateLimit-Usage": f"{usage['short']},{usage['daily']}",
            "X-ReadRateLimit-Limit": "100,1000",
            "content-type": "application/json",
        }
        if path == "/api/v3/athlete/activities":
            body = [_summary(1), _summary(2), _summary(3)]
        elif path.endswith("/streams"):
            body = _streams_payload()
        elif path.startswith("/api/v3/activities/"):
            body = _detail(1)
        elif path == "/api/v3/athlete":
            body = {"id": 42}
        else:
            body = {}
        return httpx.Response(200, json=body, headers=headers)

    monkeypatch.setattr(
        client, "_client",
        lambda: httpx.Client(
            base_url=client.BASE_URL,
            transport=httpx.MockTransport(handler),
        ),
    )
    store = Store()
    store.init_schema()
    for aid in (1, 2, 3):
        store.upsert_activity_summary(_summary(aid))
    # a tool call before the batch: remaining 20 > 10 reserve, allowed
    ok = tools.ATHLETE_TOOL.dispatch("get", {})
    assert ok["id"] == 42
    result = engine.run_batch("backfill")
    assert result["outcome"] == "rate_stopped"
    assert result["note"] and "reserve" in result["note"]
    # after the stop the bucket sits at/below the reserve: tools refuse too
    limited = tools.ATHLETE_TOOL.dispatch("get", {})
    assert limited["success"] is False and limited["rate_limited"] is True
    processed = Store().counts()
    assert processed["with_details"] >= 1  # partial progress persisted
