"""The Garmin local data archive store (garmin-local-archive design §3).

Layout (all paths relative to GARMIN_STORE_DIR):

    activities/<activity_id>/search-record.json   search-page record (raw)
    activities/<activity_id>/detail.json          get_activity response
    activities/<activity_id>/workouts.json        get_activity_workouts (may be [])
    activities/<activity_id>/file.zip             download_activity bytes
    raw/<family>/<chunk_start>_<chunk_end>.json   canonical ranged chunks
    perday/<family>/<YYYY-MM-DD>.json             per-day raw responses
                                                  (ranged families: derived
                                                  from their chunks, §3.1)
    point/<name>.json                             profile settings, HR zones,
                                                  legacy daily
    index.db                                      SQLite index (DERIVED)
    state/sync-state.json                         watermarks + cursors
    state/backup-status.json                      written by the backup checker

Primary data is the FILES; index.db is an index over them and every query it
answers is rebuildable from the files alone (garmin_mcp.reindex, §5.4). Every
write is atomic: temp file + rename for JSON/ZIP, transactional for SQLite;
sync-state.json is rewritten temp+rename+fsync after every completed unit by
the sync engine (§4.5). The store directory is mode 0700 owned by mcp
(adoption decision M2: plaintext, no on-box encryption).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from garmin_mcp import families

STORE_SCHEMA_VERSION = "1"

_DATE_KEYS = ("calendarDate", "calendar_date", "date", "startDate", "start_date")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
  activity_id INTEGER PRIMARY KEY,
  activity_uuid TEXT, name TEXT, type_key TEXT, sport_type_id INTEGER,
  start_time_gmt TEXT, start_local TEXT,
  distance_m REAL, duration_s REAL, moving_duration_s REAL,
  elevation_gain REAL, calories REAL, avg_hr REAL, max_hr REAL,
  steps INTEGER, owner_id INTEGER,
  search_fetched_at TEXT, detail_fetched_at TEXT, workouts_fetched_at TEXT,
  zip_fetched_at TEXT, zip_sha256 TEXT, zip_size INTEGER,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time_gmt DESC);

CREATE TABLE IF NOT EXISTS coverage (
  family TEXT, chunk_start TEXT, chunk_end TEXT,
  fetched_at TEXT, byte_size INTEGER,
  PRIMARY KEY (family, chunk_start, chunk_end)
);

CREATE TABLE IF NOT EXISTS daily_records (
  family TEXT, date TEXT, present INTEGER, fetched_at TEXT,
  PRIMARY KEY (family, date)
);

CREATE TABLE IF NOT EXISTS backfill_cursor (
  kind TEXT, family TEXT, cursor TEXT, complete INTEGER,
  PRIMARY KEY (kind, family)
);

CREATE TABLE IF NOT EXISTS sync_log (ts TEXT, kind TEXT, calls INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class StoreError(RuntimeError):
    """Store-side failure (missing/corrupt file, unreadable index, I/O)."""


class IndexUnavailable(StoreError):
    """index.db itself is unreadable (the one state api_status diagnoses)."""


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> dt.datetime | None:
    """Parse a stored ISO timestamp (``Z`` or ``+00:00`` aware)."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def has_data(data: Any) -> bool:
    """True when a raw response carries anything at all (same rule as tools)."""
    return data is not None and data != [] and data != {}


def record_date(record: Any) -> dt.date | None:
    """Pull the calendar date out of a day record, tolerating key variants."""
    if not isinstance(record, dict):
        return None
    for key in _DATE_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            try:
                return dt.date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def slice_data(data: Any, start: dt.date, end: dt.date) -> Any | None:
    """Slice a raw ranged response to inclusive [start, end] by date.

    Returns None when the shape is not date-sliceable (the caller then serves
    the raw chunk only when the request window equals the chunk window, and
    falls back live otherwise — wrong data is never assembled). Lists are
    filtered per record date; a dict with exactly one list-of-records value
    (e.g. hrv: {hrvSummaries: [...], userProfilePk}) has that list filtered
    and its scalar keys preserved.
    """
    if data is None:
        return None
    if isinstance(data, list):
        kept = [r for r in data if _record_in(r, start, end)]
        return kept
    if isinstance(data, dict):
        list_items = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if len(list_items) == 1:
            key, values = list_items[0]
            if values and not isinstance(values[0], dict):
                return None
            if not values and any(
                isinstance(v, (list, dict)) for v in data.values() if v is not values
            ):
                return None
            sliced = dict(data)
            sliced[key] = [r for r in values if _record_in(r, start, end)]
            return sliced
        if list_items:
            return None
        return data  # scalar-only object: carries no per-day structure
    return None


