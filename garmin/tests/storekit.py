"""Shared helpers for the archive tests: store builders, fake clients,
fake clocks. No network anywhere — every upstream call site is faked at the
module level (house convention, AGENTS.md)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time

from garmin_mcp import readpath
from garmin_mcp.store import Store

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

CAPTURE_DAY = dt.date(2026, 11, 12)  # every capture ends on this complete day


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def make_store(tmp_path, anchor: dt.date | None = None) -> Store:
    store = Store(tmp_path / "garmin-store")
    store.ensure_dirs()
    if anchor is not None:
        store.set_meta("anchor", anchor.isoformat())
    return store


def seed_ranged_chunk(store: Store, family: str, chunk_start: dt.date,
                      chunk_end: dt.date, data, fetched_at=None):
    """Store one canonical chunk + coverage + derived perday, optionally with
    a fixed fetched_at (for staleness tests) applied to coverage AND the
    derived daily_records rows."""
    store.write_ranged_chunk(family, chunk_start, chunk_end, data)
    if fetched_at is not None:
        store.record_coverage(family, chunk_start, chunk_end,
                              store.raw_path(family, chunk_start, chunk_end).stat().st_size,
                              fetched_at)
        day = chunk_start
        while day <= chunk_end:
            store.record_daily(family, day, store.has_daily_row(family, day),
                               fetched_at)
            day += dt.timedelta(days=1)


def seed_perday(store: Store, family: str, day: dt.date, data, fetched_at=None):
    store.write_perday(family, day, data)
    if fetched_at is not None:
        store.record_daily(family, day, data not in (None, [], {}), fetched_at)


def enable_read_through(store: Store) -> None:
    """Point the module-level read path at a ready store (init checks pass)."""
    readpath.reset_for_tests(store, "read_through")


def disable_store() -> None:
    readpath.reset_for_tests(None, "off")


class FakeClock:
    """Controllable wall clock (epoch seconds) for engine tests."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def utc(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.now, tz=dt.timezone.utc)


class SleepingClock:
    """Records sleep() calls and applies them to a FakeClock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


class FakeStatusException(RuntimeError):
    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class FakeClient:
    """Scripted stand-in for garmin_mcp.client (module-level call sites)."""

    def __init__(self) -> None:
        self.ranged_calls: list[tuple] = []
        self.perday_calls: list[tuple] = []
        self.point_calls: list[str] = []
        self.page_calls: list[tuple] = []
        self.detail_calls: list[str] = []
        self.workout_calls: list[str] = []
        self.zip_calls: list[str] = []
        self.ranged_script: dict[str, list] = {}
        self.perday_script: dict[str, list] = {}
        self.point_script: dict[str, list] = {}
        self.page_script: list = []
        self.detail_script: list = []
        self.workout_script: list = []
        self.zip_script: list = []
        self.family_errors: dict[str, dict] = {}

    # -- scripts: queue per key; a raised exception instance is raised, a
    #    callable is called, anything else is returned; empty queue returns []
    def _next(self, queue: list, key: str):
        if not queue:
            return []
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(key)
        return item

    def record_family_ok(self, family: str) -> None:
        self.family_errors.pop(family, None)

    def record_family_error(self, family: str, exc: BaseException) -> None:
        self.family_errors[family] = {"error": str(exc),
                                      "status_code": getattr(exc, "status_code", None),
                                      "ts": time.time()}

    # -- archive dispatch (sync + read path)
    def fetch_ranged_archive(self, family, start, end):
        self.ranged_calls.append((family, start.isoformat(), end.isoformat()))
        script = self.ranged_script.get(family, [])
        if callable(script):
            return script(family, start, end)
        return self._next(script, f"{start}..{end}")

    def fetch_perday_archive(self, family, day):
        self.perday_calls.append((family, day.isoformat()))
        script = self.perday_script.get(family, [])
        if callable(script):
            return script(family, day)
        return self._next(script, day.isoformat())

    def fetch_point(self, name):
        self.point_calls.append(name)
        script = self.point_script.get(name, [])
        if callable(script):
            return script(name)
        if isinstance(script, list):
            return self._next(script, name)
        return script

    def search_activities_page(self, start, limit):
        self.page_calls.append((start, limit))
        if not self.page_script:
            return []
        item = self.page_script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item((start, limit))
        return item

    def get_activity(self, activity_id):
        self.detail_calls.append(str(activity_id))
        return self._next(self.detail_script, str(activity_id))

    def get_activity_workouts(self, activity_id):
        self.workout_calls.append(str(activity_id))
        return self._next(self.workout_script, str(activity_id))

    def download_activity(self, activity_id):
        self.zip_calls.append(str(activity_id))
        return self._next(self.zip_script, str(activity_id))

    # -- tool dispatch (live read-path fallbacks)
    def search_activities(self, limit, since):
        self.page_calls.append(("search_activities", limit, since))
        if self.page_script:
            item = self.page_script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return []

    def fetch_ranged(self, family, start, end, chunks=None):
        self.ranged_calls.append((family, start.isoformat(), end.isoformat(), chunks))
        return self.fetch_ranged_archive(family, start, end)

    def fetch_perday(self, family, day):
        return self.fetch_perday_archive(family, day)


class FakeToken:
    def __init__(self, expires_at: float) -> None:
        self.access_token = "t"
        self.expires_at = expires_at


def iso_utc(dt_value: dt.datetime) -> str:
    return dt_value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
