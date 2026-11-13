"""Read-through path (design §3.5, §5): envelope fidelity against the live
captures, zero-upstream freshness, live fallback + write-through, staleness,
the activity gate, and the api_status store block."""
import datetime as dt
import json

import pytest

import garmin_mcp.cache as cache_mod
from garmin_mcp import client, readpath, sync, tools
from storekit import (CAPTURE_DAY, FakeClient, FakeStatusException, FakeToken,
                      load_fixture, make_store, seed_perday, seed_ranged_chunk)

STEP_START = dt.date(2026, 10, 15)   # the 28-day cap-window piece of the capture
STEP_END = dt.date(2026, 11, 11)
STEP_TAIL = dt.date(2026, 11, 12)
WINDOW_END = CAPTURE_DAY             # every capture ends on this complete day


@pytest.fixture()
def clean_tools(monkeypatch):
    monkeypatch.setattr(cache_mod.time, "sleep", lambda s: None)
    monkeypatch.delenv("GARMIN_STORE_DIR", raising=False)
    monkeypatch.delenv("GARMIN_STORE_MODE", raising=False)
    client._FAMILY_ERRORS.clear()
    tools.CACHE.clear()
    yield
    client._FAMILY_ERRORS.clear()
    tools.CACHE.clear()
    readpath.reset_for_tests(None, "off")


