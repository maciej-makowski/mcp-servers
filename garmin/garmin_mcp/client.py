"""Thin wrapper over the official Garmin Connect Personal API client.

``PersonalApiAuthenticator`` exchanges the ``GARMIN_API_KEY`` env var for a
24h bearer token and caches it 0600, re-deriving it from the key when less
than an hour of validity remains; ``GarminPersonalApiClient`` handles the
authenticated reads. This module wires the two together, maps every tool
family to the matching official wrapper, and tracks per-family last errors
for ``garmin_api_status``.

All official-client call sites are module-level attributes or functions so
tests can monkeypatch them (house convention: no real network in tests).
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from typing import Any

from garmin_personal_api import (
    BearerToken,
    GarminPersonalApiClient,
    PersonalApiAuthenticator,
)

API_KEY_ENV_VAR = "GARMIN_API_KEY"

# Monkeypatch targets (module-level call sites, per AGENTS.md).
authenticator_cls: type = PersonalApiAuthenticator
api_client_cls: type = GarminPersonalApiClient

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_AUTHENTICATOR: Any = None
_API_CLIENT: Any = None
_TOKEN: BearerToken | None = None
_FAMILY_ERRORS: dict[str, dict[str, Any]] = {}


class GarminAuthError(RuntimeError):
    """Raised when GARMIN_API_KEY is missing or the token exchange fails."""


def _reset_state() -> None:
    """Drop cached authenticator/client/token (test isolation helper)."""
    global _AUTHENTICATOR, _API_CLIENT, _TOKEN
    with _LOCK:
        _AUTHENTICATOR = None
        _API_CLIENT = None
        _TOKEN = None


def _api_key() -> str:
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not key:
        raise GarminAuthError(
            f"{API_KEY_ENV_VAR} is not set; the server cannot authenticate to Garmin"
        )
    return key


def obtain_bearer_token() -> BearerToken:
    """Return a valid bearer token; the authenticator re-derives it when needed."""
    return _authenticator().obtain_token(_api_key())


def _authenticator() -> Any:
    global _AUTHENTICATOR
    with _LOCK:
        if _AUTHENTICATOR is None:
            _AUTHENTICATOR = authenticator_cls()
    return _AUTHENTICATOR


def api_client() -> Any:
    """Return the shared API client bound to a currently valid bearer token.

    The official authenticator reuses its cached token and only re-derives
    one from the key when validity drops under an hour, so repeated calls
    stay cheap.
    """
    global _API_CLIENT, _TOKEN
    token = obtain_bearer_token()
    with _LOCK:
        if _API_CLIENT is None:
            _API_CLIENT = api_client_cls(token.access_token)
        else:
            _API_CLIENT.update_bearer_token(token.access_token)
        _TOKEN = token
    return _API_CLIENT


def record_family_ok(family: str) -> None:
    with _STATE_LOCK:
        _FAMILY_ERRORS.pop(family, None)


def record_family_error(family: str, exc: BaseException) -> None:
    with _STATE_LOCK:
        _FAMILY_ERRORS[family] = {
            "error": str(exc),
            "status_code": getattr(exc, "status_code", None),
            "ts": time.time(),
        }


def family_errors() -> dict[str, dict[str, Any]]:
    with _STATE_LOCK:
        return {family: dict(info) for family, info in _FAMILY_ERRORS.items()}


# --- tool-facing wrappers -------------------------------------------------


def search_activities(limit: int, since: str) -> Any:
    return api_client().search_activities(start=0, limit=limit, start_date=since or None)


def get_activity(activity_id: str) -> Any:
    return api_client().get_activity(activity_id)


def get_activity_workouts(activity_id: str) -> Any:
    return api_client().get_activity_workouts(activity_id)


def download_activity(activity_id: str) -> bytes:
    return api_client().download_activity(activity_id)


def fetch_ranged(
    family: str,
    start: dt.date,
    end: dt.date,
    *,
    chunks: int | None = None,
) -> Any:
    """Dispatch one ranged family call to the matching official wrapper."""
    c = api_client()
    if family == "stress":
        return c.get_daily_stress(start, end)
    if family == "steps":
        return c.get_daily_steps(start, end)
    if family == "hydration":
        return c.get_daily_hydration(start, end)
    if family == "weight":
        return c.get_weight_measurements(start, end)
    if family == "sleep":
        return c.get_daily_sleep_summaries(start, end)
    if family == "body_battery":
        return c.get_daily_body_battery(start, end, chunks=chunks)
    if family == "daily":
        return c.get_daily_summaries(start, end)
    if family == "hrv":
        return c.get_daily_hrv_summaries(start, end)
    raise ValueError(f"unknown ranged family {family!r}")


def fetch_perday(family: str, day: dt.date) -> Any:
    """Dispatch one per-day family call (these endpoints take a single date)."""
    c = api_client()
    if family == "spo2":
        return c.get_daily_spo2(day)
    if family == "fitness_age":
        return c.get_fitness_age(day)
    if family == "heart_rate":
        return c.get_daily_heart_rate(day)
    raise ValueError(f"unknown per-day family {family!r}")


# --- archive-only dispatch (garmin-local-archive design §2.1) ----------------
#
# The archive sync (garmin_mcp.sync) and the store read path
# (garmin_mcp.readpath) reach every family the Personal API publishes, not
# just the ones the seven tools expose. These call sites stay module-level so
# tests can monkeypatch them (house convention); no existing tool dispatch
# changes.

_ARCHIVE_RANGED_WRAPPERS: dict[str, str] = {
    "stress": "get_daily_stress",
    "steps": "get_daily_steps",
    "hydration": "get_daily_hydration",
    "weight": "get_weight_measurements",
    "weight_goals": "get_effective_weight_goals",
    "sleep": "get_daily_sleep_summaries",
    "body_battery": "get_daily_body_battery",
    "daily": "get_daily_summaries",
    "hrv": "get_daily_hrv_summaries",
    "wellness_activities": "get_wellness_activity_summaries",
}

_ARCHIVE_PERDAY_WRAPPERS: dict[str, str] = {
    "sleep_detail": "get_daily_sleep",
    "hrv_detail": "get_daily_hrv",
    "nutrition": "get_daily_food_logs",
    "spo2": "get_daily_spo2",
    "fitness_age": "get_fitness_age",
    "heart_rate": "get_daily_heart_rate",
    "menstrual_summary": "get_menstrual_cycle_summary",
    "menstrual_day_view": "get_menstrual_cycle_day_view",
}


def search_activities_page(start: int, limit: int) -> Any:
    """One raw search page at an arbitrary offset (sync paging, §4.2)."""
    return api_client().search_activities(start=start, limit=limit)


def fetch_ranged_archive(family: str, start: dt.date, end: dt.date) -> Any:
    """Dispatch one ranged ARCHIVE family call (the 11 families of §2.1)."""
    c = api_client()
    wrapper = _ARCHIVE_RANGED_WRAPPERS.get(family)
    if wrapper is not None:
        if family == "body_battery":
            return c.get_daily_body_battery(start, end)
        return getattr(c, wrapper)(start, end)
    if family == "weight_all":
        # No official wrapper (P5): reached via the generic escape hatch.
        return c.get(f"/weight-service/weight/all/{start}/{end}")
    raise ValueError(f"unknown ranged archive family {family!r}")


def fetch_perday_archive(family: str, day: dt.date) -> Any:
    """Dispatch one per-day ARCHIVE family call (the 8 families of §2.1)."""
    c = api_client()
    wrapper = _ARCHIVE_PERDAY_WRAPPERS.get(family)
    if wrapper is None:
        raise ValueError(f"unknown per-day archive family {family!r}")
    if family == "menstrual_day_view":
        return c.get_menstrual_cycle_day_view(day, day)
    return getattr(c, wrapper)(day)


def fetch_point(name: str) -> Any:
    """Dispatch one of the three point reads (no date; §2.1)."""
    c = api_client()
    if name == "profile_settings":
        return c.get_user_profile_settings()
    if name == "hr_zones":
        return c.get_heart_rate_zones()
    if name == "legacy_daily":
        # No official wrapper (P5): reached via the generic escape hatch.
        return c.get("/usersummary-service/usersummary/daily")
    raise ValueError(f"unknown point read {name!r}")
