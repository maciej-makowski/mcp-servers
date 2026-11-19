"""Sync engine (design §4): budget, pacing, circuits, backfill stages,
sweep, arrival-safe paging, gap backfill, hourly tier gate, weekly verify,
and the §7.5 acceptance test: a crashing sync task must leave the seven
tools serving live."""
import contextlib
import datetime as dt
import time

import pytest

import garmin_mcp.cache as cache_mod
from garmin_mcp import client, readpath, sync, tools
from garmin_mcp.sync import BudgetExhausted, CircuitOpen, SyncEngine
from storekit import (FakeClock, FakeClient, FakeStatusException, FakeToken,
                      SleepingClock, load_fixture, make_store)

DAY = dt.date(2026, 11, 10)


@pytest.fixture()
def clean_state(monkeypatch):
    monkeypatch.setattr(cache_mod.time, "sleep", lambda s: None)  # no real backoff waits
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    monkeypatch.delenv(sync.ENV_HOURLY, raising=False)
    readpath.reset_for_tests(None, "off")
    sync.reset_for_tests()
    client._FAMILY_ERRORS.clear()
    tools.CACHE.clear()
    yield
    readpath.reset_for_tests(None, "off")
    sync.reset_for_tests()
    client._FAMILY_ERRORS.clear()
    tools.CACHE.clear()


def engine(store, fake, clock=None, pacing=0.0, max_calls=1500, hourly=None,
           sleep=None):
    return SyncEngine(
        store, client_module=fake, max_calls=max_calls, pacing_s=pacing,
        hourly_enabled=hourly, clock=(clock or FakeClock()).time,
        sleep=(sleep or (lambda s: None)))


def era_script(era_start: dt.date, era_end: dt.date):
    """Window-aware ranged script: data for any window touching
    [era_start, era_end], empty for the rest (the §3.2 empty-array
    semantics). A family with no data uses an empty era far in the future."""
    def _script(family, start, end):
        if end >= era_start and start <= era_end:
            return [{"calendarDate": max(start, era_start).isoformat(), "v": 1}]
        return []
    return _script


EMPTY_ERA = (dt.date(9999, 9, 9), dt.date(9999, 9, 9))


def test_budget_aborts_cleanly_and_resumes(tmp_path, clean_state):
    fake = FakeClient()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, max_calls=1)
    eng._fetch_ranged_unit("steps", DAY, DAY + dt.timedelta(days=27))
    assert store.coverage_row("steps", DAY, DAY + dt.timedelta(days=27)) is not None
    with pytest.raises(BudgetExhausted):
        eng._fetch_ranged_unit("steps", DAY + dt.timedelta(days=28),
                               DAY + dt.timedelta(days=55))
    # resume on the next run: stored, watermark advanced
    eng2 = engine(store, fake, max_calls=5)
    eng2._fetch_ranged_unit("steps", DAY + dt.timedelta(days=28),
                            DAY + dt.timedelta(days=55))
    assert store.coverage_row("steps", DAY + dt.timedelta(days=28),
                              DAY + dt.timedelta(days=55)) is not None
    assert eng2.state["watermarks"]["steps"]["chunk_end"] == \
        (DAY + dt.timedelta(days=55)).isoformat()


def test_pacing_floor_between_calls(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    sleeper = SleepingClock(clock)
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, clock=clock, pacing=2.0, sleep=sleeper.sleep)
    eng._upstream("steps", lambda: [{"calendarDate": "2026-11-10"}])
    eng._upstream("steps", lambda: [{"calendarDate": "2026-11-11"}])
    # the second call waited out the 2 s floor (the first call waits none)
    assert sleeper.sleeps and 1.0 <= sleeper.sleeps[0] <= 2.0, sleeper.sleeps


