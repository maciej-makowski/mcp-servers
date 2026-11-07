"""In-process TTL cache plus retry/backoff for 429/5xx responses.

Personal API rate limits are undocumented, so every read goes through a
short (~10 min) TTL cache and transient failures get exponential backoff
with jitter instead of repeated hard hits from Hermes queries.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

CACHE_TTL_SECONDS = 600  # ~10 minutes; Personal API limits are undocumented
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 20.0
_BACKOFF_ATTEMPTS = 3
_RETRYABLE_STATUS_CODES = {429, *range(500, 600)}


class TTLCache:
    """Minimal thread-safe TTL cache keyed by a hashable key."""

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: Any) -> Any:
        """Return the cached value, or None when absent or expired."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if now >= expires_at:
                del self._entries[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl_seconds, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": self._ttl_seconds,
            }


def is_retryable(exc: BaseException) -> bool:
    """True for 429 and 5xx-shaped errors (duck-typed via ``status_code``)."""
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUS_CODES


def call_with_backoff(
    fn: Callable[[], Any],
    *,
    attempts: int = _BACKOFF_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[], float] | None = None,
) -> Any:
    """Run ``fn``, retrying 429/5xx errors with exponential backoff and jitter.

    ``sleep`` and ``jitter`` are resolved at call time so tests can stub them
    by patching this module's ``time.sleep``. Non-retryable errors and
    exhausted attempts re-raise the original error unchanged.
    """
    if sleep is None:
        sleep = time.sleep
    if jitter is None:
        jitter = random.random
    delay = _BACKOFF_BASE_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — any retryable-shaped error counts
            if attempt >= attempts or not is_retryable(exc):
                raise
            sleep(min(delay, _BACKOFF_MAX_SECONDS) * (0.5 + jitter()))
            delay *= 2
    raise RuntimeError("backoff loop exhausted without re-raising")
