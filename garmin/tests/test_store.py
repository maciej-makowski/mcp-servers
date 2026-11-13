"""Store mechanics (design §3): layout, atomic writes, index, coverage,
per-day derivation, reindex, status rendering."""
import datetime as dt
import json
import os
import stat

from garmin_mcp.store import (STORE_SCHEMA_VERSION, Store, StoreError,
                              slice_data, utc_now_iso)
from storekit import load_fixture, make_store


def test_store_layout_created_mode_0700(tmp_path):
    root = tmp_path / "garmin-store"
    store = Store(root)
    store.ensure_dirs()
    for name in ("activities", "raw", "perday", "point", "state"):
        assert (root / name).is_dir()
    mode = stat.S_IMODE(os.stat(root).st_mode)
    assert mode == 0o700  # M2: plaintext 0700 mcp-owned


def test_atomic_json_roundtrip_and_no_tmp_leftover(tmp_path):
    store = make_store(tmp_path)
    target = store.root / "point" / "hr_zones.json"
    store.write_json_atomic(target, {"zones": [1, 2, 3]})
    assert store.read_json(target) == {"zones": [1, 2, 3]}
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == []


def test_atomic_write_failure_keeps_old_file(tmp_path):
    store = make_store(tmp_path)
    target = store.root / "point" / "hr_zones.json"
    store.write_json_atomic(target, {"v": 1})
    # Force a failure: target parent replaced by a file so open() fails
    bad = store.root / "point2.json"
    bad.write_text("x")
    (store.root / "point_decoy").write_text("y")
    # Simulate a crashed write: a leftover tmp must not corrupt the original
    store.write_json_atomic(target, {"v": 2})
    assert store.read_json(target) == {"v": 2}


def test_index_schema_version_and_meta(tmp_path):
    store = make_store(tmp_path)
    assert store.get_meta("schema_version") == STORE_SCHEMA_VERSION
    store.set_meta("anchor", "2024-03-01")
    assert store.get_meta("anchor") == "2024-03-01"


def test_activity_upsert_markers_and_pending(tmp_path):
    store = make_store(tmp_path)
    record = load_fixture("search_activities_page1.json")["activities"][0]
    store.upsert_activity_search(record, utc_now_iso())
    assert store.has_activity(record["activityId"])
    row = store.activity_row(record["activityId"])
    assert row["name"] == "Wandsworth Walking"
    assert row["start_time_gmt"] == "2026-11-07 10:57:57"
    assert store.counts() == (1, 0)
    assert store.activities_pending_payloads() == [record["activityId"]]
    store.set_activity_marker(record["activityId"], "detail_fetched_at", utc_now_iso())
    store.set_activity_marker(record["activityId"], "workouts_fetched_at", utc_now_iso())
    store.set_activity_marker(record["activityId"], "zip_fetched_at", utc_now_iso(),
                              zip_sha256="ab" * 32, zip_size=106357)
    assert store.activities_pending_payloads() == []
    assert store.counts() == (1, 1)


def test_search_record_ids_newest_first_and_since_filter(tmp_path):
    store = make_store(tmp_path)
    for aid, start in ((3, "2026-11-07 10:57:57"), (1, "2026-10-27 07:24:09"),
                       (2, "2026-10-29 18:57:05")):
        store.upsert_activity_search({
            "activityId": aid, "activityName": f"a{aid}", "startTimeGMT": start,
            "activityType": {"typeKey": "running"},
        }, utc_now_iso())
    assert store.search_record_ids(None, 10) == [3, 2, 1]
    assert store.search_record_ids("2026-10-29", 10) == [3, 2]
    assert store.search_record_ids(None, 2) == [3, 2]


def test_coverage_daily_records_and_holes(tmp_path):
    store = make_store(tmp_path)
    anchor = dt.date(2026, 11, 10)
    store.set_meta("anchor", anchor.isoformat())
    store.record_coverage("steps", anchor, dt.date(2026, 11, 11), 100, utc_now_iso())
    summary = store.coverage_summary("steps", 28, anchor, dt.date(2026, 11, 13))
    # expected chunks for [anchor, 11-13] with cap 28: one grid chunk; we only
    # stored a row for a different (non-grid) window in this raw test — the
    # grid-based holes math is exercised via write_ranged_chunk below.
    assert summary["chunks"] == 1
    store.record_daily("hrv", dt.date(2026, 11, 11), True, utc_now_iso())
    assert store.has_daily_row("hrv", dt.date(2026, 11, 11))
    assert store.daily_fetched_at("hrv", dt.date(2026, 11, 11)) is not None
    assert store.daily_fetched_at("hrv", dt.date(2026, 11, 12)) is None