@pytest.fixture()
def fake(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(client, "search_activities", fake.search_activities)
    monkeypatch.setattr(client, "search_activities_page", fake.search_activities_page)
    monkeypatch.setattr(client, "get_activity", fake.get_activity)
    monkeypatch.setattr(client, "get_activity_workouts", fake.get_activity_workouts)
    monkeypatch.setattr(client, "download_activity", fake.download_activity)
    monkeypatch.setattr(client, "fetch_ranged", fake.fetch_ranged)
    monkeypatch.setattr(client, "fetch_perday", fake.fetch_perday)
    monkeypatch.setattr(client, "fetch_ranged_archive", fake.fetch_ranged_archive)
    monkeypatch.setattr(client, "fetch_perday_archive", fake.fetch_perday_archive)
    monkeypatch.setattr(client, "fetch_point", fake.fetch_point)
    monkeypatch.setattr(client, "obtain_bearer_token",
                        lambda: FakeToken(_now() + 9999))
    return fake


def _now() -> float:
    import time

    return time.time()


def seeded_store(tmp_path, anchor: dt.date):
    return make_store(tmp_path, anchor=anchor)


# ------------------------------------------------------------- daily fidelity

RANGED_FIXTURES = [
    ("steps", "daily_steps.json", dt.date(2026, 10, 15)),
    ("hrv", "daily_hrv.json", dt.date(2026, 11, 11)),
    ("sleep", "daily_sleep.json", dt.date(2026, 11, 10)),
    ("stress", "daily_stress.json", dt.date(2026, 11, 10)),
    ("hydration", "daily_hydration.json", dt.date(2026, 11, 10)),
    ("body_battery", "daily_body_battery.json", dt.date(2026, 11, 11)),
    ("daily", "daily_daily.json", dt.date(2026, 11, 11)),
]


@pytest.mark.parametrize("family,fixture,window_start", RANGED_FIXTURES)
def test_daily_readthrough_matches_live_capture(tmp_path, clean_tools, fake,
                                                family, fixture, window_start):
    """The read path replays the API's own bytes for the captured window with
    ZERO upstream calls — byte-faithful per §2.3/§5.2."""
    payload = load_fixture(fixture)
    store = make_store(tmp_path, anchor=window_start)
    from garmin_mcp import ranges
    from garmin_mcp.families import chunk_for_day

    cap = ranges.RANGED_FAMILY_CAPS[family]
    for piece in payload["families"][family]["chunks"]:
        cs, ce = chunk_for_day(window_start, cap,
                               dt.date.fromisoformat(piece["end"]))
        seed_ranged_chunk(store, family, cs, ce, piece["data"])
    readpath.reset_for_tests(store, "read_through")
    fake.ranged_calls.clear()
    out = tools.daily(payload["start_date"], payload["end_date"], families=family)
    assert out["errors"] == {}
    assert out["families"][family] == payload["families"][family]
    assert out["start_date"] == payload["start_date"]
    assert out["end_date"] == payload["end_date"]
    assert fake.ranged_calls == []  # zero upstream calls (§5.2)
    assert fake.perday_calls == []


def test_daily_readthrough_slices_subwindow(tmp_path, clean_tools, fake):
    payload = load_fixture("daily_steps.json")
    store = make_store(tmp_path, anchor=STEP_START)
    piece = payload["families"]["steps"]["chunks"][0]
    seed_ranged_chunk(store, "steps", STEP_START, STEP_END, piece["data"])
    readpath.reset_for_tests(store, "read_through")
    out = tools.daily("2026-11-07", "2026-11-07", families="steps")
    block = out["families"]["steps"]
    assert block["chunks"] == [{
        "start": "2026-11-07", "end": "2026-11-07",
        "data": [{"calendarDate": "2026-11-07", "totalSteps": 8518,
                  "totalDistance": 6974, "stepGoal": 8690}],
    }]
    assert block["present"] is True
    assert fake.ranged_calls == []


def test_daily_weight_full_chunk_window_replays_raw(tmp_path, clean_tools, fake):
    """Weight's aggregate dict shape is not date-sliceable: the read path
    replays it raw only when the request equals the stored chunk window."""
    payload = load_fixture("daily_weight.json")
    window_start = dt.date(2026, 11, 10)
    chunk_end = window_start + dt.timedelta(days=29)  # cap 30
    store = make_store(tmp_path, anchor=window_start)
    seed_ranged_chunk(store, "weight", window_start, chunk_end,
                      payload["families"]["weight"]["chunks"][0]["data"])
    readpath.reset_for_tests(store, "read_through")
    out = tools.daily("2026-11-10", chunk_end.isoformat(), families="weight")
    assert out["families"]["weight"]["chunks"][0]["data"] == \
        payload["families"]["weight"]["chunks"][0]["data"]
    assert fake.ranged_calls == []


def test_daily_weight_subwindow_falls_back_live(tmp_path, clean_tools, fake):
    payload = load_fixture("daily_weight.json")
    window_start = dt.date(2026, 11, 10)
    chunk_end = window_start + dt.timedelta(days=29)
    store = make_store(tmp_path, anchor=window_start)
    seed_ranged_chunk(store, "weight", window_start, chunk_end,
                      payload["families"]["weight"]["chunks"][0]["data"])
    readpath.reset_for_tests(store, "read_through")
    fake.ranged_script["weight"] = [payload["families"]["weight"]["chunks"][0]["data"]]
    out = tools.daily("2026-11-11", "2026-11-11", families="weight")
    assert out["errors"] == {}
    live_calls = [c for c in fake.ranged_calls if len(c) == 4]  # tool-layer calls
    assert len(live_calls) == 1  # served live, store untouched for it


def test_daily_miss_fetches_canonical_chunk_write_through(tmp_path, clean_tools, fake):
    """§3.5 on-miss: the store fetches the missing CANONICAL chunk (not the
    request piece) and serves the request from the store."""
    store = make_store(tmp_path, anchor=STEP_START)
    today = dt.date.today()
    readpath.reset_for_tests(store, "read_through")
    fake.ranged_script["steps"] = [[
        {"calendarDate": (today - dt.timedelta(days=1)).isoformat(),
         "totalSteps": 123},
        {"calendarDate": today.isoformat(), "totalSteps": 456},
    ]]
    out = tools.daily((today - dt.timedelta(days=1)).isoformat(),
                      today.isoformat(), families="steps")
    assert out["errors"] == {}
    assert out["families"]["steps"]["present"] is True
    # exactly one upstream call: the canonical chunk containing the window
    assert len(fake.ranged_calls) == 1
    fam, cs, ce = fake.ranged_calls[0]  # archive-layer dispatch (3-tuple)
    assert fam == "steps"
    from garmin_mcp.families import chunk_for_day
    canonical = chunk_for_day(STEP_START, 28, today)
    assert (cs, ce) == (canonical[0].isoformat(), canonical[1].isoformat())
    assert store.coverage_row("steps", canonical[0], canonical[1]) is not None


def test_body_battery_chunks_param_stays_live(tmp_path, clean_tools, fake):
    payload = load_fixture("daily_body_battery.json")
    window_start = dt.date(2026, 11, 11)
    chunk_end = window_start + dt.timedelta(days=29)
    store = make_store(tmp_path, anchor=window_start)
    seed_ranged_chunk(store, "body_battery", window_start, chunk_end,
                      payload["families"]["body_battery"]["chunks"][0]["data"])
    readpath.reset_for_tests(store, "read_through")
    fake.ranged_script["body_battery"] = [
        payload["families"]["body_battery"]["chunks"][0]["data"]]
    out = tools.daily("2026-11-11", "2026-11-12", families="body_battery",
                      body_battery_chunks=2)
    live_calls = [c for c in fake.ranged_calls if len(c) == 4]  # tool-layer calls
    assert len(live_calls) == 1  # a chunks request bypasses the store


# ------------------------------------------------------------- per-day reads

PERDAY_FIXTURES = ["daily_perday_spo2.json", "daily_perday_fitness_age.json",
                   "daily_perday_heart_rate.json"]


@pytest.mark.parametrize("fixture", PERDAY_FIXTURES)
def test_perday_readthrough_matches_live_capture(tmp_path, clean_tools, fake, fixture):
    payload = load_fixture(fixture)
    family = next(iter(payload["families"]))
    store = make_store(tmp_path, anchor=dt.date(2026, 11, 11))
    for day_piece in payload["families"][family]["days"]:
        seed_perday(store, family, dt.date.fromisoformat(day_piece["date"]),
                    day_piece["data"])
    readpath.reset_for_tests(store, "read_through")
    out = tools.daily(payload["start_date"], payload["end_date"], families=family)
    assert out["errors"] == {}
    assert out["families"][family] == payload["families"][family]
    assert fake.perday_calls == []  # zero upstream calls


def test_perday_missing_day_fetches_live_and_writes_through(tmp_path, clean_tools, fake):
    store = make_store(tmp_path, anchor=dt.date(2026, 11, 11))
    readpath.reset_for_tests(store, "read_through")
    day = dt.date.today() - dt.timedelta(days=3)
    fake.perday_script["spo2"] = [{"calendarDate": day.isoformat(), "avg": 94}]
    out = tools.daily(day.isoformat(), day.isoformat(), families="spo2")
    assert out["families"]["spo2"]["days"][0]["data"] == \
        {"calendarDate": day.isoformat(), "avg": 94}
    assert len(fake.perday_calls) == 1
    assert store.read_json(store.perday_path("spo2", day)) == \
        {"calendarDate": day.isoformat(), "avg": 94}


def test_perday_stale_past_day_refetches(tmp_path, clean_tools, fake):
    store = make_store(tmp_path, anchor=dt.date(2026, 11, 11))
    day = dt.date.today() - dt.timedelta(days=3)
    seed_perday(store, "spo2", day, {"stale": True},
                fetched_at="2026-11-11T00:00:00+00:00")  # before day end + lag
    readpath.reset_for_tests(store, "read_through")
    fake.perday_script["spo2"] = [{"calendarDate": day.isoformat(), "avg": 95}]
    out = tools.daily(day.isoformat(), day.isoformat(), families="spo2")
    assert out["families"]["spo2"]["days"][0]["data"]["avg"] == 95
    assert len(fake.perday_calls) == 1


def test_perday_today_ttl_gate(tmp_path, clean_tools, fake):
    store = make_store(tmp_path, anchor=dt.date(2026, 11, 11))
    today = dt.date.today()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=700)).isoformat()
    seed_perday(store, "spo2", today, {"old": True}, fetched_at=old)
    readpath.reset_for_tests(store, "read_through")
    fake.perday_script["spo2"] = [{"calendarDate": today.isoformat(), "fresh": True}]
    out = tools.daily(today.isoformat(), today.isoformat(), families="spo2")
    assert out["families"]["spo2"]["days"][0]["data"]["fresh"] is True
    assert len(fake.perday_calls) == 1


