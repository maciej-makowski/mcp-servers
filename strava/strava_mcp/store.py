"""The Strava local store: SQLite index + flat per-activity JSON files
(strava-local-store design section 3.2, artifact A3).

Layout (all paths relative to the store root, /app/store in the container;
the quadlet mounts the mcp-owned host directory /home/mcp/strava-store there):

    index.db                       SQLite index (WAL; rebuildable)
    activities/<id>/summary.json   list/summary payload (raw)
    activities/<id>/detail.json    detail payload incl. segment efforts
    activities/<id>/streams.json   the fixed 11-key streams payload
    activities/<id>/photos/<n>.*   primary photo bytes + sidecar url.txt
    gear/<gear_id>.json
    athlete/<ts>.json              profile snapshots (only when changed)
    stats/<date>.json              daily stats snapshots
    zones/<ts>.json                zone snapshots
    segments/<id>.json             starred segments (post-scope)
    routes/<id>.json + streams.json + route.gpx (post-scope)

Primary data is the FILES; index.db only carries what queries and the sync
loop need plus the paths and fingerprints that tie rows to files (A3). JSON
files are never deleted: a Strava-side deletion marks the row (deleted_at)
and keeps the archive (D10). Every write is atomic: temp file + rename for
JSON, transactions for SQLite. The read path (D9's source=store) serves from
these files and the index and makes zero API requests.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
from typing import Any

STORE_DIR = "/app/store"  # hardcoded like oauth's /app/data/token.json (D3)
STORE_SCHEMA_VERSION = 1

# The compact list shape the tools layer serves for both sources; the
# activities table carries exactly these columns (A3).
SUMMARY_COLUMNS = (
    "id",
    "sport_type",
    "start_date",
    "name",
    "distance",
    "moving_time",
    "elapsed_time",
    "total_elevation_gain",
    "private",
    "gear_id",
    "total_photo_count",
    "kudos_count",
    "comment_count",
    "achievement_count",
    "workout_type",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
  id                  INTEGER PRIMARY KEY,
  sport_type          TEXT,
  start_date          TEXT,
  start_date_local    TEXT,
  name                TEXT,
  distance            REAL,
  moving_time         INTEGER,
  elapsed_time        INTEGER,
  total_elevation_gain REAL,
  private             INTEGER,
  visibility          TEXT,
  gear_id             TEXT,
  total_photo_count   INTEGER,
  kudos_count         INTEGER,
  comment_count       INTEGER,
  achievement_count   INTEGER,
  workout_type        INTEGER,
  external_id         TEXT,
  upload_id           INTEGER,
  summary_sha256      TEXT,
  detail_fetched_at   TEXT,
  detail_sha256       TEXT,
  streams_fetched_at  TEXT,
  streams_sha256      TEXT,
  photos_fetched_at   TEXT,
  first_seen_at       TEXT NOT NULL,
  last_checked_at     TEXT,
  deleted_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_date);
CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport_type);
CREATE INDEX IF NOT EXISTS idx_activities_gear  ON activities(gear_id);
CREATE INDEX IF NOT EXISTS idx_activities_open  ON activities(deleted_at);
CREATE TABLE IF NOT EXISTS gear (
  id              TEXT PRIMARY KEY,
  name            TEXT,
  retired         INTEGER,
  distance        REAL,
  detail_sha256   TEXT,
  last_checked_at TEXT,
  deleted_at      TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  kind         TEXT NOT NULL,
  key          TEXT NOT NULL,
  path         TEXT NOT NULL,
  sha256       TEXT NOT NULL,
  fetched_at   TEXT NOT NULL,
  PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS segments (
  id              INTEGER PRIMARY KEY,
  name            TEXT,
  activity_type   TEXT,
  distance        REAL,
  climb_category  INTEGER,
  starred         INTEGER,
  private         INTEGER,
  detail_sha256   TEXT,
  last_checked_at TEXT,
  deleted_at      TEXT
);
CREATE TABLE IF NOT EXISTS routes (
  id              INTEGER PRIMARY KEY,
  name            TEXT,
  updated_at      TEXT,
  distance        REAL,
  elevation_gain  REAL,
  private         INTEGER,
  detail_sha256   TEXT,
  streams_sha256  TEXT,
  gpx_sha256      TEXT,
  last_checked_at TEXT,
  deleted_at      TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS sync_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tier        TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  outcome     TEXT,
  api_requests INTEGER DEFAULT 0,
  added       INTEGER DEFAULT 0,
  updated     INTEGER DEFAULT 0,
  deleted     INTEGER DEFAULT 0,
  errors      INTEGER DEFAULT 0,
  note        TEXT
);
"""


