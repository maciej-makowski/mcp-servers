"""Rate guard tests: W2 header format, D5 reserves, reset waits."""

import datetime as dt

import httpx
import pytest

from strava_mcp import rate


def test_parse_pair():
    assert rate._parse_pair("27,180") == (27, 180)
    assert rate._parse_pair(" 27 , 180 ") == (27, 180)
    assert rate._parse_pair("bogus") is None
    assert rate._parse_pair("1,2,3") is None
    assert rate._parse_pair(None) is None


def test_update_from_headers_ingests_usage_and_limits():
    state = rate.RateState()
    state.update_from_headers(
        httpx.Headers(
            {
                "X-ReadRateLimit-Usage": "27,180",
                "X-ReadRateLimit-Limit": "100,1000",
                "X-RateLimit-Usage": "50,400",
            }
        )
    )
    assert state.short == {"used": 27, "limit": 100}
    assert state.daily == {"used": 180, "limit": 1000}
    assert state.overall == {"short": 50, "daily": 400}
    assert state.last_seen is not None


def test_fresh_state_allows_reads():
    rate.RateState().check()  # no headers seen yet: defaults leave room


def test_check_refuses_short_bucket_at_reserve():
    state = rate.RateState()
    state.update_from_headers(httpx.Headers({"X-ReadRateLimit-Usage": "90,500"}))
    with pytest.raises(rate.RateGuardError) as exc:
        state.check()
    assert "remaining 10" in str(exc.value)
    assert exc.value.wait["quarter_hour_reset"]
    assert exc.value.wait["daily_reset"]


def test_check_allows_above_reserve():
    state = rate.RateState()
    # 89 of 100 -> 11 left (> reserve 10); 899 of 1000 -> 101 left.
    state.update_from_headers(httpx.Headers({"X-ReadRateLimit-Usage": "89,899"}))
    state.check()


def test_check_refuses_daily_bucket_at_reserve():
    state = rate.RateState()
    state.update_from_headers(httpx.Headers({"X-ReadRateLimit-Usage": "5,900"}))
    with pytest.raises(rate.RateGuardError):
        state.check()


def test_wait_after_429_reports_both_resets():
    state = rate.RateState()
    now = dt.datetime(2026, 11, 7, 12, 5, 30, tzinfo=dt.timezone.utc)
    wait = state.wait_after_429(now=now)
    assert wait["quarter_hour_seconds"] == 570  # 12:05:30 -> 12:15
    assert wait["daily_seconds"] == 11 * 3600 + 54 * 60 + 30
    assert wait["quarter_hour_reset"] == "2026-11-07T12:15:00+00:00"
    assert wait["daily_reset"] == "2026-11-08T00:00:00+00:00"


def test_next_quarter_hour():
    now = dt.datetime(2026, 11, 7, 12, 5, 0, tzinfo=dt.timezone.utc)
    nxt = rate.next_quarter_hour(now)
    assert (nxt - now).total_seconds() == 600
    assert nxt.minute == 15 and nxt.second == 0


def test_next_quarter_hour_on_a_boundary():
    now = dt.datetime(2026, 11, 7, 12, 30, 0, tzinfo=dt.timezone.utc)
    assert rate.next_quarter_hour(now) == dt.datetime(
        2026, 11, 7, 12, 45, tzinfo=dt.timezone.utc
    )


def test_next_daily_reset():
    now = dt.datetime(2026, 11, 7, 23, 59, tzinfo=dt.timezone.utc)
    assert rate.next_daily_reset(now) == dt.datetime(
        2026, 11, 8, 0, 0, tzinfo=dt.timezone.utc
    )


def test_status_shape():
    state = rate.RateState()
    state.update_from_headers(httpx.Headers({"X-ReadRateLimit-Usage": "3,40"}))
    out = state.status()
    assert out["15_minute"]["used"] == 3
    assert out["15_minute"]["limit"] == 100
    assert out["15_minute"]["remaining"] == 97
    assert out["15_minute"]["reserve"] == 10
    assert out["daily"]["used"] == 40
    assert out["daily"]["reserve"] == 100
    assert out["daily"]["next_reset"]
    assert out["last_seen"]