def test_perday_upstream_failure_lands_in_errors_map(tmp_path, clean_tools, fake):
    store = make_store(tmp_path, anchor=dt.date(2026, 11, 11))
    readpath.reset_for_tests(store, "read_through")
    day = dt.date.today() - dt.timedelta(days=3)
    fake.perday_script["spo2"] = [FakeStatusException(500)] * 3
    out = tools.daily(day.isoformat(), day.isoformat(), families="spo2")
    assert out["families"]["spo2"] == {"present": False}
    assert "spo2" in out["errors"]


# ------------------------------------------------------------- staleness (ranged)

def test_stale_day_triggers_canonical_chunk_refetch(tmp_path, clean_tools, fake):
    store = make_store(tmp_path, anchor=STEP_START)
    seed_ranged_chunk(store, "steps", STEP_START, STEP_END,
                      [{"calendarDate": "2026-11-07", "totalSteps": None}],
                      fetched_at="2026-11-07T06:00:00+00:00")  # stale for 11-07
    readpath.reset_for_tests(store, "read_through")
    fresh = [{"calendarDate": "2026-11-07", "totalSteps": 8518}]
    fake.ranged_script["steps"] = [fresh]
    out = tools.daily("2026-11-07", "2026-11-07", families="steps")
    assert out["families"]["steps"]["chunks"][0]["data"] == fresh
    assert len(fake.ranged_calls) == 1  # whole canonical chunk re-fetched
    assert store.read_json(store.raw_path("steps", STEP_START, STEP_END)) == fresh


