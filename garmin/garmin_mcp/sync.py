"""The archive sync engine (garmin-local-archive design §4).

Runs as a background task INSIDE the deployed garmin server process (a
FastMCP lifespan task; no second container, no second credential, §4.1). It
reuses the module-level client and token state the tools already use — it
never mints tokens on its own schedule and never touches the token cache
file directly; a restart re-derives the token from the key in one call, as
today. One key, one token flow.

Cadences (§4.2):
- one-time backfill (paced, resumable, starts on deploy per M4);
- nightly sweep self-scheduled at 04:17 local (asyncio timer in the
  cadence task, not a cron), gap backfill after the sweep;
- hourly tier implemented but DISABLED until the phase-5 verdict
  (adoption decisions M1/OD-3 — env-gated, default off);
- weekly re-verify of the newest 30 activities (OD-8);
- freshness edge handled on the read path (readpath.py, §3.5).

Budget and pacing (§4.3): per-run hard cap GARMIN_SYNC_MAX_CALLS (default
1500) aborting cleanly at a chunk boundary, pacing floor
GARMIN_SYNC_PACING_S (default 2 s) between every two upstream calls, no
parallelism (families run sequentially, one engine run at a time).

Rate/error policy (§4.4): retry via the existing call_with_backoff
semantics; a 429/5xx that survives backoff — or a 403 — opens a per-family
circuit (30 min, doubling to a 6 h cap) visible in garmin_api_status; 401 is
not retried (family error + the attended key-rotation runbook).

Crash resume (§4.5): every write is atomic (store handles temp+rename;
SQLite transactional); sync-state.json is rewritten temp+rename+fsync after
every completed unit (chunk, page, activity). BACKFILL and GAP units are
idempotent (skip when stored); SWEEP / HOURLY / freshness units are
idempotent-overwrite by design; ZIPs are stored only when the sha256 differs
from the stored one.

Failure isolation: the sync task is guarded — any unhandled exception kills
only the affected unit/run; the engine logs it, surfaces it in
garmin_api_status (last_error + circuits), and the tools keep working live
(§7.5 acceptance: a crashing sync task must leave all seven tools serving).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Callable

from garmin_mcp import cache, families
from garmin_mcp.store import Store, StoreError, has_data, parse_ts, utc_now_iso

log = logging.getLogger("mcp.garmin.sync")

STATE_SCHEMA_VERSION = 1
SYNC_STATE_RELPATH = ("state", "sync-state.json")

CIRCUIT_BASE_SECONDS = 1800        # 30 min
CIRCUIT_MAX_SECONDS = 21600        # 6 h cap
NIGHTLY_HOUR = 4                   # 04:17 local, after the matrix 03:17 cron
NIGHTLY_MINUTE = 17
HOURLY_MINUTE = 43                 # hourly tier, gated off (OD-3)
SWEEP_TRAILING_DAYS = 32           # covers every cap (28-31) with slack
WEEKLY_VERIFY_SIZE = 30            # OD-8: newest 30 activities
WEEKLY_VERIFY_SECONDS = 7 * 86400
PAGE_SIZE = 100
SWEEP_PAGE_CAP = 5
GAP_PAGE_CAP = 20                  # 2000 activities safety cap
PROBE_BUDGET_CALLS = 800           # OD-7 sparse probe rides the run budget; safety cap, not a target
PROBE_FLOOR_YEARS_BACK = 10
PROBE_MAX_ATTEMPTS = 3
ABSENT_RECHECK_DAYS = 7

ENV_MAX_CALLS = "GARMIN_SYNC_MAX_CALLS"
ENV_PACING_S = "GARMIN_SYNC_PACING_S"
ENV_HOURLY = "GARMIN_SYNC_HOURLY"


class CircuitOpen(RuntimeError):
    """The family's circuit is open; upstream calls are refused."""


class BudgetExhausted(RuntimeError):
    """The run's hard call budget is spent; abort cleanly, resume next fire."""


class SkippedRun(RuntimeError):
    """Another engine run is active; this fire is skipped (no parallelism)."""


class UpstreamFailure(RuntimeError):
    """A non-retryable upstream failure surfaced by the engine."""

    def __init__(self, family: str, exc: BaseException) -> None:
        super().__init__(f"{family}: {exc}")
        self.family = family
        self.status_code = getattr(exc, "status_code", None)


def local_today() -> dt.date:
    """The local calendar day (container TZ; deployment may set TZ)."""
    return dt.datetime.now().astimezone().date()


