"""The tiered local-store sync (strava-local-store design section 3.4,
artifact A4): one asyncio task inside the 8016 server process.

No timer unit, no second process, no second token writer: all Strava traffic
(tool calls and sync batches) shares one rate guard (rate.STATE) and one
token writer (oauth), and every sync request goes through client.api_get so
the guard pre-check, header ingestion, and the 401 refresh path apply to
sync traffic exactly as to tool traffic.

Tiers (endpoint-dependent cadence, per Maciek's answer):
  frequent (STRAVA_SYNC_FREQUENT_HOURS, default 6 h): activities after-walk
    from the watermark minus a 7-day look-back margin, 100 per page, until a
    page is fully known-unchanged; detail + streams + primary photo per
    new/changed activity; athlete and stats snapshots.
  slow (STRAVA_SYNC_SLOW_HOURS, default 24 h, phase-offset from the
    frequent slot): zones, gear; post-scope only (after the attended
    read_all re-authorization): starred segments and routes.
  sweep (in the slow slot when due, every STRAVA_SYNC_SWEEP_DAYS = 7): full
    list walk with no after filter; catches deletions, backdated imports,
    and edits older than the look-back window; refetches on fingerprint
    mismatch.
  backfill (trigger-only; the scheduler never cold-starts it): walks the
    list to build the index, then fetches detail + streams per activity,
    oldest-missing-first; a persisted cursor survives rate stops, 429s, and
    restarts, and a backfill with a cursor resumes on its own in the
    frequent tier's slots.

Batch rules (A4 interleave): strictly one batch at a time; one API request
in flight from the sync at any moment; at most STRAVA_SYNC_BULK_CAP (60)
requests per quarter-hour window, then the batch sleeps to the next
boundary inside the same run; the guard's reserve stops the batch with its
cursor persisted (outcome rate_stopped) and tool traffic always keeps its
reserve. Nothing runs on its own: STRAVA_SYNC_ENABLED ships false (A1), the
frequent tier no-ops on an empty store, and only the strava_sync trigger
action can start the first population.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import client, oauth, rate, store as store_mod
from .store import Store, StoreError, utc_now_iso

log = logging.getLogger("mcp.strava.sync")

TICK_S = 60.0  # scheduler tick cadence
LOOK_BACK_DAYS = 7  # frequent walk margin behind the watermark
LIST_PER_PAGE = 100  # the walk's page size (our clamp, not an API claim)
QUARTER_S = 900  # the bulk-cap window (the guard's 15-minute bucket)

SLEEP_FN: Callable[[float], None] = time.sleep  # tests patch this

TIERS = ("frequent", "slow", "sweep", "backfill")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value


def enabled() -> bool:
    """The operator switch (A1: STRAVA_SYNC_ENABLED, rollout default false)."""
    return os.environ.get("STRAVA_SYNC_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def scope_string() -> str:
    """The configured scope string (A1); read_all gates segments/routes (D6)."""
    return os.environ.get(
        "STRAVA_OAUTH_SCOPES", "read,profile:read_all,activity:read_all"
    )


def read_all_granted() -> bool:
    """True when the attended re-authorization added read_all (D6)."""
    return "read_all" in [s.strip() for s in scope_string().split(",")]


def quarter_key(epoch: float | None = None) -> int:
    return int((epoch if epoch is not None else time.time()) // QUARTER_S)


def seconds_to_quarter_end(epoch: float | None = None) -> float:
    epoch = epoch if epoch is not None else time.time()
    return QUARTER_S - (epoch % QUARTER_S) + 1.0


class Pacer:
    """Bulk-cap pacing: at most cap API requests per quarter-hour (rule 3)."""

    def __init__(self, cap: int) -> None:
        self.cap = max(1, cap)
        self.count = 0
        self._key = quarter_key()

    def gate(self) -> None:
        key = quarter_key()
        if key != self._key:
            self._key = key
            self.count = 0
        if self.count >= self.cap:
            wait = seconds_to_quarter_end()
            log.info("bulk cap %d reached; sleeping %.0fs to the quarter boundary",
                     self.cap, wait)
            SLEEP_FN(wait)
            self._key = quarter_key()
            self.count = 0
        self.count += 1


class Counters:
    """Per-batch tallies (added/updated/deleted/errors/api_requests)."""

    def __init__(self) -> None:
        self.api_requests = 0
        self.added = 0
        self.updated = 0
        self.deleted = 0
        self.errors = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "api_requests": self.api_requests,
            "added": self.added,
            "updated": self.updated,
            "deleted": self.deleted,
            "errors": self.errors,
        }


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _walk_page(store: Store, counters: Counters, pacer: Pacer,
               query: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, list[int]]:
    """One list page: upsert summaries; returns (page, known_unchanged, changed).

    changed carries the ids whose upsert reported added or updated - the
    fingerprint changed, so detail/streams refetch work follows (A4).
    """
    pacer.gate()
    counters.api_requests += 1
    batch = client.api_get("/athlete/activities", params=query)
    if not isinstance(batch, list):
        raise StoreError(f"unexpected list response shape: {type(batch)!r}")
    known_unchanged = True
    changed_ids: list[int] = []
    for summary in batch:
        if not isinstance(summary, dict):
            known_unchanged = False
            continue
        result = store.upsert_activity_summary(summary)
        if result == "added":
            counters.added += 1
            known_unchanged = False
            changed_ids.append(summary["id"])
        elif result == "updated":
            counters.updated += 1
            known_unchanged = False
            changed_ids.append(summary["id"])
    return batch, known_unchanged, changed_ids


def _update_watermark(store: Store, batch: list[dict[str, Any]]) -> None:
    """The watermark tracks the newest start_date seen while walking (A4)."""
    newest = None
    for item in batch:
        start = item.get("start_date") if isinstance(item, dict) else None
        if isinstance(start, str) and (newest is None or start > newest):
            newest = start
    if newest:
        current = store.get_state("frequent_watermark")
        if current is None or newest > current:
            store.set_state("frequent_watermark", newest)


def _fetch_photo(store: Store, activity_id: int, detail: dict[str, Any],
                 now: str) -> bool:
    """Primary photo from the detail's primary URL (D7; CDN, not rate-counted)."""
    photos = detail.get("photos")
    if not isinstance(photos, dict):
        return False
    primary = photos.get("primary")
    if not isinstance(primary, dict):
        return False
    urls = primary.get("urls")
    url = urls.get("600") if isinstance(urls, dict) else None
    if not url:
        return False
    ext = os.path.splitext(url.split("?", 1)[0])[1].lstrip(".") or "jpg"
    try:
        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("photo fetch for %s failed (kept as none): %s", activity_id, exc)
        return False
    if resp.status_code != 200:
        log.warning("photo fetch for %s returned %s", activity_id, resp.status_code)
        return False
    store.save_primary_photo(activity_id, resp.content, ext, url, now=now)
    return True


