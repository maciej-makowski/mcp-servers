"""TTL expiry, stats, and the 429/5xx backoff behaviour."""
import pytest

from garmin_mcp import cache as cache_mod
from garmin_mcp.cache import TTLCache, call_with_backoff


class HttpLike(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_ttl_expiry_and_stats():
    now = [0.0]
    c = TTLCache(ttl_seconds=10, clock=lambda: now[0])
    c.set("k", "v")
    assert c.get("k") == "v"
    now[0] += 9.9
    assert c.get("k") == "v"  # still fresh
    now[0] += 0.2
    assert c.get("k") is None  # expired
    stats = c.stats()
    assert stats["hits"] == 2 and stats["misses"] == 1 and stats["entries"] == 0


def test_stats_shape():
    c = TTLCache(ttl_seconds=600, clock=lambda: 0.0)
    c.set("a", 1)
    c.get("a")
    c.get("missing")
    assert c.stats() == {"entries": 1, "hits": 1, "misses": 1, "ttl_seconds": 600}


def test_module_default_ttl_is_about_ten_minutes():
    assert cache_mod.CACHE_TTL_SECONDS == 600


def test_missing_key_is_a_miss():
    c = TTLCache(clock=lambda: 0.0)
    assert c.get("nope") is None
    assert c.stats()["misses"] == 1


def test_backoff_retries_429_then_succeeds():
    sleeps = []
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise HttpLike(429)
        return "ok"

    out = call_with_backoff(flaky, sleep=sleeps.append, jitter=lambda: 0.0)
    assert out == "ok"
    assert len(calls) == 3
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]  # exponential growth


def test_backoff_does_not_retry_client_errors():
    sleeps = []

    def bad():
        raise HttpLike(400)

    with pytest.raises(HttpLike):
        call_with_backoff(bad, sleep=sleeps.append)
    assert sleeps == []


def test_backoff_exhausts_retries_on_5xx():
    sleeps = []

    def broken():
        raise HttpLike(503)

    with pytest.raises(HttpLike):
        call_with_backoff(broken, attempts=3, sleep=sleeps.append, jitter=lambda: 0.0)
    assert len(sleeps) == 2


def test_is_retryable_duck_types_status_code():
    assert cache_mod.is_retryable(HttpLike(429))
    assert cache_mod.is_retryable(HttpLike(500))
    assert not cache_mod.is_retryable(HttpLike(403))
    assert not cache_mod.is_retryable(RuntimeError("boom"))
    assert not cache_mod.is_retryable(RuntimeError())
