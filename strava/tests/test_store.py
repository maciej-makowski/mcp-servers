"""Store mechanics (strava-local-store design section 3.2, A3): layout,
atomic writes, schema version, fingerprint idempotency, deletion marking,
and the D9 read helpers. No network in tests (house rule)."""

from __future__ import annotations

import os
import stat

import pytest

from strava_mcp import store as store_mod
from strava_mcp.store import Store, StoreError, STORE_SCHEMA_VERSION, utc_now_iso


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    root = str(tmp_path / "strava-store")
    monkeypatch.setattr(store_mod, "STORE_DIR", root)
    return root


@pytest.fixture
def store(store_root):
    s = Store()
    s.init_schema()
    return s


def _summary(aid: int, name: str = "Morning run", **over):
    base = {
        "id": aid, "name": name, "sport_type": "Run",
        "start_date": "2026-11-07T10:57:57+00:00",
        "start_date_local": "2026-11-07T10:57:57GMT",
        "distance": 2742.0, "moving_time": 324, "elapsed_time": 341,
        "total_elevation_gain": 12.0, "average_speed": 8.4, "max_speed": 9.9,
        "private": False, "gear_id": "b123", "total_photo_count": 0,
        "kudos_count": 2, "comment_count": 0, "achievement_count": 0,
        "workout_type": None, "external_id": None, "upload_id": None,
    }
    base.update(over)
    return base


def test_layout_created_mode_0700(store_root):
    Store().ensure_dirs()
    for sub in ("activities", "gear", "athlete", "stats", "zones",
                "segments", "routes"):
        assert os.path.isdir(os.path.join(store_root, sub))
    mode = stat.S_IMODE(os.stat(store_root).st_mode)
    assert mode == 0o700


def test_schema_version_written_once(store):
    store.init_schema()  # second init must not add a row
    assert store.check_schema() == STORE_SCHEMA_VERSION
    import sqlite3
    with sqlite3.connect(os.path.join(store.root, "index.db")) as conn:
        n = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert n == 1


def test_check_schema_refuses_unknown_version(store_root):
    s = Store()
    s.init_schema()
    import sqlite3
    with sqlite3.connect(os.path.join(s.root, "index.db")) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
    with pytest.raises(StoreError, match="rebuild the store index"):
        s.check_schema()


def test_atomic_json_roundtrip_no_leftover(store_root):
    s = Store()
    s.ensure_dirs()
    target = os.path.join(store_root, "activities", "1")
    sha = s.write_json_atomic(os.path.join(target, "summary.json"), {"id": 1})
    payload = s.read_json(os.path.join(target, "summary.json"))
    assert payload == {"id": 1}
    assert list(os.listdir(target)) == ["summary.json"]  # no tmp residue
    assert len(sha) == 64


def test_summary_upsert_fingerprint_lifecycle(store):
    now = utc_now_iso()
    assert store.upsert_activity_summary(_summary(1), now=now) == "added"
    assert store.upsert_activity_summary(_summary(1), now=now) == "unchanged"
    assert store.upsert_activity_summary(
        _summary(1, name="Renamed run"), now=now
    ) == "updated"
    row = store.activity_row(1)
    assert row["name"] == "Renamed run"
    assert row["first_seen_at"] == now  # first_seen never moves


def test_detail_and_streams_markers(store):
    store.upsert_activity_summary(_summary(1))
    now = utc_now_iso()
    assert store.save_detail(1, {"id": 1, "visibility": "everyone"}, now=now)
    assert store.save_streams(1, {"time": {"data": [0, 1]}}, now=now)
    counts = store.counts()
    assert counts["with_details"] == 1 and counts["with_streams"] == 1
    assert store.detail_payload(1) == {"id": 1, "visibility": "everyone"}
    assert store.streams_payload(1) == {"time": {"data": [0, 1]}}


def test_deletion_marks_row_and_keeps_file(store):
    store.upsert_activity_summary(_summary(1))
    detail_path = None
    store.save_detail(1, {"id": 1})
    detail_path = os.path.join(store.root, "activities", "1", "detail.json")
    assert store.mark_deleted([1]) == 1
    row = store.activity_row(1)
    assert row["deleted_at"] is not None  # D10: marked, not erased
    assert os.path.isfile(detail_path)    # the archive keeps the files
    assert store.open_activity_ids() == []
    assert store.list_summaries() == []


def test_backfill_pending_set_drops_completed(store):
    for aid in (3, 2, 1):
        store.upsert_activity_summary(
            _summary(aid, start_date=f"2026-11-0{aid}T00:00:00+00:00"))
    pending = store.summaries_missing_backfill()
    assert [r["id"] for r in pending] == [1, 2, 3]  # oldest first
    store.save_detail(1, {"id": 1})
    store.save_streams(1, {"time": {"data": [0]}})
    pending = store.summaries_missing_backfill()
    assert [r["id"] for r in pending] == [2, 3]
    assert store.summaries_missing_backfill(cursor=4) == []


def test_list_summaries_newest_first_after_before(store):
    for aid, day in ((1, "2026-10-01"), (2, "2026-10-15"), (3, "2026-11-01")):
        store.upsert_activity_summary(
            _summary(aid, start_date=f"{day}T09:00:00+00:00"))
    rows = store.list_summaries()
    assert [r["id"] for r in rows] == [3, 2, 1]
    page2 = store.list_summaries(page=2, per_page=2)
    assert [r["id"] for r in page2] == [1]
    import datetime as dt
    epoch = int(dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc).timestamp())
    rows = store.list_summaries(after=epoch)
    assert [r["id"] for r in rows] == [3]
    rows = store.list_summaries(before=epoch)
    assert [r["id"] for r in rows] == [2, 1]


def test_snapshot_writes_only_on_change(store_root):
    s = Store()
    s.init_schema()
    payload = {"zones": [1, 2, 3]}
    assert s.save_snapshot("zones", "20261113T000000Z", payload) is True
    assert s.save_snapshot("zones", "20261113T000000Z", payload) is False
    assert s.save_snapshot("zones", "20261113T000000Z", {"zones": [4]}) is True


def test_summary_raw_readback(store):
    raw = _summary(7)
    store.upsert_activity_summary(raw)
    assert store.summary_raw(7) == raw
    assert store.summary_raw(999) is None


def test_gear_and_counts(store):
    store.upsert_activity_summary(_summary(1, gear_id="b1"))
    store.upsert_activity_summary(_summary(2, gear_id="b1"))
    assert store.gear_ids_seen() == ["b1"]
    assert store.save_gear({"id": "b1", "name": "Voodoo", "distance": 100.0})
    assert store.save_gear({"id": "b1", "name": "Voodoo", "distance": 100.0}) is False
    row = store.activity_row(1)
    assert row["gear_id"] == "b1"