def local_day_end(day: dt.date) -> dt.datetime:
    """Local midnight ending ``day`` (the staleness rule's boundary)."""
    return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0)).astimezone()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class SyncEngine:
    """Paced, budgeted, circuit-aware sync over the store."""

    def __init__(self, store: Store, *, client_module: Any = None,
                 max_calls: int | None = None, pacing_s: float | None = None,
                 hourly_enabled: bool | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 stop: threading.Event | None = None) -> None:
        if client_module is None:
            from garmin_mcp import client as client_module  # monkeypatch target
        self.store = store
        self.client = client_module
        self.max_calls = max_calls if max_calls is not None else int(
            os.environ.get(ENV_MAX_CALLS, "1500") or 1500)
        self.pacing_s = pacing_s if pacing_s is not None else float(
            os.environ.get(ENV_PACING_S, "2") or 2.0)
        self.hourly_enabled = (hourly_enabled if hourly_enabled is not None
                               else _truthy(os.environ.get(ENV_HOURLY)))
        self.clock = clock
        self.sleep = sleep
        self.stop = stop if stop is not None else threading.Event()
        self.run_lock = threading.Lock()  # one engine run at a time, no parallelism
        self._calls_used = 0
        self._last_call_start = 0.0
        self._state = self._load_state()

    # ------------------------------------------------------------------ state

    def state_path(self):
        return self.store.root.joinpath(*SYNC_STATE_RELPATH)

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "anchor": None,
            "backfill": {
                "state": "pending",
                "stages_complete": [],
                "probe_done": False,
                "probe_attempts": 0,
                "activity_offset": 0,
                "activities_page_complete": False,
                "perday_cursor": {},
                "point_done": False,
                "deepest_page_scanned": 0,
            },
            "watermarks": {},
            "absent_families": {},
            "circuits": {},
            "runs": {"last_nightly": None, "last_run": None},
            "weekly_verify_last": None,
            "totals": {"calls": 0},
            "last_error": None,
        }

    def _load_state(self) -> dict[str, Any]:
        defaults = self._default_state()
        try:
            raw = self.store.read_json(self.state_path())
        except StoreError:
            return defaults
        if not isinstance(raw, dict):
            return defaults
        merged = {**defaults, **raw}
        for section in ("backfill", "runs", "totals"):
            merged[section] = {**defaults[section], **(raw.get(section) or {})}
        merged.setdefault("watermarks", {})
        merged.setdefault("absent_families", {})
        merged.setdefault("circuits", {})
        return merged

    def save_state(self) -> None:
        """temp + rename + fsync after every completed unit (§4.5)."""
        path = self.state_path()
        payload = json.dumps(self._state, ensure_ascii=False, indent=1,
                             sort_keys=True).encode("utf-8")
        self.store.write_bytes_atomic(path, payload)

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    # --------------------------------------------------------------- circuits

    def circuit_open(self, family: str, now: float | None = None) -> bool:
        entry = self._state["circuits"].get(family)
        if not entry:
            return False
        until = parse_ts(entry.get("open_until"))
        now = self.clock() if now is None else now
        return bool(until and until.timestamp() > now)

    def _open_circuit(self, family: str) -> None:
        entry = self._state["circuits"].setdefault(family, {"failures": 0})
        entry["failures"] = int(entry.get("failures", 0)) + 1
        duration = min(CIRCUIT_BASE_SECONDS * (2 ** (entry["failures"] - 1)),
                       CIRCUIT_MAX_SECONDS)
        opened = dt.datetime.fromtimestamp(self.clock(), tz=dt.timezone.utc)
        entry["opened_at"] = opened.isoformat(timespec="seconds")
        entry["open_until"] = (opened + dt.timedelta(seconds=duration)).isoformat(timespec="seconds")

    def _clear_circuit(self, family: str) -> None:
        self._state["circuits"].pop(family, None)

    def open_circuits(self) -> dict[str, dict[str, Any]]:
        """Currently-open circuits for garmin_api_status (§5.3)."""
        now = self.clock()
        return {fam: dict(entry) for fam, entry in self._state["circuits"].items()
                if self.circuit_open(fam, now)}

    # --------------------------------------------------------- call machinery

    def _pace(self) -> None:
        """Pacing floor between every two upstream calls (§4.3)."""
        if self._last_call_start <= 0:
            return
        elapsed = time.monotonic() - self._last_call_start
        wait = self.pacing_s - elapsed
        if wait > 0:
            self.sleep(wait)

    def _budget_left(self) -> bool:
        return self._calls_used < self.max_calls

    def _upstream(self, family: str, fn: Callable[[], Any], *, cost: int = 1,
                  record: bool = True) -> Any:
        """One paced, budgeted, circuit-gated upstream call (§4.3, §4.4).

        Raises BudgetExhausted when the run budget is spent, CircuitOpen when
        the family's circuit is open, and the original error for anything the
        backoff could not survive (401/403/non-status). ``record=False``
        defers the per-family error-map entry to the caller (the chunk layer
        classifies pre-era 400/404 as empty markers, not family errors).
        """
        if not self._budget_left():
            raise BudgetExhausted(
                f"run budget spent ({self._calls_used}/{self.max_calls} calls)")
        if self.circuit_open(family):
            raise CircuitOpen(f"family {family!r} circuit is open")
        self._pace()
        self._last_call_start = time.monotonic()
        try:
            result = cache.call_with_backoff(fn)
        except Exception as exc:  # noqa: BLE001 — classified below
            self._record_call_failure(family, exc, record=record)
            raise
        self._record_call_success(family)
        self._calls_used += cost
        self._state["totals"]["calls"] = int(self._state["totals"].get("calls", 0)) + cost
        return result

    def _record_call_failure(self, family: str, exc: BaseException,
                             record: bool = True) -> None:
        status = getattr(exc, "status_code", None)
        if record:
            self.client.record_family_error(family, exc)
        if status == 401:
            # 401: token/key problem; never retried, no circuit (§4.4 runbook).
            self._note_error(f"{family}: 401 unauthorized ({exc})")
            return
        if status == 403:
            # 403: non-retryable policy/scope signal; record + open the circuit.
            self._note_error(f"{family}: 403 forbidden ({exc})")
            self._open_circuit(family)
            return
        if isinstance(status, int) and (status == 429 or 500 <= status < 600):
            # Survived backoff: open/double the per-family circuit (§4.4).
            self._note_error(f"{family}: {status} after backoff ({exc})")
            self._open_circuit(family)
            return
        self._note_error(f"{family}: upstream failure ({exc})")

    def _record_call_success(self, family: str) -> None:
        self.client.record_family_ok(family)
        self._clear_circuit(family)

    def _note_error(self, detail: str) -> None:
        self._state["last_error"] = {"detail": detail, "ts": utc_now_iso()}

    # ------------------------------------------------------------ sync writes

    def _sync_state_note(self, kind: str, detail: str) -> None:
        self.store.log(kind, 0, detail)

    def _fetch_ranged_unit(self, family: str, chunk_start: dt.date,
                           chunk_end: dt.date, *, overwrite: bool = False) -> None:
        """Fetch + store one canonical chunk (the ONLY ranged write shape, §3.2).

        An unclassified 4xx (400/404) on a historical chunk is recorded as the
        family's empty-chunk marker (empty array + coverage), not a family
        error — the pre-era semantics of §3.2. On the trailing chunk (end >=
        today) a 400/404 cannot be pre-era and keeps the normal error path.
        """
        today = local_today()
        try:
            data = self._upstream(family, lambda: self.client.fetch_ranged_archive(
                family, chunk_start, chunk_end), record=False)
        except Exception as exc:  # noqa: BLE001 — pre-era marker check below
            status = getattr(exc, "status_code", None)
            if (isinstance(status, int) and status in (400, 404)
                    and chunk_end < today and not isinstance(exc, CircuitOpen)):
                self.client.record_family_ok(family)  # pre-era: not a family error
                self.store.write_ranged_chunk(family, chunk_start, chunk_end, [])
                self._sync_state_note(
                    "pre-era-marker",
                    f"{family} {chunk_start}..{chunk_end}: {status} recorded as empty chunk")
                self._after_unit(family, chunk_start, chunk_end)
                return
            self.client.record_family_error(family, exc)  # real failure: record it
            raise
        self.store.write_ranged_chunk(family, chunk_start, chunk_end, data)
        self._after_unit(family, chunk_start, chunk_end)

    def _fetch_perday_unit(self, family: str, day: dt.date, *, advance_cursor: bool) -> None:
        """Fetch + atomically replace one per-day file (§3.5)."""
        try:
            data = self._upstream(family, lambda: self.client.fetch_perday_archive(family, day))
        except Exception as exc:  # noqa: BLE001 — re-record (error map), then raise
            self.client.record_family_error(family, exc)
            raise
        self.store.write_perday(family, day, data)
        if advance_cursor:
            self._advance_perday_cursor(family, day)

    def _fetch_point_unit(self, name: str) -> None:
        data = self._upstream(f"point:{name}", lambda: self.client.fetch_point(name))
        self.store.write_json_atomic(self.store.point_path(name), data)

    def _fetch_activity_payloads(self, activity_id: int, *, zip_gate: bool) -> None:
        """detail + workouts + ZIP for one activity (backfill/sweep/verify).

        ZIPs are stored only when the sha256 differs from the stored one (§4.5).
        """
        now = utc_now_iso()
        if not self._activity_marker_set(activity_id, "detail"):
            data = self._upstream("activities",
                                  lambda: self.client.get_activity(str(activity_id)))
            self.store.write_json_atomic(
                self.store.activity_dir(activity_id) / "detail.json", data)
            self.store.set_activity_marker(activity_id, "detail_fetched_at", now)
        if not self._activity_marker_set(activity_id, "workouts"):
            data = self._upstream("activities",
                                  lambda: self.client.get_activity_workouts(str(activity_id)))
            self.store.write_json_atomic(
                self.store.activity_dir(activity_id) / "workouts.json",
                data if data is not None else [])
            self.store.set_activity_marker(activity_id, "workouts_fetched_at", now)
        if not self._activity_marker_set(activity_id, "zip") or zip_gate:
            blob = self._upstream("activities",
                                  lambda: self.client.download_activity(str(activity_id)))
            if isinstance(blob, str):
                blob = blob.encode()
            digest = hashlib.sha256(blob).hexdigest()
            row = self.store.activity_row(activity_id) or {}
            if not zip_gate or row.get("zip_sha256") != digest:
                self.store.write_bytes_atomic(
                    self.store.activity_dir(activity_id) / "file.zip", blob)
            self.store.set_activity_marker(activity_id, "zip_fetched_at", now,
                                           zip_sha256=digest, zip_size=len(blob))
        self.save_state()

    @staticmethod
    def _marker_column(marker: str) -> str:
        return {"detail": "detail_fetched_at",
                "workouts": "workouts_fetched_at",
                "zip": "zip_fetched_at"}[marker]

    def _activity_marker_set(self, activity_id: int, marker: str) -> bool:
        row = self.store.activity_row(activity_id) or {}
        return row.get(self._marker_column(marker)) is not None

    def _after_unit(self, family: str, chunk_start: dt.date, chunk_end: dt.date) -> None:
        """Watermark + sync-state persist after a completed unit (§4.5)."""
        self._state["watermarks"][family] = {
            "chunk_start": chunk_start.isoformat(),
            "chunk_end": chunk_end.isoformat(),
            "fetched_at": utc_now_iso(),
        }
        self.save_state()

    def _advance_perday_cursor(self, family: str, day: dt.date) -> None:
        self._state["backfill"]["perday_cursor"][family] = day.isoformat()
        self.save_state()

    def _absent_skip(self, family: str, now: float | None = None) -> bool:
        """True when the family is absent-marked and not yet due for recheck."""
        entry = self._state["absent_families"].get(family)
        if not entry:
            return False
        now = self.clock() if now is None else now
        recheck = parse_ts(entry.get("next_recheck"))
        return bool(recheck and recheck.timestamp() > now)

    def _mark_absent(self, family: str, reason: str) -> None:
        recheck = (dt.datetime.fromtimestamp(self.clock(), tz=dt.timezone.utc)
                   + dt.timedelta(days=ABSENT_RECHECK_DAYS))
        self._state["absent_families"][family] = {
            "reason": reason, "next_recheck": recheck.isoformat(timespec="seconds"),
        }
        self.save_state()

    def _maybe_absent(self, family: str, exc: BaseException) -> None:
        """Repeated non-retryable failures mark a family absent (§4.5)."""
        status = getattr(exc, "status_code", None)
        if status in (400, 404, 403):
            self._mark_absent(family, f"{status} ({exc})")
            self._sync_state_note("absent-family", f"{family}: {exc}")

    # ------------------------------------------------------------ stage marks

    def _complete_stage(self, stage: int) -> None:
        stages = self._state["backfill"].setdefault("stages_complete", [])
        if stage not in stages:
            stages.append(stage)
            self._sync_state_note("backfill", f"stage {stage} complete")
        self.save_state()

    def _set_backfill_state(self) -> None:
        stages = self._state["backfill"].get("stages_complete", [])
        self._state["backfill"]["state"] = "complete" if len(stages) >= 5 else "running"
        self.save_state()

    def backfill_incomplete(self) -> bool:
        return self._state["backfill"].get("state") != "complete"

    def backfill_progress(self) -> dict[str, str]:
        stages = len(self._state["backfill"].get("stages_complete", []))
        state = self._state["backfill"].get("state", "pending")
        return {"state": state, "progress": f"{stages} of 5 stages"}

    # ------------------------------------------------------- backfill (§4.2)

    def run_deploy_backfill(self) -> None:
        """M4: backfill starts on deploy — first run after startup."""
        self.run_backfill()

    def run_backfill(self) -> None:
        """One backfill pass under the shared run budget (resumable, §4.2/§4.5)."""
        with self._guarded_run("backfill"):
            self._backfill_body()

    def _backfill_body(self) -> None:
        try:
            self._stage_probe()
            self._stage_activities()
            self._stage_activity_payloads()
            self._stage_ranged()
            self._stage_perday()
            self._stage_points()
        except BudgetExhausted:
            self._sync_state_note("backfill", "run budget spent; resuming next fire")
        except CircuitOpen:
            self._sync_state_note("backfill", "circuit open; resuming next fire")
        except StoreError as exc:
            self._note_error(f"backfill store I/O error: {exc}")
            log.warning("backfill degraded by store I/O error: %s", exc)
        self._set_backfill_state()

    def _stage_probe(self) -> None:
        """OD-7 sparse probe: sets the GLOBAL anchor A once (§3.2)."""
        backfill = self._state["backfill"]
        if self._state.get("anchor"):
            backfill["probe_done"] = True
            return
        if backfill.get("probe_done"):
            return
        attempts = int(backfill.get("probe_attempts", 0))
        if attempts >= PROBE_MAX_ATTEMPTS:
            # The probe never settled: fall back to the floor year — biased
            # complete (empty chunks are cheap; missed history is not, §3.2).
            anchor = dt.date(local_today().year - PROBE_FLOOR_YEARS_BACK, 1, 1)
            self._set_anchor(anchor)
            backfill["probe_done"] = True
            self._sync_state_note("probe", "probe unsettled after max attempts; "
                                           f"anchor set to floor {anchor}")
            return
        backfill["probe_attempts"] = attempts + 1
        self.save_state()
        anchor = self._sparse_probe()
        if anchor is None:
            self._sync_state_note("probe", "sparse probe incomplete; will re-probe next fire")
            return
        self._set_anchor(anchor)
        backfill["probe_done"] = True
        self._sync_state_note("probe", f"anchor set to {anchor}")

    def _set_anchor(self, anchor: dt.date) -> None:
        self._state["anchor"] = anchor.isoformat()
        self.store.set_meta("anchor", anchor.isoformat())
        self.save_state()

    def anchor(self) -> dt.date | None:
        raw = self._state.get("anchor")
        if not raw:
            raw = self.store.get_meta("anchor")
        if not raw:
            return None
        try:
            return dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None

    def _stage_activities(self) -> None:
        """Stage 1: page search_activities by start offset into the index."""
        backfill = self._state["backfill"]
        if backfill.get("activities_page_complete"):
            self._complete_stage(1)
            return
        offset = int(backfill.get("activity_offset", 0))
        try:
            while True:
                self._check_stop()
                records = self._upstream(
                    "activities",
                    lambda o=offset: self.client.search_activities_page(o, PAGE_SIZE))
                for record in records or []:
                    aid = record.get("activityId") if isinstance(record, dict) else None
                    if not isinstance(aid, int):
                        continue
                    if self.store.has_activity(aid):
                        continue  # idempotent: skip if stored (§4.5)
                    self.store.write_json_atomic(
                        self.store.activity_dir(aid) / "search-record.json", record)
                    self.store.upsert_activity_search(record, utc_now_iso())
                if not records or len(records) < PAGE_SIZE:
                    backfill["activities_page_complete"] = True
                    self._complete_stage(1)
                    return
                offset += len(records)
                backfill["activity_offset"] = offset
                self.save_state()
        except CircuitOpen:
            self.save_state()

    def _stage_activity_payloads(self) -> None:
        """Stage 2: detail/workouts/ZIP per activity, newest first, resumable."""
        while True:
            self._check_stop()
            pending = self.store.activities_pending_payloads(limit=50)
            if not pending:
                self._complete_stage(2)
                return
            for activity_id in pending:
                self._check_stop()
                try:
                    self._fetch_activity_payloads(activity_id, zip_gate=False)
                except CircuitOpen:
                    return

    def _stage_ranged(self) -> None:
        """Stage 3: ranged families from anchor A to now in canonical chunks."""
        anchor = self.anchor()
        if anchor is None:
            return  # the probe must settle first; next fire re-enters here
        today = local_today()
        all_done = True
        for family_spec in families.RANGED_FAMILIES:
            family = family_spec.name
            self._check_stop()
            if self._absent_skip(family):
                continue  # absent families recheck on their own schedule
            try:
                for chunk_start, chunk_end in families.canonical_chunks(
                        anchor, family_spec.cap_days, anchor, today):
                    self._check_stop()
                    if self.store.coverage_row(family, chunk_start, chunk_end):
                        continue
                    self._fetch_ranged_unit(family, chunk_start, chunk_end)
            except CircuitOpen:
                all_done = False
                continue
            except UpstreamFailure as exc:
                self._maybe_absent(family, exc)
                all_done = False
                continue
            # the family is done when its whole grid is covered (this pass or
            # an earlier one) — not merely "nothing was fetched"
            for chunk_start, chunk_end in families.canonical_chunks(
                    anchor, family_spec.cap_days, anchor, today):
                if not self.store.coverage_row(family, chunk_start, chunk_end):
                    all_done = False
                    break
        if all_done:
            self._complete_stage(3)

    def _stage_perday(self) -> None:
        """Stage 4: per-day families over the data-bearing era (OD-7 anchor)."""
        anchor = self.anchor()
        if anchor is None:
            return
        today = local_today()
        all_done = True
        for family in families.PERDAY_FAMILIES_ARCHIVE:
            self._check_stop()
            if self._absent_skip(family):
                continue
            cursor = self._state["backfill"]["perday_cursor"].get(family)
            day = (dt.date.fromisoformat(cursor) + dt.timedelta(days=1)
                   if cursor else anchor)
            while day <= today:
                self._check_stop()
                if self.store.has_daily_row(family, day):
                    self._advance_perday_cursor(family, day)
                    day += dt.timedelta(days=1)
                    continue
                try:
                    self._fetch_perday_unit(family, day, advance_cursor=True)
                except CircuitOpen:
                    all_done = False
                    break
                except UpstreamFailure as exc:
                    self._maybe_absent(family, exc)
                    all_done = False
                    break
                day += dt.timedelta(days=1)
            if day <= today:
                all_done = False
        if all_done:
            self._complete_stage(4)

    def _stage_points(self) -> None:
        """Stage 5: the three point reads."""
        if self._state["backfill"].get("point_done"):
            self._complete_stage(5)
            return
        for name in families.POINT_NAMES:
            self._check_stop()
            if self.store.point_path(name).exists():
                continue
            try:
                self._fetch_point_unit(name)
            except CircuitOpen:
                continue
            except UpstreamFailure as exc:
                self._maybe_absent(f"point:{name}", exc)
                continue
        self._state["backfill"]["point_done"] = True
        self._complete_stage(5)

    # ---------------------------------------------------------- nightly fire

    def run_nightly(self) -> None:
        """The 04:17 fire (§4.2 runner composition): on backfill nights the
        backfill runs first under the shared budget; the sweep runs on budget
        remaining, or is skipped when the backfill consumed the budget; after
        the backfill completes, the fire is the sweep plus gap backfill."""
        with self._guarded_run("nightly"):
            self._state["runs"]["last_nightly"] = {
                "started_at": utc_now_iso(), "date": local_today().isoformat(),
            }
            self.save_state()
            try:
                if self.backfill_incomplete():
                    self._backfill_body()
                if self._budget_left():
                    self._sweep_body()
                if not self.backfill_incomplete():
                    if self._weekly_verify_due():
                        self._weekly_body()
                    self._gap_body()
            finally:
                self._state["runs"]["last_run"] = {
                    "kind": "nightly", "ended_at": utc_now_iso(),
                    "calls": self._calls_used,
                }
                self.save_state()

    # ------------------------------------------------------------- sweep (§4.2)

    def run_sweep(self) -> None:
        with self._guarded_run("sweep"):
            self._sweep_body()

    def _sweep_body(self) -> None:
        """Nightly sweep: trailing 32-day window for all ranged families in
        canonical chunks, per-day families over the same window, arrival-safe
        activity paging, point-read tail (§4.2)."""
        try:
            self._sweep_ranged()
            self._sweep_perday()
            new_ids = self._arrival_safe_paging(page_cap=SWEEP_PAGE_CAP)
            for activity_id in new_ids:
                self._check_stop()
                try:
                    self._fetch_activity_payloads(activity_id, zip_gate=True)
                except CircuitOpen:
                    continue
            self._sweep_points()
        except BudgetExhausted:
            self._sync_state_note("sweep", "run budget spent")
        except StoreError as exc:
            self._note_error(f"sweep store I/O error: {exc}")
            log.warning("sweep degraded by store I/O error: %s", exc)
        finally:
            self._state["runs"]["last_run"] = {
                "kind": "sweep", "ended_at": utc_now_iso(), "calls": self._calls_used,
            }
            self.save_state()

    def _sweep_ranged(self) -> None:
        anchor = self.anchor()
        if anchor is None:
            return
        today = local_today()
        window_start = today - dt.timedelta(days=SWEEP_TRAILING_DAYS - 1)
        for family_spec in families.RANGED_FAMILIES:
            family = family_spec.name
            self._check_stop()
            if self._absent_skip(family):
                continue
            for chunk_start, chunk_end in families.canonical_chunks(
                    anchor, family_spec.cap_days, window_start, today):
                self._check_stop()
                try:
                    self._fetch_ranged_unit(family, chunk_start, chunk_end,
                                            overwrite=True)
                except CircuitOpen:
                    break
                except UpstreamFailure as exc:
                    self._maybe_absent(family, exc)
                    break

    def _sweep_perday(self) -> None:
        today = local_today()
        start = today - dt.timedelta(days=SWEEP_TRAILING_DAYS - 1)
        for family in families.PERDAY_FAMILIES_ARCHIVE:
            self._check_stop()
            if self._absent_skip(family):
                continue
            day = start
            while day <= today:
                self._check_stop()
                try:
                    self._fetch_perday_unit(family, day, advance_cursor=False)
                except CircuitOpen:
                    break
                except UpstreamFailure as exc:
                    self._maybe_absent(family, exc)
                    break
                day += dt.timedelta(days=1)

    def _sweep_points(self) -> None:
        for name in families.POINT_NAMES:
            self._check_stop()
            try:
                self._fetch_point_unit(name)
            except CircuitOpen:
                continue
            except UpstreamFailure as exc:
                self._maybe_absent(f"point:{name}", exc)
                continue

    def _arrival_safe_paging(self, *, page_cap: int) -> list[int]:
        """Newest-first search paging with activity_id dedupe (§4.2).

        Stops at the first fully-known page or the page safety cap; the
        page-depth cursor resets to 0 whenever a page yields any new id, and
        sync-state records the deepest page scanned.
        """
        new_ids: list[int] = []
        pages_scanned = 0
        depth = 0
        offset = 0
        try:
            while pages_scanned < page_cap:
                self._check_stop()
                records = self._upstream(
                    "activities",
                    lambda o=offset: self.client.search_activities_page(o, PAGE_SIZE))
                if not records:
                    break
                fresh = 0
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    aid = record.get("activityId")
                    if not isinstance(aid, int):
                        continue
                    if self.store.has_activity(aid):
                        continue
                    self.store.write_json_atomic(
                        self.store.activity_dir(aid) / "search-record.json", record)
                    self.store.upsert_activity_search(record, utc_now_iso())
                    new_ids.append(aid)
                    fresh += 1
                if fresh:
                    depth = 0  # page-depth cursor resets on any new id (§4.2)
                else:
                    depth += 1
                pages_scanned += 1
                offset += len(records)
                if len(records) < PAGE_SIZE:
                    break
        except CircuitOpen:
            pass
        except BudgetExhausted:
            raise
        self._state["backfill"]["deepest_page_scanned"] = max(
            int(self._state["backfill"].get("deepest_page_scanned", 0)),
            pages_scanned if depth else depth)
        self.save_state()
        return new_ids

    # --------------------------------------------------- weekly verify (OD-8)

    def _weekly_verify_due(self) -> bool:
        last = self._state.get("weekly_verify_last")
        if not last:
            return True
        parsed = parse_ts(str(last)) or _parse_date_only(str(last))
        if parsed is None:
            return True
        return (self.clock() - parsed.timestamp()) >= WEEKLY_VERIFY_SECONDS

    def run_weekly_verify(self) -> None:
        with self._guarded_run("weekly_verify"):
            self._weekly_body()

    def _weekly_body(self) -> None:
        """OD-8: re-fetch detail/workouts/ZIP for the newest 30 activities;
        the sha256 gate catches an edit (§4.5)."""
        try:
            for activity_id in self.store.newest_activity_ids(WEEKLY_VERIFY_SIZE):
                self._check_stop()
                try:
                    self._fetch_activity_payloads(activity_id, zip_gate=True)
                except CircuitOpen:
                    continue
                except UpstreamFailure as exc:
                    self._note_error(f"weekly verify: {exc}")
                    continue
        except BudgetExhausted:
            self._sync_state_note("weekly_verify", "run budget spent")
        finally:
            self._state["weekly_verify_last"] = utc_now_iso()
            self.save_state()

    # -------------------------------------------------------- gap backfill

    def run_gap_backfill(self) -> None:
        with self._guarded_run("gap_backfill"):
            self._gap_body()

    def _gap_body(self) -> None:
        """Nightly, after the sweep: scan daily_records and coverage for
        missing dates/windows, refill oldest-first within the run budget, then
        re-run the activity paging to the 20-page safety cap (§4.2)."""
        anchor = self.anchor()
        today = local_today()
        if anchor is not None:
            for family_spec in families.RANGED_FAMILIES:
                family = family_spec.name
                self._check_stop()
                if self._absent_skip(family):
                    continue
                try:
                    for chunk_start, chunk_end in families.canonical_chunks(
                            anchor, family_spec.cap_days, anchor, today):
                        self._check_stop()
                        if self.store.coverage_row(family, chunk_start, chunk_end):
                            continue
                        self._fetch_ranged_unit(family, chunk_start, chunk_end)
                except CircuitOpen:
                    continue
                except UpstreamFailure as exc:
                    self._maybe_absent(family, exc)
                    continue
            for family in families.PERDAY_FAMILIES_ARCHIVE:
                self._check_stop()
                if self._absent_skip(family):
                    continue
                day = anchor
                while day <= today:
                    self._check_stop()
                    if self.store.has_daily_row(family, day):
                        day += dt.timedelta(days=1)
                        continue
                    try:
                        self._fetch_perday_unit(family, day, advance_cursor=True)
                    except CircuitOpen:
                        break
                    except UpstreamFailure as exc:
                        self._maybe_absent(family, exc)
                        break
                    day += dt.timedelta(days=1)
        try:
            new_ids = self._arrival_safe_paging(page_cap=GAP_PAGE_CAP)
        except BudgetExhausted:
            new_ids = []
        for activity_id in new_ids:
            self._check_stop()
            try:
                self._fetch_activity_payloads(activity_id, zip_gate=True)
            except CircuitOpen:
                continue

    # ---------------------------------------------------- hourly tier (OD-3)

    def run_hourly(self) -> None:
        with self._guarded_run("hourly"):
            self._hourly_body()

    def _hourly_body(self) -> None:
        """Hourly tier (§4.2): ranged families over yesterday..today, per-day
        families over the same 2 days, one page of arrival-safe activity
        paging. Implemented but DISABLED until the phase-5 verdict (OD-3/M1):
        the cadence loop only fires it when GARMIN_SYNC_HOURLY is truthy."""
        anchor = self.anchor()
        today = local_today()
        if anchor is not None:
            yesterday = today - dt.timedelta(days=1)
            for family_spec in families.RANGED_FAMILIES:
                family = family_spec.name
                self._check_stop()
                if self._absent_skip(family):
                    continue
                for chunk_start, chunk_end in families.canonical_chunks(
                        anchor, family_spec.cap_days, yesterday, today):
                    self._check_stop()
                    try:
                        self._fetch_ranged_unit(family, chunk_start, chunk_end,
                                                overwrite=True)
                    except CircuitOpen:
                        break
                    except UpstreamFailure as exc:
                        self._maybe_absent(family, exc)
                        break
            for family in families.PERDAY_FAMILIES_ARCHIVE:
                self._check_stop()
                if self._absent_skip(family):
                    continue
                for day in (yesterday, today):
                    self._check_stop()
                    try:
                        self._fetch_perday_unit(family, day, advance_cursor=False)
                    except CircuitOpen:
                        break
                    except UpstreamFailure as exc:
                        self._maybe_absent(family, exc)
                        break
        try:
            new_ids = self._arrival_safe_paging(page_cap=1)
        except BudgetExhausted:
            new_ids = []
        for activity_id in new_ids:
            self._check_stop()
            try:
                self._fetch_activity_payloads(activity_id, zip_gate=True)
            except CircuitOpen:
                continue
        self._state["runs"]["last_run"] = {
            "kind": "hourly", "ended_at": utc_now_iso(), "calls": self._calls_used,
        }
        self.save_state()

    # ----------------------------------------------------- sparse probe (OD-7)

    def _sparse_probe(self) -> dt.date | None:
        """Find the data-bearing era across ALL ranged families (§3.2, OD-7).

        Probes are 28-day ranged windows (every cap >= 28). Within a known
        data-bearing year the 12 month-start windows cover every day of the
        year, so the per-family era is blind-spot free; the quarterly samples
        are only a cheap pre-filter. Errors on old windows follow the §3.2
        pre-era semantics (400/404 = no data). Returns the earliest era start
        found, or None when the probe could not settle (budget spent / every
        family errored) — the caller retries next fire.
        """
        started = self._calls_used
        best: dt.date | None = None
        for family_spec in families.RANGED_FAMILIES:
            if self._calls_used - started >= PROBE_BUDGET_CALLS:
                return None  # unsettled: re-probe next fire (cheap, idempotent)
            if self._absent_skip(family_spec.name):
                continue
            try:
                era = self._probe_family_era(family_spec.name)
            except BudgetExhausted:
                return None
            except CircuitOpen:
                continue
            except UpstreamFailure:
                continue
            if era is not None and (best is None or era < best):
                best = era
        return best

    def _probe_family_era(self, family: str) -> dt.date | None:
        today = local_today()
        floor_year = today.year - PROBE_FLOOR_YEARS_BACK
        era: dt.date | None = None
        year = today.year
        while year >= floor_year:
            self._check_stop()
            if year == today.year:
                # The current year is scanned monthly directly: its Nov/Dec
                # tail is invisible to the quarterly samples (Jan/Apr/Jul/Oct).
                year_has_data = True
            else:
                year_has_data = self._year_quarterly_has_data(family, year)
            if year_has_data:
                window = self._first_data_month_window(family, year)
                if window is not None:
                    candidate = self._halve_window(family, window[0], window[1])
                    if era is None or candidate < era:
                        era = candidate
                year -= 1
                continue
            if era is not None:
                # Boundary year below found data: close the quarterly filter's
                # blind spots with one monthly scan before concluding empty.
                window = self._first_data_month_window(family, year)
                if window is not None:
                    candidate = self._halve_window(family, window[0], window[1])
                    if era is None or candidate < era:
                        era = candidate
                    year -= 1
                    continue
                break  # first truly empty year below found data bounds the era
            year -= 1  # nothing found yet; keep walking down
        return era

    def _first_data_month_window(self, family: str, year: int):
        """Earliest data-bearing month window of a year, Jan→Dec (each day of
        the year lies inside exactly one month window)."""
        for month in range(1, 13):
            self._check_stop()
            month_start = dt.date(year, month, 1)
            if self._probe_window(family, month_start):
                return (month_start,
                        month_start + dt.timedelta(days=families.PROBE_WINDOW_DAYS - 1))
        return None

    def _year_quarterly_has_data(self, family: str, year: int) -> bool:
        for month in (1, 4, 7, 10):
            try:
                if self._probe_window(family, dt.date(year, month, 1)):
                    return True
            except ValueError:
                continue
        return False

    def _probe_window(self, family: str, start: dt.date,
                      end: dt.date | None = None) -> bool:
        """True when one probe window carries data (pre-era 4xx = no, §3.2)."""
        if end is None:
            end = start + dt.timedelta(days=families.PROBE_WINDOW_DAYS - 1)
        try:
            data = self._upstream(family, lambda: self.client.fetch_ranged_archive(
                family, start, end), record=False)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and status in (400, 404):
                self.client.record_family_ok(family)  # expected pre-era semantics
                return False
            if isinstance(exc, (CircuitOpen, BudgetExhausted)):
                raise
            self.client.record_family_error(family, exc)
            raise UpstreamFailure(family, exc) from exc
        return has_data(data)

    def _halve_window(self, family: str, start: dt.date, end: dt.date) -> dt.date:
        """Narrow a data-bearing window to <= 7 days; return its start.

        Invariant: the family's earliest data lies in [start, end], so the
        returned start is never after it (anchor biased early = safe, §3.2).
        """
        while (end - start).days + 1 > 7:
            self._check_stop()
            mid = start + dt.timedelta(days=(end - start).days // 2)
            try:
                lower_has = self._probe_window(family, start, mid)
            except UpstreamFailure:
                break
            if lower_has:
                end = mid
            else:
                start = mid + dt.timedelta(days=1)
        return start

    # ------------------------------------------------------------- run guard

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise BudgetExhausted("stop requested")

    class _Guard:
        def __init__(self, engine: "SyncEngine", kind: str) -> None:
            self.engine = engine
            self.kind = kind

        def __enter__(self) -> "SyncEngine._Guard":
            if not self.engine.run_lock.acquire(blocking=False):
                raise SkippedRun(f"another engine run is active; {self.kind} skipped")
            self.engine._calls_used = 0
            self.engine._last_call_start = 0.0
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            self.engine.run_lock.release()
            if exc_type is not None and not issubclass(exc_type, (BudgetExhausted, SkippedRun)):
                # Guard: a crashing run kills only this run (§4.1, §7.5).
                self.engine._note_error(f"sync {self.kind} crashed: {exc!r}")
                try:
                    self.engine.save_state()
                except StoreError:
                    pass
                log.exception("sync %s crashed (isolated; tools keep serving)", self.kind)
                return True  # swallow: the task never propagates
            return False

    def _guarded_run(self, kind: str) -> "SyncEngine._Guard":
        return self._Guard(self, kind)

    # ------------------------------------------------------------- run stats

    def run_stats(self) -> dict[str, Any]:
        return {"calls_used": self._calls_used, "max_calls": self.max_calls,
                "pacing_s": self.pacing_s, "hourly_enabled": self.hourly_enabled,
                "runs": dict(self._state.get("runs", {})),
                "backfill": self.backfill_progress(),
                "anchor": self._state.get("anchor"),
                "circuits": self.open_circuits(),
                "absent_families": dict(self._state.get("absent_families", {})),
                "last_error": self._state.get("last_error")}


def _parse_date_only(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.combine(dt.date.fromisoformat(value[:10]), dt.time(0, 0),
                                   tzinfo=dt.timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Cadence task wiring (FastMCP lifespan; §4.1)
# ---------------------------------------------------------------------------

_ENGINE: SyncEngine | None = None
_ENGINE_LOCK = threading.Lock()
_STOP = threading.Event()


def engine() -> SyncEngine | None:
    return _ENGINE


def request_stop() -> None:
    _STOP.set()


def reset_for_tests() -> None:
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None
    _STOP.clear()


def start_cadence_task() -> asyncio.Task | None:
    """Create the cadence asyncio task when the store is configured for sync.

    Sync runs only when the store dir is set and the mode is live_only or
    read_through (§5.1): mode off means the PR alone changes nothing — no
    background calls at all.
    """
    from garmin_mcp import readpath

    store = readpath.store()
    mode = readpath.mode()
    if store is None or mode not in ("live_only", "read_through"):
        return None
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return None
        _STOP.clear()
        _ENGINE = SyncEngine(store)
        return asyncio.ensure_future(_cadence_loop(_ENGINE))


async def _cadence_loop(engine_ref: SyncEngine) -> None:
    """Asyncio timer inside the task: nightly 04:17 local (+ hourly :43 when
    enabled), deploy-time backfill first (M4). Fires run in worker threads so
    the event loop stays responsive; runs are sequential by the engine lock."""
    loop = asyncio.get_running_loop()
    log.info("garmin sync cadence started (hourly tier %s)",
             "ENABLED" if engine_ref.hourly_enabled else "disabled (OD-3)")
    try:
        await loop.run_in_executor(
            None, _guarded_thread(engine_ref.run_deploy_backfill))
    except Exception:  # noqa: BLE001 — startup backfill must never kill the task
        log.exception("deploy backfill crashed (isolated)")
    last_nightly_date: str | None = None
    last_hourly_hour: tuple[dt.date, int] | None = None
    while not _STOP.is_set():
        try:
            await asyncio.sleep(20)
            if _STOP.is_set():
                return
            now = dt.datetime.now().astimezone()
            today = now.date()
            due_nightly = (
                (now.hour, now.minute) >= (NIGHTLY_HOUR, NIGHTLY_MINUTE)
                and last_nightly_date != today.isoformat()
            )
            due_hourly = (
                engine_ref.hourly_enabled
                and now.minute >= HOURLY_MINUTE
                and last_hourly_hour != (today, now.hour)
            )
            if engine_ref.run_lock.locked():
                continue  # a run is active; never parallel
            if due_nightly:
                last_nightly_date = today.isoformat()
                await loop.run_in_executor(
                    None, _guarded_thread(engine_ref.run_nightly))
            elif due_hourly:
                last_hourly_hour = (today, now.hour)
                await loop.run_in_executor(
                    None, _guarded_thread(engine_ref.run_hourly))
        except asyncio.CancelledError:
            _STOP.set()
            return
        except Exception:  # noqa: BLE001 — the cadence task itself must not die
            log.exception("cadence loop iteration failed (continuing)")
            await asyncio.sleep(5)


def _guarded_thread(run: Callable[[], Any]) -> Callable[[], Any]:
    def _runner() -> None:
        try:
            run()
        except Exception:  # noqa: BLE001 — guarded: logged, never propagated
            log.exception("engine run crashed (isolated; tools keep serving)")
    return _runner