def test_circuit_opens_doubles_and_caps(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, clock=clock, max_calls=100)
    # three attempts all 429 (survives the existing backoff semantics)
    fake.ranged_script["steps"] = [FakeStatusException(429)] * 3
    with pytest.raises(FakeStatusException):
        eng._upstream("steps", lambda: fake._next(fake.ranged_script["steps"], "w"))
    assert eng.circuit_open("steps")
    entry = eng.state["circuits"]["steps"]
    assert entry["failures"] == 1
    until = dt.datetime.fromisoformat(entry["open_until"])
    assert (until - clock.utc()).total_seconds() == pytest.approx(1800, abs=2)
    # while open, calls are refused without touching upstream
    calls_before = len(fake.ranged_calls)
    with pytest.raises(CircuitOpen):
        eng._upstream("steps", lambda: [])
    assert len(fake.ranged_calls) == calls_before
    # after the circuit expires, a surviving failure doubles to 60 min
    clock.advance(1801)
    fake.ranged_script["steps"] = [FakeStatusException(429)] * 3
    with pytest.raises(FakeStatusException):
        eng._upstream("steps", lambda: fake._next(fake.ranged_script["steps"], "w"))
    until2 = dt.datetime.fromisoformat(eng.state["circuits"]["steps"]["open_until"])
    assert (until2 - clock.utc()).total_seconds() == pytest.approx(3600, abs=2)
    # the doubling sequence caps at 6 h
    for _ in range(4):
        clock.advance(21601)
        fake.ranged_script["steps"] = [FakeStatusException(429)] * 3
        try:
            eng._upstream("steps", lambda: fake._next(fake.ranged_script["steps"], "w"))
        except FakeStatusException:
            pass
    until_last = dt.datetime.fromisoformat(eng.state["circuits"]["steps"]["open_until"])
    assert (until_last - clock.utc()).total_seconds() <= 21600 + 2


def test_401_not_retried_no_circuit(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, clock=clock)
    with pytest.raises(FakeStatusException):
        eng._upstream("steps", lambda: (_ for _ in ()).throw(FakeStatusException(401)))
    assert not eng.circuit_open("steps")
    assert "steps" in fake.family_errors


def test_403_non_retryable_opens_circuit(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, clock=clock)
    with pytest.raises(FakeStatusException):
        eng._upstream("steps", lambda: (_ for _ in ()).throw(FakeStatusException(403)))
    assert eng.circuit_open("steps")
    assert "steps" in fake.family_errors


def test_backfill_full_run_completes(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path)
    today = sync.local_today()
    eng = engine(store, fake, clock=clock, max_calls=1000)
    # sparse probe: steps' era starts 6 days ago; every other family empty
    fake.ranged_script["steps"] = era_script(today - dt.timedelta(days=6), today)
    for other in ("daily", "stress", "hrv", "sleep", "body_battery", "hydration",
                  "weight", "weight_goals", "wellness_activities", "weight_all"):
        fake.ranged_script[other] = era_script(*EMPTY_ERA)
    # stage 1: one page then a short page -> paging complete
    fake.page_script = [
        [{"activityId": 11, "activityName": "a", "startTimeGMT": "2026-11-07 10:57:57"}],
        [],
    ]
    # stage 2 payloads
    fake.detail_script = [{"activityId": 11}]
    fake.workout_script = [[]]
    fake.zip_script = [b"ZIPBYTES11"]
    # stage 4 per-day families over the short era
    for fam in ("spo2", "fitness_age", "heart_rate", "sleep_detail", "hrv_detail",
                "nutrition", "menstrual_summary", "menstrual_day_view"):
        fake.perday_script[fam] = [None] * 20
    # stage 5 points
    fake.point_script = {"profile_settings": [{"a": 1}], "hr_zones": {"z": 1},
                         "legacy_daily": {"d": 1}}
    eng.run_backfill()
    progress = eng.backfill_progress()
    assert progress["state"] == "complete", progress
    assert progress["progress"] == "5 of 5 stages"
    # the anchor is biased early (§3.2): the era's first day lies inside
    # [anchor, anchor + 6], never after it. Exact values are date-coupled.
    era_start = today - dt.timedelta(days=6)
    assert eng.anchor() is not None
    assert eng.anchor() <= era_start <= eng.anchor() + dt.timedelta(days=6)
    assert store.activity_row(11)["detail_fetched_at"] is not None
    assert (store.activity_dir(11) / "file.zip").read_bytes() == b"ZIPBYTES11"
    assert store.coverage_row("steps", eng.anchor(),
                              eng.anchor() + dt.timedelta(days=27)) is not None
    assert store.point_path("profile_settings").exists()


