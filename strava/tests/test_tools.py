"""Tool-layer tests: action tables, reshaping, clamps, downsampling, errors.

The Strava HTTP layer is mocked (house rule): tests monkeypatch
strava_mcp.client.api_get, so no test touches the network.
"""

import pytest

from strava_mcp import client, oauth, rate, tools


SUMMARY = {
    "id": 1,
    "name": "Morning run",
    "sport_type": "Run",
    "type": "Run",  # deprecated field: the reshape must drop it
    "start_date": "2026-11-07T06:00:00Z",
    "distance": 5000.0,
    "moving_time": 1500,
    "elapsed_time": 1600,  # not in the compact dict
    "total_elevation_gain": 12.0,
    "average_speed": 3.3,
    "max_speed": 4.5,
    "private": False,
}


@pytest.fixture()
def api(monkeypatch):
    """Install a fake client.api_get; returns (calls, install(handler))."""
    calls = []

    def install(handler):
        def fake_api_get(path, params=None):
            calls.append({"path": path, "params": params})
            return handler(path, params)

        monkeypatch.setattr(client, "api_get", fake_api_get)

    install(lambda path, params: [])
    return calls, install


def test_unknown_action_is_a_validation_error(api):
    calls, _ = api
    res = tools.ACTIVITIES_TOOL.dispatch("delete", {})
    assert res["validation"] is True and res["success"] is False
    assert calls == []  # nothing reached the network


def test_list_reshapes_compact(api):
    calls, install = api
    install(lambda path, params: [dict(SUMMARY)])
    res = tools.ACTIVITIES_TOOL.dispatch("list", {})
    assert res["success"] is True
    rec = res["activities"][0]
    assert set(rec) == set(tools.SUMMARY_FIELDS)
    assert "type" not in rec  # deprecated field dropped (section 5)
    assert rec["sport_type"] == "Run"
    assert res["count"] == 1
    assert calls[0]["path"] == "/athlete/activities"
    assert calls[0]["params"]["per_page"] == tools.DEFAULT_PER_PAGE
    assert calls[0]["params"]["page"] == 1


def test_list_clamps_and_page_cap(api):
    calls, install = api
    install(lambda path, params: [dict(SUMMARY)] * 100)
    res = tools.ACTIVITIES_TOOL.dispatch(
        "list", {"per_page": 500, "pages": 99, "page": 0}
    )
    assert res["pages"] == tools.MAX_PAGES_PER_CALL  # 4, not 99
    assert len(res["activities"]) == tools.MAX_PAGES_PER_CALL * tools.MAX_PER_PAGE
    assert calls[0]["params"]["per_page"] == tools.MAX_PER_PAGE
    assert calls[0]["params"]["page"] == 1  # page 0 coerced up
    assert len(calls) == 4


def test_list_stops_on_short_page(api):
    """A short page means the last page: no pointless extra request."""
    calls, install = api
    install(lambda path, params: [dict(SUMMARY), dict(SUMMARY, id=2)])
    res = tools.ACTIVITIES_TOOL.dispatch("list", {"pages": 4})
    assert res["count"] == 2
    assert len(calls) == 1


def test_list_epoch_filters(api):
    calls, install = api
    install(lambda path, params: [])
    tools.ACTIVITIES_TOOL.dispatch("list", {"after": 1000, "before": 2000})
    assert calls[0]["params"]["after"] == 1000
    assert calls[0]["params"]["before"] == 2000


def test_get_passthrough_and_efforts(api):
    calls, install = api
    install(lambda path, params: {"id": 7, "name": "Detail"})
    res = tools.ACTIVITIES_TOOL.dispatch("get", {"id": 7})
    assert res == {"id": 7, "name": "Detail"}
    assert calls[0]["path"] == "/activities/7"
    assert calls[0]["params"] is None
    tools.ACTIVITIES_TOOL.dispatch("get", {"id": 7, "include_all_efforts": True})
    assert calls[1]["params"] == {"include_all_efforts": "true"}


def test_get_requires_integer_id(api):
    calls, install = api
    install(lambda path, params: {})
    for bad in (None, "seven", [7]):
        res = tools.ACTIVITIES_TOOL.dispatch("get", {"id": bad})
        assert res["validation"] is True
    assert calls == []


def test_streams_downsampled_by_default(api):
    calls, install = api
    series = list(range(2500))

    def handler(path, params):
        assert path == "/activities/9/streams"
        assert params["key_by_type"] == "true"
        assert params["keys"] == ",".join(tools.STREAM_KEYS)  # CSV since the under-fetch fix
        return {
            "time": {"type": "time", "data": series},
            "latlng": {"type": "latlng", "data": [[1, 2]] * 2500},
        }

    install(handler)
    res = tools.ACTIVITIES_TOOL.dispatch("streams", {"id": 9})
    assert res["success"] is True and res["raw"] is False
    assert len(res["streams"]["time"]["data"]) == 834  # ceil(2500/1000) stride
    assert len(res["streams"]["latlng"]["data"]) == 834
    assert res["streams"]["time"]["data"][:3] == [0, 3, 6]