def test_stale_refetch_failure_never_serves_stale_bytes(tmp_path, clean_tools, fake):
    """§5.2: a piece whose re-fetch fails is never served from the store's
    stale bytes — it lands in the errors map like a failed live family call."""
    store = make_store(tmp_path, anchor=STEP_START)
    seed_ranged_chunk(store, "steps", STEP_START, STEP_END,
                      [{"calendarDate": "2026-11-07", "totalSteps": None}],
                      fetched_at="2026-11-07T06:00:00+00:00")
    readpath.reset_for_tests(store, "read_through")
    fake.ranged_script["steps"] = [FakeStatusException(503)] * 3
    out = tools.daily("2026-11-07", "2026-11-07", families="steps")
    assert out["families"]["steps"] == {"present": False}
    assert "steps" in out["errors"]


# ------------------------------------------------------------- activity reads

def test_recent_activities_fresh_index_zero_calls(tmp_path, clean_tools):
    record = load_fixture("search_activities_page1.json")["activities"][0]
    store = make_store(tmp_path)
    store.write_json_atomic(store.activity_dir(record["activityId"])
                            / "search-record.json", record)
    store.upsert_activity_search(record, sync.utc_now_iso())
    readpath.reset_for_tests(store, "read_through")
    out = tools.recent_activities(limit=5)
    assert out == {"activities": [record]}
    assert tools.api_status()["mode"] == "store-read-through-v2"


def test_recent_activities_gate_finds_new_activity(tmp_path, clean_tools, fake,
                                                   monkeypatch):
    record = load_fixture("search_activities_page1.json")["activities"][0]
    store = make_store(tmp_path)
    store.write_json_atomic(store.activity_dir(record["activityId"])
                            / "search-record.json", record)
    store.upsert_activity_search(record, "2026-11-10T00:00:00+00:00")  # old
    readpath.reset_for_tests(store, "read_through")
    fresh_record = dict(record, activityId=999, activityName="Brand new",
                        startTimeGMT="2026-11-12 08:00:00",
                        startTimeLocal="2026-11-12 08:00:00")
    fake.page_script = [[fresh_record], []]
    fake.detail_script = [{"activityId": 999}]
    out = tools.recent_activities(limit=5)
    ids = [a["activityId"] for a in out["activities"]]
    assert ids == [999, record["activityId"]]
    assert store.has_activity(999)
    assert store.activity_row(999)["detail_fetched_at"] is not None  # write-through