def _process_activity(store: Store, counters: Counters, pacer: Pacer,
                      activity_id: int, now: str, with_efforts: bool = True
                      ) -> None:
    """Detail (include_all_efforts) + streams + primary photo for one id."""
    pacer.gate()
    counters.api_requests += 1
    detail = client.api_get(
        f"/activities/{activity_id}",
        params={"include_all_efforts": "true"} if with_efforts else None,
    )
    if not isinstance(detail, dict):
        counters.errors += 1
        log.warning("detail for %s came back with an unexpected shape", activity_id)
        return
    store.save_detail(activity_id, detail, now=now)
    pacer.gate()
    counters.api_requests += 1
    streams = client.api_get(
        f"/activities/{activity_id}/streams",
        params={"keys": ",".join(tools_stream_keys()), "key_by_type": "true"},
    )
    if isinstance(streams, dict):
        store.save_streams(activity_id, streams, now=now)
    else:
        counters.errors += 1
        log.warning("streams for %s came back with an unexpected shape", activity_id)
    try:
        _fetch_photo(store, activity_id, detail, now)
    except Exception as exc:  # photo failures never fail the item  # noqa: BLE001
        log.warning("photo handling for %s failed: %s", activity_id, exc)


def tools_stream_keys() -> tuple[str, ...]:
    """The fixed 11-key set (imported late to keep tools the single source)."""
    from .tools import STREAM_KEYS  # noqa: PLC0415

    return STREAM_KEYS