def test_backfill_resumable_across_runs(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path)
    today = sync.local_today()
    eng = engine(store, fake, clock=clock, max_calls=12)
    fake.ranged_script["steps"] = era_script(today - dt.timedelta(days=6), today)
    for other in ("daily", "stress", "hrv", "sleep", "body_battery", "hydration",
                  "weight", "weight_goals", "wellness_activities", "weight_all"):
        fake.ranged_script[other] = era_script(*EMPTY_ERA)
    fake.page_script = [
        [{"activityId": 1, "startTimeGMT": "2026-11-01 00:00:00"}], [],
    ]
    eng.run_backfill()  # the small budget dies before the last stages
    assert eng.backfill_progress()["state"] in ("running", "pending")
    # a fresh engine (a restart) resumes from sync-state and completes
    fake2 = FakeClient()
    eng2 = SyncEngine(store, client_module=fake2, max_calls=1000, pacing_s=0.0,
                      clock=clock.time, sleep=lambda s: None)
    fake2.ranged_script["steps"] = era_script(today - dt.timedelta(days=6), today)
    for other in ("daily", "stress", "hrv", "sleep", "body_battery", "hydration",
                  "weight", "weight_goals", "wellness_activities", "weight_all"):
        fake2.ranged_script[other] = era_script(*EMPTY_ERA)
    fake2.page_script = [
        [{"activityId": 1, "startTimeGMT": "2026-11-01 00:00:00"}], [],
    ]
    for fam in ("spo2", "fitness_age", "heart_rate", "sleep_detail", "hrv_detail",
                "nutrition", "menstrual_summary", "menstrual_day_view"):
        fake2.perday_script[fam] = [None] * 20
    fake2.point_script = {"profile_settings": {}, "hr_zones": {}, "legacy_daily": {}}
    fake2.detail_script = [{"activityId": 1}]
    fake2.workout_script = [[]]
    fake2.zip_script = [b"Z1"]
    eng2.run_backfill()
    assert eng2.backfill_progress()["state"] == "complete"
    assert store.activity_row(1)["zip_fetched_at"] is not None


def test_sparse_probe_sets_global_anchor_at_min(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path)
    today = sync.local_today()
    eng = engine(store, fake, clock=clock, max_calls=2000)
    fake.ranged_script["steps"] = era_script(today - dt.timedelta(days=6), today)
    fake.ranged_script["daily"] = era_script(today - dt.timedelta(days=40), today)
    for other in ("stress", "hrv", "sleep", "body_battery", "hydration",
                  "weight", "weight_goals", "wellness_activities", "weight_all"):
        fake.ranged_script[other] = era_script(*EMPTY_ERA)
    anchor = eng._sparse_probe()
    # the oldest family's era wins (§3.2): the anchor is biased early, so the
    # era's first day (40 days back) lies inside [anchor, anchor + 6]
    era_start = today - dt.timedelta(days=40)
    assert anchor is not None and anchor <= era_start <= anchor + dt.timedelta(days=6), (
        anchor, era_start)