def test_recent_activities_gate_failure_raises_like_live(tmp_path, clean_tools, fake):
    store = make_store(tmp_path)
    store.upsert_activity_search(
        {"activityId": 1, "startTimeGMT": "2026-11-01 00:00:00"},
        "2026-11-10T00:00:00+00:00")
    readpath.reset_for_tests(store, "read_through")
    fake.page_script = [FakeStatusException(429)] * 3
    with pytest.raises(FakeStatusException):
        tools.recent_activities(limit=5)


def test_activity_readthrough_and_fallback(tmp_path, clean_tools, fake):
    detail = load_fixture("activity_detail_24997020730.json")
    store = make_store(tmp_path)
    readpath.reset_for_tests(store, "read_through")
    # miss -> live + write-through
    fake.detail_script = [detail["activity"]]
    out = tools.activity("24997020730")
    assert out == detail
    assert (store.activity_dir(24997020730) / "detail.json").exists()
    # hit -> store, zero calls
    fake.detail_calls.clear()
    out2 = tools.activity("24997020730")
    assert out2 == detail
    assert fake.detail_calls == []


def test_activity_file_readthrough_zero_calls(tmp_path, clean_tools, fake):
    store = make_store(tmp_path)
    store.write_bytes_atomic(store.activity_dir(24997020730) / "file.zip", b"ZIP")
    readpath.reset_for_tests(store, "read_through")
    out = tools.activity_file("24997020730")
    fixture = load_fixture("activity_file_summary.json")
    assert out["activity_id"] == fixture["activity_id"]
    assert out["content_type"] == fixture["content_type"]
    assert out["note"] == fixture["note"]
    assert out["size_bytes"] == 3
    import hashlib

    assert out["sha256"] == hashlib.sha256(b"ZIP").hexdigest()
    assert fake.zip_calls == []  # computed from the stored file, zero API calls


def test_workout_usage_readthrough(tmp_path, clean_tools, fake):
    payload = load_fixture("activity_workouts_24997020730.json")
    store = make_store(tmp_path)
    store.write_json_atomic(store.activity_dir(24997020730) / "workouts.json",
                            payload["workouts"])
    readpath.reset_for_tests(store, "read_through")
    assert tools.workout_usage("24997020730") == payload
    assert fake.workout_calls == []


def test_trend_readthrough_uses_store(tmp_path, clean_tools, fake):
    """§3.5: trend aggregates from the store's perday files (derived from the
    chunk) with the same weekly code as today, zero upstream calls."""
    payload = load_fixture("daily_steps.json")
    store = make_store(tmp_path, anchor=dt.date(2026, 11, 7))
    piece = payload["families"]["steps"]["chunks"][0]
    # seed the grid chunk covering the trend window; its perday files derive
    seed_ranged_chunk(store, "steps", dt.date(2026, 11, 7),
                      dt.date(2026, 11, 7) + dt.timedelta(days=27),
                      piece["data"][-6:])
    readpath.reset_for_tests(store, "read_through")
    out = tools.trend("steps", weeks=1)
    assert out["family"] == "steps"
    assert out["buckets"]
    bucket = out["buckets"][-1]
    assert bucket["week_start"] == "2026-11-09"  # Monday-anchored
    in_window = [r for r in piece["data"] if r["calendarDate"] >= "2026-11-09"]
    assert bucket["days"] == len(in_window)
    assert bucket["mean"]["totalSteps"] == round(
        sum(r["totalSteps"] for r in in_window) / len(in_window), 3)
    assert fake.ranged_calls == []


# ------------------------------------------------------------- degradation

def test_store_io_error_degrades_to_live(tmp_path, clean_tools, fake, monkeypatch):
    payload = load_fixture("daily_steps.json")
    store = make_store(tmp_path, anchor=STEP_START)
    readpath.reset_for_tests(store, "read_through")
    # the index is unreadable: every store query raises
    from garmin_mcp.store import IndexUnavailable

    def broken_query(sql, params=()):
        raise IndexUnavailable("boom")
    monkeypatch.setattr(store, "_query", broken_query)
    fake.ranged_script["steps"] = [
        payload["families"]["steps"]["chunks"][0]["data"]]
    out = tools.daily("2026-10-15", "2026-11-11", families="steps")
    assert out["errors"] == {}  # degraded to live, family served
    assert out["families"]["steps"]["present"] is True
    live_calls = [c for c in fake.ranged_calls if len(c) == 4]  # tool-layer
    assert len(live_calls) == 1


