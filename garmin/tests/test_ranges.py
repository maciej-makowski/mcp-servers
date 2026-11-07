"""Range-splitting math: caps, multi-cap windows, single-day edges."""
import datetime as dt

import pytest

from garmin_mcp.ranges import (
    GARMIN_CAP_SLEEP_DAYS,
    GARMIN_CAP_STEPS_DAYS,
    parse_date,
    split_range,
)

D = dt.date


def test_range_within_cap_single_chunk():
    chunks = split_range(D(2026, 10, 1), D(2026, 10, 28), GARMIN_CAP_STEPS_DAYS)
    assert chunks == [(D(2026, 10, 1), D(2026, 10, 28))]


def test_range_spanning_multiple_caps():
    chunks = split_range(D(2026, 10, 1), D(2026, 11, 15), GARMIN_CAP_STEPS_DAYS)
    assert chunks == [
        (D(2026, 10, 1), D(2026, 10, 28)),
        (D(2026, 10, 29), D(2026, 11, 15)),
    ]
    assert [(e - s).days + 1 for s, e in chunks] == [28, 18]


def test_range_spanning_multiple_caps_is_contiguous_and_bounded():
    chunks = split_range(D(2026, 10, 1), D(2026, 12, 31), GARMIN_CAP_SLEEP_DAYS)
    assert chunks[0][0] == D(2026, 10, 1)
    assert chunks[-1][1] == D(2026, 12, 31)
    assert all((e - s).days + 1 <= GARMIN_CAP_SLEEP_DAYS for s, e in chunks)
    assert all((chunks[i + 1][0] - chunks[i][1]).days == 1
               for i in range(len(chunks) - 1))
    assert sum((e - s).days + 1 for s, e in chunks) == (D(2026, 12, 31) - D(2026, 10, 1)).days + 1


def test_exactly_two_caps():
    chunks = split_range(D(2026, 10, 1), D(2026, 11, 25), 28)
    assert len(chunks) == 2
    assert all((e - s).days + 1 == 28 for s, e in chunks)


def test_start_equals_end_single_day():
    assert split_range(D(2026, 11, 7), D(2026, 11, 7), GARMIN_CAP_SLEEP_DAYS) == [
        (D(2026, 11, 7), D(2026, 11, 7))
    ]


def test_inverted_range_raises():
    with pytest.raises(ValueError):
        split_range(D(2026, 11, 7), D(2026, 11, 1), 28)


def test_zero_cap_raises():
    with pytest.raises(ValueError):
        split_range(D(2026, 11, 1), D(2026, 11, 7), 0)


def test_parse_date():
    assert parse_date("2026-11-07") == D(2026, 11, 7)
    assert parse_date(D(2026, 11, 7)) == D(2026, 11, 7)
    with pytest.raises(ValueError):
        parse_date("07/11/2026")