class StoreError(RuntimeError):
    """Store open/layout/schema failure surfaced to the tool layer."""


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Store:
    """The local store: files under root, index.db beside them (A3)."""

    def __init__(self, root: str | None = None) -> None:
        self.root = root if root is not None else STORE_DIR

    # -- open / schema -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        db_path = os.path.join(self.root, "index.db")
        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open store index at {db_path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_dirs(self) -> None:
        try:
            for sub in ("activities", "gear", "athlete", "stats", "zones",
                        "segments", "routes"):
                os.makedirs(os.path.join(self.root, sub), exist_ok=True)
            os.chmod(self.root, 0o700)
        except OSError as exc:
            raise StoreError(f"cannot create store layout under {self.root}: {exc}") from exc

    def init_schema(self) -> None:
        """Create the schema if needed; write the version row only when empty."""
        self.ensure_dirs()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
            if row["n"] == 0:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (STORE_SCHEMA_VERSION,),
                )

    def check_schema(self) -> int:
        """Refuse to serve or sync against an unknown layout (section 3.6)."""
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None or row["version"] != STORE_SCHEMA_VERSION:
            raise StoreError(
                f"store schema at {self.root} is missing or has version "
                f"{row['version'] if row else 'none'} (this build expects "
                f"{STORE_SCHEMA_VERSION}): rebuild the store index"
            )
        return STORE_SCHEMA_VERSION

    # -- atomic JSON writes ---------------------------------------------------

    def _activity_dir(self, activity_id: int) -> str:
        d = os.path.join(self.root, "activities", str(activity_id))
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def write_json_atomic(path: str, payload: Any) -> str:
        """Temp file + os.replace + fsync; returns the sha256 of the bytes."""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        raw = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return _sha256_bytes(raw)

    @staticmethod
    def read_json(path: str) -> Any | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise StoreError(f"store file at {path} is not valid JSON: {exc}") from exc

    def write_bytes_atomic(self, path: str, raw: bytes) -> str:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return _sha256_bytes(raw)

    # -- activities ------------------------------------------------------------

    def upsert_activity_summary(self, summary: dict[str, Any],
                                now: str | None = None) -> str:
        """Write summary.json and upsert the row; returns added|updated|unchanged.

        The fingerprint is the sha256 of the summary.json bytes, so a repeat
        walk over an unchanged activity costs one file write only when the
        upstream summary actually changed (section 3.2 idempotency).
        """
        now = now or utc_now_iso()
        activity_id = summary.get("id")
        if not isinstance(activity_id, int):
            raise StoreError(f"activity summary carries no integer id: {activity_id!r}")
        adir = self._activity_dir(activity_id)
        path = os.path.join(adir, "summary.json")
        sha = self.write_json_atomic(path, summary)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary_sha256, deleted_at FROM activities WHERE id = ?",
                (activity_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO activities
                       (id, sport_type, start_date, start_date_local, name,
                        distance, moving_time, elapsed_time,
                        total_elevation_gain, private, gear_id,
                        total_photo_count, kudos_count, comment_count,
                        achievement_count, workout_type, external_id,
                        upload_id, summary_sha256, first_seen_at,
                        last_checked_at, deleted_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                    (activity_id, summary.get("sport_type"),
                     summary.get("start_date"), summary.get("start_date_local"),
                     summary.get("name"), summary.get("distance"),
                     summary.get("moving_time"), summary.get("elapsed_time"),
                     summary.get("total_elevation_gain"),
                     1 if summary.get("private") else 0,
                     summary.get("gear_id"), summary.get("total_photo_count"),
                     summary.get("kudos_count"), summary.get("comment_count"),
                     summary.get("achievement_count"),
                     summary.get("workout_type"), summary.get("external_id"),
                     summary.get("upload_id"), sha, now, now),
                )
                return "added"
            if row["summary_sha256"] != sha:
                conn.execute(
                    """UPDATE activities SET
                       sport_type=?, start_date=?, start_date_local=?, name=?,
                       distance=?, moving_time=?, elapsed_time=?,
                       total_elevation_gain=?, private=?, gear_id=?,
                       total_photo_count=?, kudos_count=?, comment_count=?,
                       achievement_count=?, workout_type=?, external_id=?,
                       upload_id=?, summary_sha256=?, last_checked_at=?,
                       deleted_at=NULL
                       WHERE id=?""",
                    (summary.get("sport_type"), summary.get("start_date"),
                     summary.get("start_date_local"), summary.get("name"),
                     summary.get("distance"), summary.get("moving_time"),
                     summary.get("elapsed_time"),
                     summary.get("total_elevation_gain"),
                     1 if summary.get("private") else 0,
                     summary.get("gear_id"), summary.get("total_photo_count"),
                     summary.get("kudos_count"), summary.get("comment_count"),
                     summary.get("achievement_count"),
                     summary.get("workout_type"), summary.get("external_id"),
                     summary.get("upload_id"), sha, now, activity_id),
                )
                return "updated"
            conn.execute(
                "UPDATE activities SET last_checked_at=? WHERE id=?",
                (now, activity_id),
            )
        return "unchanged"

    def save_detail(self, activity_id: int, detail: dict[str, Any],
                    now: str | None = None) -> bool:
        """Write detail.json; returns True when the payload changed (sweep)."""
        now = now or utc_now_iso()
        adir = self._activity_dir(activity_id)
        path = os.path.join(adir, "detail.json")
        raw = json.dumps(detail, indent=1, sort_keys=True).encode("utf-8")
        sha = self.write_bytes_atomic(path, raw)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail_sha256 FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()
            changed = row is None or row["detail_sha256"] != sha
            conn.execute(
                """INSERT INTO activities (id, first_seen_at) VALUES (?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (activity_id, now),
            )
            conn.execute(
                """UPDATE activities SET detail_fetched_at=?, detail_sha256=?,
                   visibility=? WHERE id=?""",
                (now, sha, detail.get("visibility"), activity_id),
            )
        return changed

    def save_streams(self, activity_id: int, payload: dict[str, Any],
                     now: str | None = None) -> bool:
        now = now or utc_now_iso()
        adir = self._activity_dir(activity_id)
        path = os.path.join(adir, "streams.json")
        raw = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
        sha = self.write_bytes_atomic(path, raw)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT streams_sha256 FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()
            changed = row is None or row["streams_sha256"] != sha
            conn.execute(
                """INSERT INTO activities (id, first_seen_at) VALUES (?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (activity_id, now),
            )
            conn.execute(
                "UPDATE activities SET streams_fetched_at=?, streams_sha256=? WHERE id=?",
                (now, sha, activity_id),
            )
        return changed

    def save_primary_photo(self, activity_id: int, raw: bytes, ext: str,
                           source_url: str, now: str | None = None) -> str:
        """Store the primary photo bytes + a sidecar with the source URL (D7)."""
        now = now or utc_now_iso()
        adir = self._activity_dir(activity_id)
        pdir = os.path.join(adir, "photos")
        os.makedirs(pdir, exist_ok=True)
        path = os.path.join(pdir, f"primary.{ext}")
        self.write_bytes_atomic(path, raw)
        self.write_json_atomic(
            os.path.join(pdir, "primary.source.json"),
            {"url": source_url, "ext": ext, "fetched_at": now, "bytes": len(raw)},
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO activities (id, first_seen_at) VALUES (?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (activity_id, now),
            )
            conn.execute(
                "UPDATE activities SET photos_fetched_at=? WHERE id=?",
                (now, activity_id),
            )
        return path

    def activity_row(self, activity_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()
        return dict(row) if row else None

    def open_activity_ids(self) -> list[int]:
        """All ids with deleted_at NULL (section 3.6 reconciliation set)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM activities WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
        return [r["id"] for r in rows]

    def mark_deleted(self, ids: list[int], now: str | None = None) -> int:
        """Sweep: rows whose ids vanished from the list (D10: files stay)."""
        if not ids:
            return 0
        now = now or utc_now_iso()
        with self._connect() as conn:
            cur = conn.executemany(
                "UPDATE activities SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                [(now, i) for i in ids],
            )
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        """Store counters for the strava_sync status action (section 5)."""
        with self._connect() as conn:
            one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            out = {
                "activities": one("SELECT COUNT(*) FROM activities"),
                "with_details": one(
                    "SELECT COUNT(*) FROM activities WHERE detail_sha256 IS NOT NULL"),
                "with_streams": one(
                    "SELECT COUNT(*) FROM activities WHERE streams_sha256 IS NOT NULL"),
                "with_photos": one(
                    "SELECT COUNT(*) FROM activities WHERE photos_fetched_at IS NOT NULL"),
                "deleted": one(
                    "SELECT COUNT(*) FROM activities WHERE deleted_at IS NOT NULL"),
                "gear": one("SELECT COUNT(*) FROM gear"),
                "snapshots": one("SELECT COUNT(*) FROM snapshots"),
                "segments": one("SELECT COUNT(*) FROM segments"),
                "routes": one("SELECT COUNT(*) FROM routes"),
            }
        return out

    def summaries_missing_backfill(self, cursor: int | None = None) -> list[dict[str, Any]]:
        """Open rows still lacking detail+streams, oldest-first (backfill set).

        The cursor is the oldest id not yet processed; rows with id < cursor
        are considered done. An empty list means the backfill is complete.
        """
        q = """SELECT id FROM activities
               WHERE deleted_at IS NULL AND (detail_sha256 IS NULL
                    OR streams_sha256 IS NULL)"""
        params: list[Any] = []
        if cursor is not None:
            q += " AND id >= ?"
            params.append(cursor)
        q += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # -- read path (D9 source=store) --------------------------------------------

    def list_summaries(self, page: int = 1, per_page: int = 30,
                       after: int | None = None, before: int | None = None,
                       ) -> list[dict[str, Any]]:
        """Compact list from the index, newest first, API page semantics."""
        q = "SELECT * FROM activities WHERE deleted_at IS NULL"
        params: list[Any] = []
        if after is not None:
            dt_after = dt.datetime.fromtimestamp(after, dt.timezone.utc)
            q += " AND start_date > ?"
            params.append(dt_after.isoformat())
        if before is not None:
            dt_before = dt.datetime.fromtimestamp(before, dt.timezone.utc)
            q += " AND start_date < ?"
            params.append(dt_before.isoformat())
        q += " ORDER BY start_date DESC LIMIT ? OFFSET ?"
        params += [per_page, (page - 1) * per_page]
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [
            {k: row[k] for k in SUMMARY_COLUMNS if k in row.keys()} for row in rows
        ]

    def detail_payload(self, activity_id: int) -> dict[str, Any] | None:
        path = os.path.join(self.root, "activities", str(activity_id), "detail.json")
        payload = self.read_json(path)
        return payload if isinstance(payload, dict) else None

    def summary_raw(self, activity_id: int) -> dict[str, Any] | None:
        """The stored raw summary (the exact API list item shape source)."""
        path = os.path.join(self.root, "activities", str(activity_id), "summary.json")
        payload = self.read_json(path)
        return payload if isinstance(payload, dict) else None

    def streams_payload(self, activity_id: int) -> dict[str, Any] | None:
        path = os.path.join(self.root, "activities", str(activity_id), "streams.json")
        payload = self.read_json(path)
        return payload if isinstance(payload, dict) else None

    # -- gear / snapshots / segments / routes -----------------------------------

    def gear_ids_seen(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT gear_id FROM activities
                   WHERE gear_id IS NOT NULL AND deleted_at IS NULL"""
            ).fetchall()
        return [r["gear_id"] for r in rows]

    def save_gear(self, gear: dict[str, Any], now: str | None = None) -> bool:
        now = now or utc_now_iso()
        gear_id = gear.get("id")
        if not gear_id:
            raise StoreError(f"gear payload carries no id: {gear!r}")
        path = os.path.join(self.root, "gear", f"{gear_id}.json")
        raw = json.dumps(gear, indent=1, sort_keys=True).encode("utf-8")
        sha = _sha256_bytes(raw)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail_sha256 FROM gear WHERE id = ?", (gear_id,)
            ).fetchone()
            changed = row is None or row["detail_sha256"] != sha
            if changed:
                self.write_bytes_atomic(path, raw)
            conn.execute(
                """INSERT INTO gear (id, name, retired, distance, detail_sha256,
                                     last_checked_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     retired=excluded.retired, distance=excluded.distance,
                     detail_sha256=excluded.detail_sha256,
                     last_checked_at=excluded.last_checked_at""",
                (gear_id, gear.get("name"), 1 if gear.get("retired") else 0,
                 gear.get("distance"), sha, now),
            )
        return changed

    def save_snapshot(self, kind: str, key: str, payload: Any,
                      now: str | None = None) -> bool:
        """Snapshot registry: a new file only when the payload changed (A3)."""
        now = now or utc_now_iso()
        raw = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
        sha = _sha256_bytes(raw)
        sub = {"athlete": "athlete", "stats": "stats", "zones": "zones"}[kind]
        path = os.path.join(self.root, sub, f"{key}.json")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sha256 FROM snapshots WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
            if row is not None and row["sha256"] == sha:
                return False
            self.write_bytes_atomic(path, raw)
            conn.execute(
                """INSERT INTO snapshots (kind, key, path, sha256, fetched_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(kind, key) DO UPDATE SET path=excluded.path,
                     sha256=excluded.sha256, fetched_at=excluded.fetched_at""",
                (kind, key, path, sha, now),
            )
        return True

    def save_segment(self, segment: dict[str, Any], now: str | None = None) -> bool:
        now = now or utc_now_iso()
        seg_id = segment.get("id")
        if not isinstance(seg_id, int):
            raise StoreError(f"segment payload carries no integer id: {seg_id!r}")
        path = os.path.join(self.root, "segments", f"{seg_id}.json")
        raw = json.dumps(segment, indent=1, sort_keys=True).encode("utf-8")
        sha = _sha256_bytes(raw)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail_sha256 FROM segments WHERE id = ?", (seg_id,)
            ).fetchone()
            changed = row is None or row["detail_sha256"] != sha
            if changed:
                self.write_bytes_atomic(path, raw)
            conn.execute(
                """INSERT INTO segments (id, name, activity_type, distance,
                     climb_category, starred, private, detail_sha256,
                     last_checked_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     activity_type=excluded.activity_type,
                     distance=excluded.distance,
                     climb_category=excluded.climb_category,
                     starred=excluded.starred, private=excluded.private,
                     detail_sha256=excluded.detail_sha256,
                     last_checked_at=excluded.last_checked_at""",
                (seg_id, segment.get("name"), segment.get("activity_type"),
                 segment.get("distance"), segment.get("climb_category"),
                 1 if segment.get("starred") else 0,
                 1 if segment.get("private") else 0, sha, now),
            )
        return changed

    def save_route(self, route: dict[str, Any], now: str | None = None) -> bool:
        now = now or utc_now_iso()
        route_id = route.get("id")
        if not isinstance(route_id, int):
            raise StoreError(f"route payload carries no integer id: {route_id!r}")
        path = os.path.join(self.root, "routes", f"{route_id}.json")
        raw = json.dumps(route, indent=1, sort_keys=True).encode("utf-8")
        sha = _sha256_bytes(raw)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail_sha256 FROM routes WHERE id = ?", (route_id,)
            ).fetchone()
            changed = row is None or row["detail_sha256"] != sha
            if changed:
                self.write_bytes_atomic(path, raw)
            conn.execute(
                """INSERT INTO routes (id, name, updated_at, distance,
                     elevation_gain, private, detail_sha256, last_checked_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     updated_at=excluded.updated_at, distance=excluded.distance,
                     elevation_gain=excluded.elevation_gain,
                     private=excluded.private,
                     detail_sha256=excluded.detail_sha256,
                     last_checked_at=excluded.last_checked_at""",
                (route_id, route.get("name"), route.get("updated_at"),
                 route.get("distance"), route.get("elevation_gain"),
                 1 if route.get("private") else 0, sha, now),
            )
        return changed

    def save_route_aux(self, route_id: int, streams: dict[str, Any] | None,
                       gpx: bytes | None, now: str | None = None) -> None:
        now = now or utc_now_iso()
        updates: dict[str, Any] = {"last_checked_at": now}
        if streams is not None:
            raw = json.dumps(streams, indent=1, sort_keys=True).encode("utf-8")
            updates["streams_sha256"] = self.write_bytes_atomic(
                os.path.join(self.root, "routes", str(route_id), "streams.json"),
                raw,
            )
        if gpx is not None:
            updates["gpx_sha256"] = self.write_bytes_atomic(
                os.path.join(self.root, "routes", str(route_id), "route.gpx"), gpx
            )
        sets = ", ".join(f"{k}=?" for k in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE routes SET {sets} WHERE id=?",
                (*updates.values(), route_id),
            )

    # -- sync_state + sync_log ---------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO sync_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def log_batch_start(self, tier: str, now: str | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_log (tier, started_at) VALUES (?, ?)",
                (tier, now or utc_now_iso()),
            )
            return int(cur.lastrowid or 0)

    def log_batch_end(self, batch_id: int, outcome: str, api_requests: int = 0,
                      added: int = 0, updated: int = 0, deleted: int = 0,
                      errors: int = 0, note: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE sync_log SET finished_at=?, outcome=?, api_requests=?,
                   added=?, updated=?, deleted=?, errors=?, note=? WHERE id=?""",
                (utc_now_iso(), outcome, api_requests, added, updated, deleted,
                 errors, note, batch_id),
            )

    def last_batches(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT tier, started_at, finished_at, outcome, api_requests,
                          added, updated, deleted, errors, note
                   FROM sync_log ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_run_per_tier(self) -> dict[str, dict[str, Any]]:
        """Most recent finished batch per tier (what status reads out)."""
        out: dict[str, dict[str, Any]] = {}
        for tier in ("frequent", "slow", "sweep", "backfill"):
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT started_at, finished_at, outcome, api_requests,
                              added, updated, deleted, errors
                       FROM sync_log WHERE tier = ? AND finished_at IS NOT NULL
                       ORDER BY id DESC LIMIT 1""",
                    (tier,),
                ).fetchone()
            out[tier] = dict(row) if row else {}
        return out