def test_read_through_rejected_without_init_checks(tmp_path, clean_tools, monkeypatch):
    store = make_store(tmp_path)  # no coverage, no anchor
    monkeypatch.setenv("GARMIN_STORE_DIR", str(store.root))
    monkeypatch.setenv("GARMIN_STORE_MODE", "read_through")
    readpath.init_from_env()
    assert readpath.mode() == "live_only"  # demoted, reads stay live
    assert readpath.degraded() and "init checks failed" in readpath.degraded()
    assert readpath.read_through() is False


# ------------------------------------------------------------- api_status block

def test_api_status_store_block(tmp_path, clean_tools, fake, monkeypatch):
    payload = load_fixture("daily_steps.json")
    store = make_store(tmp_path, anchor=STEP_START)
    piece = payload["families"]["steps"]["chunks"][0]
    seed_ranged_chunk(store, "steps", STEP_START, STEP_END, piece["data"])
    readpath.reset_for_tests(store, "read_through")
    monkeypatch.setenv("GARMIN_API_KEY", "test-key")
    status = tools.api_status()
    assert status["mode"] == "store-read-through-v2"
    block = status["store"]
    assert block["enabled"] is True and block["mode"] == "read_through"
    assert block["activities"] == 0 and block["zips"] == 0
    assert block["coverage"]["steps"]["chunks"] == 1
    assert block["coverage"]["steps"]["holes"] == 1  # the [11-12..12-09] grid chunk is not stored
    assert block["coverage"]["steps"]["range"] == ["2026-10-15", "2026-11-11"]
    assert "steps" in block["last_sync"]
    assert block["backfill"] == {"state": "unknown", "progress": "0 of 5 stages"}
    assert block["circuits"] == {}
    assert block["degraded"] is None
    assert block["off_host"]["healthy"] is False  # no backup-status yet
    assert "missing" in block["off_host"]["reason"]


def test_api_status_off_host_stale_alarm(tmp_path, clean_tools, fake):
    """§5.3/§6.1 step 9: healthy:true in a file older than ~30 h renders
    healthy:false with the stale reason (a dead checker turns the alarm on)."""
    store = make_store(tmp_path)
    status_path = store.root / "state" / "backup-status.json"
    status_path.write_text(json.dumps({
        "date": "2026-11-12", "off_host_healthy": True, "reason": "ok",
        "last_upload": "2026-11-12T04:47:00+00:00",
        "newest_drive_item": "garmin-archive-2026-11-12.age", "age_hours": 5,
    }))
    old_stamp = _now() - 31 * 3600
    import os

    os.utime(status_path, (old_stamp, old_stamp))
    readpath.reset_for_tests(store, "read_through")
    block = readpath.status_block()
    assert block["off_host"]["healthy"] is False
    assert "stale" in block["off_host"]["reason"]
    # a fresh, healthy file renders healthy with recomputed age_hours
    status_path.write_text(json.dumps({
        "date": "2026-11-13", "off_host_healthy": True, "reason": "ok",
        "last_upload": "2026-11-13T04:47:00+00:00",
        "newest_drive_item": "garmin-archive-2026-11-13.age", "age_hours": 5,
    }))
    block = readpath.status_block()
    assert block["off_host"]["healthy"] is True
    assert isinstance(block["off_host"]["age_hours"], int)


def test_off_mode_has_no_store_block_and_no_sync(tmp_path, clean_tools, fake,
                                                 monkeypatch):
    monkeypatch.setenv("GARMIN_STORE_DIR", str(tmp_path / "garmin-store"))
    monkeypatch.delenv("GARMIN_STORE_MODE", raising=False)  # default off
    readpath.init_from_env()
    assert readpath.mode() == "off"
    assert readpath.status_block() is None
    monkeypatch.setenv("GARMIN_API_KEY", "test-key")
    status = tools.api_status()
    assert "store" not in status
    assert status["mode"] == "live-read-v1"