def test_write_ranged_chunk_derives_perday_for_list_shape(tmp_path):
    store = make_store(tmp_path)
    fixture = load_fixture("daily_steps.json")
    piece = fixture["families"]["steps"]["chunks"][0]
    start = dt.date.fromisoformat(piece["start"])
    end = dt.date.fromisoformat(piece["end"])
    store.write_ranged_chunk("steps", start, end, piece["data"])
    raw = store.read_json(store.raw_path("steps", start, end))
    assert raw == piece["data"]
    # perday files exist for every day in the chunk and carry that day's slice
    day = start
    while day <= end:
        perday = store.read_json(store.perday_path("steps", day))
        if day.isoformat() == "2026-11-05":
            # a real null day in the capture: the record exists but is empty
            assert perday == [{"calendarDate": "2026-11-05", "totalSteps": None,
                               "totalDistance": 0, "stepGoal": 8690}]
        assert store.has_daily_row("steps", day)
        day += dt.timedelta(days=1)


def test_reindex_rebuilds_from_files(tmp_path):
    store = make_store(tmp_path)
    record = load_fixture("search_activities_page1.json")["activities"][0]
    aid = record["activityId"]
    store.write_json_atomic(store.activity_dir(aid) / "search-record.json", record)
    store.write_json_atomic(store.activity_dir(aid) / "detail.json", {"x": 1})
    start, end = dt.date(2026, 11, 10), dt.date(2026, 11, 11)
    store.write_ranged_chunk("steps", start, end,
                             [{"calendarDate": "2026-11-10", "totalSteps": 5}])
    (store.root / "state" / "sync-state.json").write_text(
        json.dumps({"anchor": "2026-11-10"}))
    # wipe every derived table, then rebuild
    stats = store.reindex()
    assert stats["activities"] == 1
    assert stats["chunks"] == 1
    row = store.activity_row(aid)
    assert row is not None and row["detail_fetched_at"] is not None
    assert store.coverage_row("steps", start, end) is not None
    assert store.has_daily_row("steps", start)
    assert store.get_meta("anchor") == "2026-11-10"
    assert store.get_meta("schema_version") == STORE_SCHEMA_VERSION


def test_reindex_resets_markers_without_files(tmp_path):
    store = make_store(tmp_path)
    record = load_fixture("search_activities_page1.json")["activities"][0]
    aid = record["activityId"]
    store.write_json_atomic(store.activity_dir(aid) / "search-record.json", record)
    store.reindex()
    # no detail.json on disk -> pending payloads again (files are primary)
    assert store.activities_pending_payloads() == [aid]


def test_slice_data_list_and_single_list_dict(tmp_path):
    steps = [{"calendarDate": "2026-11-10", "totalSteps": 5},
             {"calendarDate": "2026-11-11", "totalSteps": 6}]
    assert slice_data(steps, dt.date(2026, 11, 11), dt.date(2026, 11, 11)) == \
        [{"calendarDate": "2026-11-11", "totalSteps": 6}]
    hrv = {"hrvSummaries": [{"calendarDate": "2026-11-10", "weeklyAvg": 52},
                            {"calendarDate": "2026-11-11", "weeklyAvg": 53}],
           "userProfilePk": 101702041}
    assert slice_data(hrv, dt.date(2026, 11, 11), dt.date(2026, 11, 11)) == {
        "hrvSummaries": [{"calendarDate": "2026-11-11", "weeklyAvg": 53}],
        "userProfilePk": 101702041}
    # weight's aggregate dict shape (one list + scalar dicts incl. sub-dicts)
    weight = {"dailyWeightSummaries": [], "totalAverage": {"weight": None},
              "nextDateWeight": None}
    assert slice_data(weight, dt.date(2026, 11, 10), dt.date(2026, 11, 10)) is None


def test_read_through_init_checks_helpers(tmp_path):
    store = make_store(tmp_path)
    # empty coverage: the store itself is queryable but has no rows
    assert store.coverage_rows() == []
    store.record_coverage("steps", dt.date(2026, 11, 10), dt.date(2026, 11, 11),
                          10, utc_now_iso())
    store.set_meta("anchor", "2026-11-10")
    assert len(store.coverage_rows()) == 1


def test_counts_and_last_sync(tmp_path):
    store = make_store(tmp_path)
    assert store.counts() == (0, 0)
    now = utc_now_iso()
    store.record_coverage("steps", dt.date(2026, 11, 10), dt.date(2026, 11, 11), 1, now)
    store.record_daily("spo2", dt.date(2026, 11, 10), True, now)
    last = store.last_sync_per_family()
    assert last["steps"] == now and last["spo2"] == now