def _record_in(record: Any, start: dt.date, end: dt.date) -> bool:
    day = record_date(record)
    if day is None:
        return True  # records without a parseable date are kept (never dropped)
    return start <= day <= end


class Store:
    """The on-disk archive: atomic file writes + a derived SQLite index."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser()
        self._db: sqlite3.Connection | None = None
        self._db_lock = threading.RLock()

    # -- layout -----------------------------------------------------------

    def ensure_dirs(self) -> None:
        """Create the store tree 0700 (M2). Best-effort chmod on existing dirs."""
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for sub in ("activities", "raw", "perday", "point", "state"):
                (self.root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in (self.root, self.root / "state"):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
        except OSError as exc:
            raise StoreError(f"cannot create store dirs under {self.root}: {exc}") from exc

    def activity_dir(self, activity_id: int | str) -> Path:
        return self.root / "activities" / str(activity_id)

    def raw_path(self, family: str, chunk_start: dt.date, chunk_end: dt.date) -> Path:
        return self.root / "raw" / family / f"{chunk_start.isoformat()}_{chunk_end.isoformat()}.json"

    def perday_path(self, family: str, day: dt.date) -> Path:
        return self.root / "perday" / family / f"{day.isoformat()}.json"

    def point_path(self, name: str) -> Path:
        return self.root / "point" / f"{name}.json"

    # -- atomic file io ----------------------------------------------------

    def _write_atomic(self, path: Path, payload: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic on POSIX: readers see old or new
            self._fsync_dir(path.parent)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StoreError(f"store write failed for {path}: {exc}") from exc

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def write_json_atomic(self, path: Path, obj: Any) -> None:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._write_atomic(path, payload)

    def write_bytes_atomic(self, path: Path, data: bytes) -> None:
        self._write_atomic(path, bytes(data))

    def read_json(self, path: Path) -> Any:
        try:
            with open(path, "rb") as fh:
                return json.loads(fh.read().decode("utf-8"))
        except FileNotFoundError as exc:
            raise StoreError(f"missing {path}") from exc
        except (OSError, ValueError) as exc:
            raise StoreError(f"unreadable {path}: {exc}") from exc

    def read_bytes(self, path: Path) -> bytes:
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise StoreError(f"missing {path}") from exc
        except OSError as exc:
            raise StoreError(f"unreadable {path}: {exc}") from exc

    # -- sqlite index (derived, §3.3) ---------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            db_path = self.root / "index.db"
            try:
                conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
                conn.execute("PRAGMA journal_mode=DELETE")  # single-file, backup-friendly
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (STORE_SCHEMA_VERSION,),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise IndexUnavailable(f"index.db unusable: {exc}") from exc
            self._db = conn
        return self._db

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._db_lock:
            try:
                cur = self._connection().execute(sql, params)
                self._db.commit()
                return cur
            except sqlite3.Error as exc:
                raise IndexUnavailable(f"index.db query failed: {exc}") from exc

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._db_lock:
            try:
                return self._connection().execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise IndexUnavailable(f"index.db query failed: {exc}") from exc

    def close(self) -> None:
        with self._db_lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None

    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- activities ---------------------------------------------------------

    def upsert_activity_search(self, record: dict[str, Any], fetched_at: str) -> None:
        """Insert/update the index row from a raw search-page record."""
        activity_id = record.get("activityId")
        if not isinstance(activity_id, int):
            raise StoreError(f"search record without numeric activityId: {activity_id!r}")
        activity_type = record.get("activityType") or {}
        self._execute(
            """
            INSERT INTO activities (
              activity_id, activity_uuid, name, type_key, sport_type_id,
              start_time_gmt, start_local, distance_m, duration_s,
              moving_duration_s, elevation_gain, calories, avg_hr, max_hr,
              steps, owner_id, search_fetched_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(activity_id) DO UPDATE SET
              activity_uuid=excluded.activity_uuid, name=excluded.name,
              type_key=excluded.type_key, sport_type_id=excluded.sport_type_id,
              start_time_gmt=excluded.start_time_gmt, start_local=excluded.start_local,
              distance_m=excluded.distance_m, duration_s=excluded.duration_s,
              moving_duration_s=excluded.moving_duration_s,
              elevation_gain=excluded.elevation_gain, calories=excluded.calories,
              avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, steps=excluded.steps,
              owner_id=excluded.owner_id,
              search_fetched_at=excluded.search_fetched_at, updated_at=excluded.updated_at
            """,
            (
                activity_id,
                record.get("activityUUID"),
                record.get("activityName"),
                activity_type.get("typeKey") if isinstance(activity_type, dict) else None,
                record.get("sportTypeId"),
                record.get("startTimeGMT"),
                record.get("startTimeLocal"),
                record.get("distance"),
                record.get("duration"),
                record.get("movingDuration"),
                record.get("elevationGain"),
                record.get("calories"),
                record.get("averageHR"),
                record.get("maxHR"),
                record.get("steps"),
                record.get("ownerId"),
                fetched_at,
                fetched_at,
            ),
        )

    def has_activity(self, activity_id: int) -> bool:
        rows = self._query(
            "SELECT 1 FROM activities WHERE activity_id = ? AND search_fetched_at IS NOT NULL",
            (activity_id,),
        )
        return bool(rows)

    def activity_row(self, activity_id: int) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        )
        if not rows:
            return None
        with self._db_lock:
            names = [c[0] for c in self._connection().execute(
                "SELECT * FROM activities LIMIT 0").description]
        return dict(zip(names, rows[0]))

    def set_activity_marker(self, activity_id: int, marker: str, fetched_at: str,
                            zip_sha256: str | None = None, zip_size: int | None = None) -> None:
        if marker not in ("detail_fetched_at", "workouts_fetched_at", "zip_fetched_at"):
            raise ValueError(f"unknown activity marker {marker!r}")
        sets = f"{marker} = ?, updated_at = ?"
        params: list[Any] = [fetched_at, fetched_at]
        if marker == "zip_fetched_at":
            sets = f"{marker} = ?, zip_sha256 = ?, zip_size = ?, updated_at = ?"
            params = [fetched_at, zip_sha256, zip_size, fetched_at]
        self._execute(
            f"UPDATE activities SET {sets} WHERE activity_id = ?",
            (*params, activity_id),
        )

    def activities_pending_payloads(self, limit: int = 500) -> list[int]:
        """Ids with any payload marker missing (backfill stage 2), newest first."""
        rows = self._query(
            """
            SELECT activity_id FROM activities
            WHERE detail_fetched_at IS NULL OR workouts_fetched_at IS NULL
               OR zip_fetched_at IS NULL
            ORDER BY start_time_gmt DESC, activity_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [r[0] for r in rows]

    def newest_search_fetched_at(self) -> str | None:
        rows = self._query(
            "SELECT MAX(search_fetched_at) FROM activities WHERE search_fetched_at IS NOT NULL"
        )
        return rows[0][0] if rows and rows[0][0] else None

    def search_record_ids(self, since_date: str | None, limit: int) -> list[int]:
        """Newest-first ids for the recent_activities read (§3.5)."""
        if since_date:
            rows = self._query(
                """
                SELECT activity_id FROM activities
                WHERE search_fetched_at IS NOT NULL AND substr(start_time_gmt, 1, 10) >= ?
                ORDER BY start_time_gmt DESC, activity_id DESC LIMIT ?
                """,
                (since_date, limit),
            )
        else:
            rows = self._query(
                """
                SELECT activity_id FROM activities
                WHERE search_fetched_at IS NOT NULL
                ORDER BY start_time_gmt DESC, activity_id DESC LIMIT ?
                """,
                (limit,),
            )
        return [r[0] for r in rows]

    def newest_activity_ids(self, limit: int) -> list[int]:
        """Newest-first ids for the OD-8 weekly re-verify."""
        rows = self._query(
            """
            SELECT activity_id FROM activities WHERE search_fetched_at IS NOT NULL
            ORDER BY start_time_gmt DESC, activity_id DESC LIMIT ?
            """,
            (limit,),
        )
        return [r[0] for r in rows]

    def counts(self) -> tuple[int, int]:
        """(activities indexed, zips stored)."""
        acts = self._query("SELECT COUNT(*) FROM activities")[0][0]
        zips = self._query("SELECT COUNT(*) FROM activities WHERE zip_fetched_at IS NOT NULL")[0][0]
        return int(acts), int(zips)

    def log(self, kind: str, calls: int, detail: str) -> None:
        self._execute(
            "INSERT INTO sync_log(ts, kind, calls, detail) VALUES (?,?,?,?)",
            (utc_now_iso(), kind, calls, detail),
        )

    # -- coverage / daily_records -------------------------------------------

    def record_coverage(self, family: str, chunk_start: dt.date, chunk_end: dt.date,
                        byte_size: int, fetched_at: str) -> None:
        self._execute(
            """
            INSERT INTO coverage(family, chunk_start, chunk_end, fetched_at, byte_size)
            VALUES (?,?,?,?,?)
            ON CONFLICT(family, chunk_start, chunk_end) DO UPDATE SET
              fetched_at = excluded.fetched_at, byte_size = excluded.byte_size
            """,
            (family, chunk_start.isoformat(), chunk_end.isoformat(), fetched_at, byte_size),
        )

    def coverage_row(self, family: str, chunk_start: dt.date, chunk_end: dt.date) -> str | None:
        rows = self._query(
            """
            SELECT fetched_at FROM coverage
            WHERE family = ? AND chunk_start = ? AND chunk_end = ?
            """,
            (family, chunk_start.isoformat(), chunk_end.isoformat()),
        )
        return rows[0][0] if rows else None

    def coverage_rows(self, family: str | None = None) -> list[tuple[str, str, str, str]]:
        if family is None:
            rows = self._query(
                "SELECT family, chunk_start, chunk_end, fetched_at FROM coverage "
                "ORDER BY family, chunk_start"
            )
        else:
            rows = self._query(
                "SELECT family, chunk_start, chunk_end, fetched_at FROM coverage "
                "WHERE family = ? ORDER BY chunk_start",
                (family,),
            )
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def record_daily(self, family: str, day: dt.date, present: bool, fetched_at: str) -> None:
        self._execute(
            """
            INSERT INTO daily_records(family, date, present, fetched_at)
            VALUES (?,?,?,?)
            ON CONFLICT(family, date) DO UPDATE SET
              present = excluded.present, fetched_at = excluded.fetched_at
            """,
            (family, day.isoformat(), 1 if present else 0, fetched_at),
        )

    def daily_fetched_at(self, family: str, day: dt.date) -> str | None:
        rows = self._query(
            "SELECT fetched_at FROM daily_records WHERE family = ? AND date = ?",
            (family, day.isoformat()),
        )
        return rows[0][0] if rows else None

    def daily_rows(self, family: str) -> list[tuple[str, int, str]]:
        rows = self._query(
            "SELECT date, present, fetched_at FROM daily_records WHERE family = ? ORDER BY date",
            (family,),
        )
        return [(r[0], int(r[1]), r[2]) for r in rows]

    def has_daily_row(self, family: str, day: dt.date) -> bool:
        rows = self._query(
            "SELECT 1 FROM daily_records WHERE family = ? AND date = ?",
            (family, day.isoformat()),
        )
        return bool(rows)

    # -- chunk + perday write-throughs (used by sync and read path) ----------

    def write_ranged_chunk(self, family: str, chunk_start: dt.date, chunk_end: dt.date,
                           data: Any) -> None:
        """Store one canonical chunk raw response + coverage + derived perday."""
        now = utc_now_iso()
        path = self.raw_path(family, chunk_start, chunk_end)
        self.write_json_atomic(path, data)
        self.record_coverage(family, chunk_start, chunk_end, path.stat().st_size, now)
        self.derive_perday_from_chunk(family, chunk_start, chunk_end, data, now)

    def derive_perday_from_chunk(self, family: str, chunk_start: dt.date, chunk_end: dt.date,
                                 data: Any, fetched_at: str) -> None:
        """Re-derive the per-day files for a chunk's days (ranged families, §3.1)."""
        day = chunk_start
        while day <= chunk_end:
            sliced = slice_data(data, day, day)
            present = has_data(sliced) if sliced is not None else has_data(data)
            if sliced is not None:
                self.write_json_atomic(self.perday_path(family, day), sliced)
            self.record_daily(family, day, present, fetched_at)
            day += dt.timedelta(days=1)

    def write_perday(self, family: str, day: dt.date, data: Any) -> None:
        """Atomic perday replace + daily_records update (§3.5)."""
        now = utc_now_iso()
        self.write_json_atomic(self.perday_path(family, day), data)
        self.record_daily(family, day, has_data(data), now)

    # -- status helpers (§5.3) ------------------------------------------------

    def coverage_summary(self, family: str, cap_days: int, anchor: dt.date | None,
                         today: dt.date) -> dict[str, Any]:
        rows = self.coverage_rows(family)
        if anchor is None or not rows:
            return {"chunks": len(rows), "holes": 0,
                    "range": [rows[0][1], rows[-1][2]] if rows else None}
        expected = families.canonical_chunks(anchor, cap_days, anchor, today)
        have = {(r[1], r[2]) for r in rows}
        holes = sum(1 for cs, ce in expected if (cs.isoformat(), ce.isoformat()) not in have)
        return {"chunks": len(rows), "holes": holes,
                "range": [rows[0][1], rows[-1][2]]}

    def last_sync_per_family(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for family, _cs, _ce, fetched_at in self.coverage_rows():
            prev = out.get(family)
            if prev is None or fetched_at > prev:
                out[family] = fetched_at
        for family, _date, _present, fetched_at in self.daily_rows_all():
            prev = out.get(family)
            if prev is None or fetched_at > prev:
                out[family] = fetched_at
        return out

    def daily_rows_all(self) -> list[tuple[str, str, int, str]]:
        rows = self._query(
            "SELECT family, date, present, fetched_at FROM daily_records ORDER BY family, date"
        )
        return [(r[0], r[1], int(r[2]), r[3]) for r in rows]

    # -- reindex (§5.4): rebuild the index from the files alone ---------------

    def reindex(self) -> dict[str, int]:
        """Drop and rebuild every derived table from the on-disk files."""
        with self._db_lock:
            conn = self._connection()
            try:
                conn.executescript(
                    """
                    DROP TABLE IF EXISTS activities;
                    DROP TABLE IF EXISTS coverage;
                    DROP TABLE IF EXISTS daily_records;
                    DROP TABLE IF EXISTS backfill_cursor;
                    DROP TABLE IF EXISTS sync_log;
                    DROP TABLE IF EXISTS meta;
                    """
                )
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (STORE_SCHEMA_VERSION,),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise IndexUnavailable(f"reindex failed: {exc}") from exc
        stats = {"activities": 0, "chunks": 0, "days": 0}
        acts_dir = self.root / "activities"
        if acts_dir.is_dir():
            for entry in sorted(acts_dir.iterdir()):
                if not entry.is_dir() or not entry.name.isdecimal():
                    continue
                search = entry / "search-record.json"
                try:
                    record = self.read_json(search)
                except StoreError:
                    continue
                mtime = _mtime_iso(search)
                self.upsert_activity_search(record, mtime)
                stats["activities"] += 1
                detail = entry / "detail.json"
                if detail.exists():
                    self.set_activity_marker(int(entry.name), "detail_fetched_at", _mtime_iso(detail))
                workouts = entry / "workouts.json"
                if workouts.exists():
                    self.set_activity_marker(int(entry.name), "workouts_fetched_at", _mtime_iso(workouts))
                zipped = entry / "file.zip"
                if zipped.exists():
                    blob = self.read_bytes(zipped)
                    self.set_activity_marker(
                        int(entry.name), "zip_fetched_at", _mtime_iso(zipped),
                        zip_sha256=hashlib.sha256(blob).hexdigest(), zip_size=len(blob),
                    )
        raw_dir = self.root / "raw"
        if raw_dir.is_dir():
            for family_dir in sorted(raw_dir.iterdir()):
                if not family_dir.is_dir():
                    continue
                for chunk_file in sorted(family_dir.glob("*.json")):
                    stem = chunk_file.stem
                    parts = stem.split("_")
                    if len(parts) != 2:
                        continue
                    try:
                        cs = dt.date.fromisoformat(parts[0])
                        ce = dt.date.fromisoformat(parts[1])
                    except ValueError:
                        continue
                    self.record_coverage(family_dir.name, cs, ce, chunk_file.stat().st_size,
                                         _mtime_iso(chunk_file))
                    stats["chunks"] += 1
        perday_dir = self.root / "perday"
        if perday_dir.is_dir():
            for family_dir in sorted(perday_dir.iterdir()):
                if not family_dir.is_dir():
                    continue
                for day_file in sorted(family_dir.glob("*.json")):
                    try:
                        day = dt.date.fromisoformat(day_file.stem)
                    except ValueError:
                        continue
                    try:
                        data = self.read_json(day_file)
                    except StoreError:
                        continue
                    self.record_daily(family_dir.name, day, has_data(data),
                                      _mtime_iso(day_file))
                    stats["days"] += 1
        state = self.root / "state" / "sync-state.json"
        if state.exists():
            try:
                anchor = json.loads(state.read_text(encoding="utf-8")).get("anchor")
                if anchor:
                    self.set_meta("anchor", str(anchor))
            except (OSError, ValueError):
                pass
        return stats


def _mtime_iso(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).isoformat(timespec="seconds")