def test_streams_raw_returns_full_and_warns(api, monkeypatch):
    calls, install = api
    monkeypatch.setattr(tools, "RAW_WARN_TOTAL_POINTS", 1000)
    install(lambda path, params: {"time": {"type": "time", "data": list(range(2500))}})
    res = tools.ACTIVITIES_TOOL.dispatch("streams", {"id": 9, "raw": True})
    assert res["raw"] is True
    assert len(res["streams"]["time"]["data"]) == 2500
    assert "warning" in res


def test_streams_small_raw_no_warning(api):
    calls, install = api
    install(lambda path, params: {"time": {"type": "time", "data": list(range(50))}})
    res = tools.ACTIVITIES_TOOL.dispatch("streams", {"id": 9, "raw": True})
    assert "warning" not in res


def test_streams_requires_id(api):
    calls, install = api
    install(lambda path, params: {})
    res = tools.ACTIVITIES_TOOL.dispatch("streams", {})
    assert res["validation"] is True
    assert calls == []


def test_streams_max_points_respected(api):
    calls, install = api
    install(lambda path, params: {"time": {"type": "time", "data": list(range(100))}})
    res = tools.ACTIVITIES_TOOL.dispatch("streams", {"id": 9, "max_points": 10})
    assert len(res["streams"]["time"]["data"]) == 10


def test_zones_and_laps_paths(api):
    calls, install = api
    install(lambda path, params: {"ok": True})
    tools.ACTIVITIES_TOOL.dispatch("zones", {"id": 11})
    tools.ACTIVITIES_TOOL.dispatch("laps", {"id": 11})
    assert calls[0]["path"] == "/activities/11/zones"
    assert calls[1]["path"] == "/activities/11/laps"


def test_athlete_get_and_zones_paths(api):
    calls, install = api
    install(lambda path, params: {"ok": True})
    tools.ATHLETE_TOOL.dispatch("get", {})
    tools.ATHLETE_TOOL.dispatch("zones", {})
    assert calls[0]["path"] == "/athlete"
    assert calls[1]["path"] == "/athlete/zones"


def test_stats_defaults_to_store_athlete(api, monkeypatch):
    calls, install = api
    install(lambda path, params: {"ok": True})
    monkeypatch.setattr(oauth, "load_store", lambda path=None: {"athlete": {"id": 42}})
    tools.ATHLETE_TOOL.dispatch("stats", {})
    assert calls[0]["path"] == "/athletes/42/stats"


def test_stats_explicit_id_wins_and_string_ids_coerce(api):
    calls, install = api
    install(lambda path, params: {"ok": True})
    tools.ATHLETE_TOOL.dispatch("stats", {"id": 7})
    tools.ATHLETE_TOOL.dispatch("stats", {"id": "7"})
    assert calls[0]["path"] == "/athletes/7/stats"
    assert calls[1]["path"] == "/athletes/7/stats"


def test_stats_without_id_or_store_athlete_fails(api, monkeypatch):
    calls, install = api
    install(lambda path, params: {"ok": True})
    monkeypatch.setattr(oauth, "load_store", lambda path=None: {})
    res = tools.ATHLETE_TOOL.dispatch("stats", {})
    assert res["validation"] is True
    assert calls == []


def test_upstream_errors_come_back_structured(api):
    calls, install = api

    def handler(path, params):
        raise client.StravaAPIError(403, '{"message":"Forbidden"}')

    install(handler)
    res = tools.ATHLETE_TOOL.dispatch("get", {})
    assert res["success"] is False and res["status"] == 403
    assert "Forbidden" in res["error"]


def test_rate_guard_errors_come_back_structured(api):
    calls, install = api

    def handler(path, params):
        raise rate.RateGuardError("at reserve", wait={"quarter_hour_seconds": 60})

    install(handler)
    res = tools.ACTIVITIES_TOOL.dispatch("list", {})
    assert res["success"] is False and res["rate_limited"] is True
    assert res["wait"]["quarter_hour_seconds"] == 60


def test_rate_status_makes_no_api_call(api):
    calls, install = api
    install(lambda path, params: pytest.fail("rate status must not call the API"))
    res = tools.RATE_STATUS_TOOL.dispatch("status", {})
    assert res["success"] is True
    assert res["api_call"] is False
    assert res["15_minute"]["reserve"] == 10
    assert res["daily"]["reserve"] == 100
    assert calls == []


def test_rate_status_reports_seen_budgets(api):
    calls, install = api
    install(lambda path, params: [])
    import httpx

    rate.STATE.update_from_headers(httpx.Headers({"X-ReadRateLimit-Usage": "12,700"}))
    res = tools.RATE_STATUS_TOOL.dispatch("status", {})
    assert res["15_minute"]["used"] == 12
    assert res["daily"]["used"] == 700
    assert res["15_minute"]["remaining"] == 88