class SyncEngine:
    """The tier scheduler + batch runner; one instance per process (A4)."""

    def __init__(self, store_root: str | None = None) -> None:
        self.store_root = store_root
        self._lock = threading.Lock()
        self._queue: collections.deque[str] = collections.deque()
        self._batch_running = False
        self._last_start: dict[str, float] = {}

    # -- configuration ---------------------------------------------------------

    @staticmethod
    def intervals() -> dict[str, float]:
        return {
            "frequent": float(_env_int("STRAVA_SYNC_FREQUENT_HOURS", 6)) * 3600.0,
            "slow": float(_env_int("STRAVA_SYNC_SLOW_HOURS", 24)) * 3600.0,
            "sweep": float(_env_int("STRAVA_SYNC_SWEEP_DAYS", 7)) * 86400.0,
        }

    def bulk_cap(self) -> int:
        return _env_int("STRAVA_SYNC_BULK_CAP", 60)

    # -- trigger (the strava_sync tool's manual surface, L2) ---------------------

    def enqueue(self, tier: str) -> dict[str, Any]:
        """Queue a tier for the next tick; the batch runs behind anything live."""
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
        with self._lock:
            if tier in self._queue:
                return {"queued": False, "position": list(self._queue).index(tier) + 1,
                        "note": "already queued"}
            self._queue.append(tier)
            return {"queued": True, "position": len(self._queue)}

    # -- scheduling --------------------------------------------------------------

    def tick(self, now_mono: float | None = None) -> dict[str, Any] | None:
        """Run at most one batch; returns its summary or None (A4 rule 1)."""
        now = now_mono if now_mono is not None else time.monotonic()
        if self._batch_running:
            return None
        if not enabled():
            return None
        tier = None
        with self._lock:
            if self._queue:
                tier = self._queue.popleft()
        if tier is None:
            tier = self._due_tier(now)
        if tier is None:
            return None
        with self._lock:
            self._batch_running = True
            self._last_start[tier] = now
        try:
            return self.run_batch(tier)
        finally:
            with self._lock:
                self._batch_running = False

    def _due_tier(self, now_mono: float) -> str | None:
        """Which scheduled tier is due; backfill rides the frequent slot."""
        intervals = self.intervals()
        last = self._last_start
        frequent_due = now_mono - last.get("frequent", now_mono - intervals["frequent"]) >= intervals["frequent"]
        slow_due = now_mono - last.get("slow", now_mono - intervals["slow"]) >= intervals["slow"]
        sweep_due = now_mono - last.get("sweep", now_mono - intervals["sweep"]) >= intervals["sweep"]
        if frequent_due or slow_due:
            cursor_set = self._backfill_cursor() is not None
            if frequent_due:
                return "backfill" if cursor_set else "frequent"
            if slow_due:
                return "sweep" if sweep_due else "slow"
        return None

    def _backfill_cursor(self) -> int | None:
        try:
            store = Store(self.store_root)
            raw = store.get_state("backfill_cursor")
            return int(raw) if raw is not None else None
        except (StoreError, ValueError):
            return None

    # -- batch runner --------------------------------------------------------------

    def run_batch(self, tier: str) -> dict[str, Any]:
        """One synchronous batch; the caller owns the one-batch-at-a-time rule."""
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        store = Store(self.store_root)
        store.init_schema()
        store.check_schema()
        batch_id = store.log_batch_start(tier)
        counters = Counters()
        pacer = Pacer(self.bulk_cap())
        now = utc_now_iso()
        outcome = "error"
        note: str | None = None
        try:
            runner = getattr(self, f"_run_{tier}")
            runner_outcome = runner(store, counters, pacer)
            if runner_outcome is not None:
                outcome = runner_outcome
            elif counters.errors:
                outcome = f"done ({counters.errors} errors)"
            else:
                outcome = "done"
        except rate.RateGuardError as exc:
            outcome = "rate_stopped"
            wait = exc.wait if isinstance(exc.wait, dict) else {}
            note = (f"guard reserve at request {counters.api_requests}: "
                    f"quarter reset {wait.get('quarter_hour_reset')}, "
                    f"daily reset {wait.get('daily_reset')}")
            log.info("batch %s stopped at the reserve: %s", tier, note)
        except StoreError as exc:
            outcome = "error"
            note = str(exc)
            log.warning("batch %s failed on the store: %s", tier, exc)
        except Exception as exc:  # the batch never takes the server down  # noqa: BLE001
            outcome = "error"
            note = f"{type(exc).__name__}: {exc}"
            log.exception("batch %s failed", tier)
        tallies = counters.as_dict()
        store.log_batch_end(batch_id, outcome.split(" ")[0], **tallies, note=note)
        return {"tier": tier, "outcome": outcome, "batch_id": batch_id, **tallies,
                "note": note}

    # -- tiers -------------------------------------------------------------------

    def _run_frequent(self, store: Store, counters: Counters, pacer: Pacer) -> str | None:
        now = utc_now_iso()
        if store.counts()["activities"] == 0:
            log.info("frequent tier: empty store, nothing to walk (no_op)")
            return "no_op"
        watermark = store.get_state("frequent_watermark")
        if watermark:
            after_epoch = _iso_to_epoch(watermark)
            if after_epoch is not None:
                after_epoch -= LOOK_BACK_DAYS * 86400.0
        else:
            after_epoch = time.time() - LOOK_BACK_DAYS * 86400.0
        page = 1
        new_ids: list[int] = []
        while True:
            query: dict[str, Any] = {"per_page": LIST_PER_PAGE, "page": page}
            if after_epoch is not None:
                query["after"] = int(after_epoch)
            batch, known_unchanged, changed_ids = _walk_page(store, counters, pacer, query)
            _update_watermark(store, batch)
            for item in batch:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                    continue
                row = store.activity_row(item["id"])
                if row is not None and row["detail_sha256"] is None:
                    new_ids.append(item["id"])
            if len(batch) < LIST_PER_PAGE or known_unchanged:
                break
            page += 1
        for activity_id in dict.fromkeys(new_ids + changed_ids):
            _process_activity(store, counters, pacer, activity_id, now)
        self._snapshot_athlete_stats(store, counters, pacer, now)
        return None

    def _snapshot_athlete_stats(self, store: Store, counters: Counters,
                                pacer: Pacer, now: str) -> None:
        pacer.gate()
        counters.api_requests += 1
        athlete = client.api_get("/athlete")
        if isinstance(athlete, dict):
            store.save_snapshot("athlete", now.replace(":", "").replace("-", "")
                                .replace("+", "_"), athlete, now=now)
        stats_id = oauth.athlete_id(oauth.load_store())
        if stats_id is not None:
            pacer.gate()
            counters.api_requests += 1
            stats = client.api_get(f"/athletes/{stats_id}/stats")
            if isinstance(stats, dict):
                store.save_snapshot("stats", now[:10], stats, now=now)

    def _run_slow(self, store: Store, counters: Counters, pacer: Pacer) -> None:
        now = utc_now_iso()
        pacer.gate()
        counters.api_requests += 1
        zones = client.api_get("/athlete/zones")
        if isinstance(zones, dict):
            store.save_snapshot("zones", now.replace(":", "").replace("-", "")
                                .replace("+", "_"), zones, now=now)
        for gear_id in store.gear_ids_seen():
            pacer.gate()
            counters.api_requests += 1
            try:
                gear = client.api_get(f"/gear/{gear_id}")
            except client.StravaAPIError as exc:
                counters.errors += 1
                log.warning("gear %s fetch failed: %s", gear_id, exc)
                continue
            if isinstance(gear, dict):
                store.save_gear(gear, now=now)
        self._run_scope_tier(store, counters, pacer, now)

    def _run_scope_tier(self, store: Store, counters: Counters, pacer: Pacer,
                        now: str) -> None:
        """Segments and routes: gated on the attended read_all scope (D6)."""
        if not read_all_granted():
            log.info("scope tier skipped: read_all not in the configured scopes")
            return
        page = 1
        while True:
            pacer.gate()
            counters.api_requests += 1
            segments = client.api_get(
                "/segments/starred", params={"per_page": LIST_PER_PAGE, "page": page}
            )
            if not isinstance(segments, list) or not segments:
                break
            for segment in segments:
                if isinstance(segment, dict) and store.save_segment(segment, now=now):
                    pacer.gate()
                    counters.api_requests += 1
                    detail = client.api_get(f"/segments/{segment['id']}")
                    if isinstance(detail, dict):
                        store.save_segment(detail, now=now)
            if len(segments) < LIST_PER_PAGE:
                break
            page += 1
        stats_id = oauth.athlete_id(oauth.load_store())
        if stats_id is None:
            return
        page = 1
        while True:
            pacer.gate()
            counters.api_requests += 1
            routes = client.api_get(
                f"/athletes/{stats_id}/routes",
                params={"per_page": LIST_PER_PAGE, "page": page},
            )
            if not isinstance(routes, list) or not routes:
                break
            for route in routes:
                if not isinstance(route, dict):
                    continue
                if not store.save_route(route, now=now):
                    continue
                route_id = route["id"]
                pacer.gate()
                counters.api_requests += 1
                detail = client.api_get(f"/routes/{route_id}")
                streams = None
                gpx = None
                if isinstance(detail, dict):
                    store.save_route(detail, now=now)
                pacer.gate()
                counters.api_requests += 1
                try:
                    r_streams = client.api_get(f"/routes/{route_id}/streams")
                    streams = r_streams if isinstance(r_streams, dict) else None
                except client.StravaAPIError as exc:
                    counters.errors += 1
                    log.warning("route %s streams failed: %s", route_id, exc)
                pacer.gate()
                counters.api_requests += 1
                try:
                    gpx = client.api_get_bytes(f"/routes/{route_id}/export_gpx")
                except client.StravaAPIError as exc:
                    counters.errors += 1
                    log.warning("route %s gpx export returned %s",
                                route_id, exc.status)
                except httpx.HTTPError as exc:
                    counters.errors += 1
                    log.warning("route %s gpx export failed: %s", route_id, exc)
                store.save_route_aux(route_id, streams, gpx, now=now)
            if len(routes) < LIST_PER_PAGE:
                break
            page += 1

    def _run_sweep(self, store: Store, counters: Counters, pacer: Pacer) -> None:
        """Full list walk: deletions, backdated edits, fingerprint refetches."""
        now = utc_now_iso()
        seen_ids: set[int] = set()
        changed_ids: list[int] = []
        page = 1
        while True:
            batch, _known, page_changed = _walk_page(
                store, counters, pacer, {"per_page": LIST_PER_PAGE, "page": page}
            )
            _update_watermark(store, batch)
            changed_ids.extend(page_changed)
            for item in batch:
                if isinstance(item, dict) and isinstance(item.get("id"), int):
                    seen_ids.add(item["id"])
            if len(batch) < LIST_PER_PAGE:
                break
            page += 1
        open_ids = set(store.open_activity_ids())
        deleted = sorted(open_ids - seen_ids)
        counters.deleted = store.mark_deleted(deleted, now=now)
        if deleted:
            log.info("sweep marked %d activities deleted: %s", len(deleted),
                     deleted[:10])
        for activity_id in dict.fromkeys(changed_ids):
            _process_activity(store, counters, pacer, activity_id, now)

    def _run_backfill(self, store: Store, counters: Counters, pacer: Pacer) -> str | None:
        """The first population (trigger-only): index the list, then fill payloads.

        Resume is the database state itself: an activity with both detail and
        streams fingerprints drops out of the pending set, so a rate stop, a
        429, or a crash resumes exactly where it stopped with no in-memory
        state (section 3.6). backfill_cursor is progress metadata for status;
        the walk phase persists its own page position (backfill_walk_page).
        """
        now = utc_now_iso()
        if store.get_state("backfill_walk_complete") != "1":
            page = 1
            raw = store.get_state("backfill_walk_page")
            if raw and raw.isdigit():
                page = int(raw)
            while True:
                batch, _known, _changed = _walk_page(
                    store, counters, pacer,
                    {"per_page": LIST_PER_PAGE, "page": page},
                )
                _update_watermark(store, batch)
                if len(batch) < LIST_PER_PAGE:
                    store.set_state("backfill_walk_complete", "1")
                    break
                page += 1
                store.set_state("backfill_walk_page", str(page))
        pending = store.summaries_missing_backfill()
        if not pending:
            store.set_state("backfill_cursor", "")
            log.info("backfill: nothing left to fetch")
            return "done"
        for row in pending:
            _process_activity(store, counters, pacer, row["id"], now)
            store.set_state("backfill_cursor", str(row["id"]))
        if not store.summaries_missing_backfill():
            # Everything filled this run: the backfill is complete, the
            # cursor clears, and the frequent tier takes back its slot.
            store.set_state("backfill_cursor", "")
            log.info("backfill complete")
            return "done"
        return None