def test_preera_404_recorded_as_empty_marker(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    today = sync.local_today()
    old_end = today - dt.timedelta(days=90)
    old_start = old_end - dt.timedelta(days=27)
    fake.ranged_script["hrv"] = [FakeStatusException(404)]
    eng = engine(store, fake, clock=clock, max_calls=10)
    eng._fetch_ranged_unit("hrv", old_start, old_end)
    # marker: empty-array file + coverage row + sync_log note, NOT a family error
    assert store.read_json(store.raw_path("hrv", old_start, old_end)) == []
    assert store.coverage_row("hrv", old_start, old_end) is not None
    assert "hrv" not in fake.family_errors
    # the trailing chunk (end >= today) keeps real error semantics
    trailing_start = today - dt.timedelta(days=5)
    fake.ranged_script["hrv"] = [FakeStatusException(404)]
    with pytest.raises(FakeStatusException):
        eng._fetch_ranged_unit("hrv", trailing_start, today)


def test_sweep_overwrites_trailing_chunks_and_records_watermark(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    today = sync.local_today()
    from garmin_mcp.families import chunk_for_day
    cs, ce = chunk_for_day(DAY, 28, today)
    store.write_ranged_chunk("steps", cs, ce,
                             [{"calendarDate": cs.isoformat(), "totalSteps": 1}])
    old_size = store.raw_path("steps", cs, ce).stat().st_size
    fake.ranged_script["steps"] = [[{"calendarDate": cs.isoformat(), "totalSteps": 99}]]
    eng = engine(store, fake, clock=clock, max_calls=50)
    eng.run_sweep()
    data = store.read_json(store.raw_path("steps", cs, ce))
    assert data == [{"calendarDate": cs.isoformat(), "totalSteps": 99}]
    assert store.raw_path("steps", cs, ce).stat().st_size != old_size
    assert eng.state["watermarks"]["steps"]["chunk_end"] == ce.isoformat()


def test_arrival_safe_paging_dedupe_stop_and_cap(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, clock=clock, max_calls=50)
    known = {"activityId": 1, "startTimeGMT": "2026-11-01 00:00:00"}
    store.upsert_activity_search(known, sync.utc_now_iso())
    fake.page_script = [
        [{"activityId": 2, "startTimeGMT": "2026-11-05 00:00:00"}, known],
        [],
    ]
    new_ids = eng._arrival_safe_paging(page_cap=5)
    assert new_ids == [2]
    assert store.has_activity(2)
    # a fully-known page stops the scan immediately (§4.2)
    fake2 = FakeClient()
    eng2 = SyncEngine(store, client_module=fake2, max_calls=50, pacing_s=0.0,
                      clock=clock.time, sleep=lambda s: None)
    fake2.page_script = [[known]] * 10
    assert eng2._arrival_safe_paging(page_cap=5) == []
    assert len(fake2.page_calls) == 1


def test_gap_backfill_refills_holes(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    today = sync.local_today()
    fake.ranged_script["steps"] = [[{"calendarDate": today.isoformat(),
                                     "totalSteps": 7}]]
    fake.perday_script["spo2"] = [None] * 40
    eng = engine(store, fake, clock=clock, max_calls=500)
    eng.run_gap_backfill()
    assert store.coverage_row("steps", DAY, DAY + dt.timedelta(days=27)) is not None


def test_hourly_tier_gated_off_by_default(tmp_path, clean_state):
    fake = FakeClient()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake)
    assert eng.hourly_enabled is False  # OD-3/M1: disabled until the verdict


def test_hourly_tier_runs_when_enabled(tmp_path, clean_state, monkeypatch):
    monkeypatch.setenv(sync.ENV_HOURLY, "1")
    fake = FakeClient()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake)
    assert eng.hourly_enabled is True
    eng.run_hourly()  # cadence code present; fires only via the env gate
    assert eng.state["runs"]["last_run"]["kind"] == "hourly"


def test_weekly_verify_sha256_gate(tmp_path, clean_state):
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    record = load_fixture("search_activities_page1.json")["activities"][0]
    aid = record["activityId"]
    store.upsert_activity_search(record, sync.utc_now_iso())
    store.set_activity_marker(aid, "detail_fetched_at", sync.utc_now_iso())
    store.set_activity_marker(aid, "workouts_fetched_at", sync.utc_now_iso())
    eng = engine(store, fake, clock=clock, max_calls=100)
    fake.zip_script = [b"SAMEBYTES"]
    eng.run_weekly_verify()
    first_mtime = (store.activity_dir(aid) / "file.zip").stat().st_mtime_ns
    assert (store.activity_dir(aid) / "file.zip").read_bytes() == b"SAMEBYTES"
    # re-verify with the SAME bytes: fetched, sha gate holds, file NOT rewritten
    clock.advance(10)
    fake.zip_script = [b"SAMEBYTES"]
    eng.run_weekly_verify()
    assert (store.activity_dir(aid) / "file.zip").read_bytes() == b"SAMEBYTES"
    # a different sha (an edit) rewrites the file (§4.5)
    fake.zip_script = [b"EDITEDBYTES"]
    eng.run_weekly_verify()
    assert (store.activity_dir(aid) / "file.zip").read_bytes() == b"EDITEDBYTES"


def test_crashing_sync_run_is_isolated_and_tools_keep_serving(tmp_path, clean_state,
                                                              monkeypatch):
    """§7.5 acceptance: a crashing sync task must leave all seven tools
    serving live."""
    fake = FakeClient()
    clock = FakeClock()
    store = make_store(tmp_path, anchor=DAY)
    eng = engine(store, fake, clock=clock, max_calls=10)

    def boom():
        raise RuntimeError("sync task exploded")

    fake.ranged_script["steps"] = [RuntimeError("boom"), boom]
    eng.run_sweep()  # the guard swallows; the run is recorded, never raised
    assert eng.state["last_error"] is not None

    # the seven tools keep serving from the live path: the module-level
    # client call sites are faked (house convention, AGENTS.md)
    monkeypatch.setattr(client, "search_activities", fake.search_activities)
    monkeypatch.setattr(client, "get_activity", fake.get_activity)
    monkeypatch.setattr(client, "download_activity", fake.download_activity)
    monkeypatch.setattr(client, "get_activity_workouts", fake.get_activity_workouts)
    monkeypatch.setattr(client, "fetch_ranged", fake.fetch_ranged)
    monkeypatch.setattr(client, "fetch_perday", fake.fetch_perday)
    monkeypatch.setattr(client, "obtain_bearer_token",
                        lambda: FakeToken(time.time() + 9999))
    monkeypatch.setenv("GARMIN_API_KEY", "test-key")
    fake.page_script = [[{"activityId": 9, "startTimeGMT": "2026-11-09 00:00:00"}]]
    assert tools.recent_activities(limit=5)["activities"][0]["activityId"] == 9
    fake.detail_script = [{"activityId": 9}]
    assert tools.activity("9")["activity"]["activityId"] == 9
    fake.zip_script = [b"Z"]
    assert tools.activity_file("9")["size_bytes"] == 1
    fake.ranged_script["steps"] = [[{"calendarDate": "2026-11-09", "totalSteps": 4}]]
    out = tools.daily("2026-11-09", "2026-11-09", families="steps")
    assert out["families"]["steps"]["present"] is True
    fake.ranged_script["steps"] = [[
        {"calendarDate": "2026-11-09", "totalSteps": 4},
        {"calendarDate": "2026-11-10", "totalSteps": 5},
    ]]
    assert tools.trend("steps", weeks=1)["buckets"]
    fake.workout_script = [[]]
    assert tools.workout_usage("9")["workouts"] == []
    status = tools.api_status()
    assert status["mode"] == "live-read-v1"
    assert status["token"]["valid"] is True


def test_cadence_task_gated_by_mode(clean_state, tmp_path):
    async def scenario():
        # unconfigured store -> mode off -> no task (the PR alone changes nothing)
        assert sync.start_cadence_task() is None
        store = make_store(tmp_path, anchor=DAY)
        readpath.reset_for_tests(store, "live_only")
        try:
            task = sync.start_cadence_task()
            assert task is not None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            readpath.reset_for_tests(None, "off")
            sync.reset_for_tests()

    import asyncio

    asyncio.run(scenario())
    sync.reset_for_tests()