def _summary_fingerprint_drifts(store: Store, summary: dict[str, Any]) -> bool:
    """True when the walked summary's fingerprint differs from the stored one.

    Kept for the status tooling: the walk itself reports changed ids
    directly (the upsert's added/updated results), which is the check the
    sweep and the frequent tier act on.
    """
    row = store.activity_row(summary["id"])
    if row is None or row["deleted_at"] is not None:
        return False
    raw = json.dumps(summary, indent=1, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest() != row["summary_sha256"]


def start_sync(engine: SyncEngine | None = None,
               tick_s: float = TICK_S) -> asyncio.Task:
    """Start the scheduler task; the caller cancels it on shutdown (A4)."""
    engine = engine or SCHEDULER

    async def _loop() -> None:
        log.info("sync scheduler started (enabled=%s, tick %.0fs)",
                 enabled(), tick_s)
        while True:
            try:
                result = await asyncio.to_thread(engine.tick)
                if result is not None:
                    log.info("sync batch %s: %s", result["tier"], result["outcome"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must survive anything  # noqa: BLE001
                log.warning("sync tick skipped (retried next tick): %s", exc)
            await asyncio.sleep(tick_s)

    return asyncio.create_task(_loop())


# One scheduler per process, like rate.STATE (single-loop assumption).
SCHEDULER = SyncEngine()
