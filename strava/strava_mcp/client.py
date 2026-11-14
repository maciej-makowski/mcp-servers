"""HTTP client for the Strava v3 API (design section 3.2).

One guard-and-send path for every proxied call:

1. the rate guard refuses up front when a bucket is at or below its D5
   reserve, so no request is spent (section 3.4),
2. the request carries the stored access token as a Bearer,
3. every response's rate headers update the tracker (W2 format),
4. a 401 refreshes the token once and retries once (the access token
   rotates every six hours); a second 401 surfaces as an auth error,
5. a 429 raises a RateGuardError carrying the wait-to-reset instead of
   retrying internally, so the agent decides (section 3.4).

No network happens at import time; tests swap in httpx.MockTransport.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import oauth, rate

BASE_URL = "https://www.strava.com/api/v3"
REQUEST_TIMEOUT_S = 60.0


class StravaAPIError(RuntimeError):
    """Upstream HTTP error surfaced with its status and body snippet.

    The tool layer returns Strava's error fields as-is (F3): public data
    keeps working while a permission error names the missing scope.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"strava api returned {status}: {body[:300]}")
        self.status = status
        self.body = body


def _client() -> httpx.Client:
    """Fresh httpx client per call; tests inject a MockTransport here."""
    return httpx.Client(base_url=BASE_URL, timeout=REQUEST_TIMEOUT_S)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _guarded_send(http: httpx.Client, path: str,
                  params: dict[str, Any] | None) -> httpx.Response:
    """Send one request with the shared guard/refresh/429 semantics."""
    store = oauth.load_store()
    resp = http.get(path, params=params, headers=_auth(oauth.access_token(store)))
    rate.STATE.update_from_headers(resp.headers)
    if resp.status_code == 401:
        # Retry once after a refresh (section 3.2): the access token
        # rotates every six hours.
        refreshed = oauth.refresh_access_token()
        resp = http.get(path, params=params,
                        headers=_auth(oauth.access_token(refreshed)))
        rate.STATE.update_from_headers(resp.headers)
    if resp.status_code == 429:
        wait = rate.STATE.wait_after_429()
        raise rate.RateGuardError(
            "strava returned 429 too many requests; wait before retrying",
            wait=wait,
        )
    if resp.status_code >= 400:
        raise StravaAPIError(resp.status_code, resp.text)
    return resp


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Guarded GET; returns the parsed JSON body of a 2xx response."""
    rate.STATE.check()  # D5 pre-check: refuse before spending a request
    with _client() as http:
        return _guarded_send(http, path, params).json()


def api_get_bytes(path: str, params: dict[str, Any] | None = None) -> bytes:
    """Guarded GET returning the raw body (the routes GPX export; sync use).

    Same guard, refresh, and 429 semantics as api_get; the sync's bulk cap
    gates the call count around it (section 3.4).
    """
    rate.STATE.check()
    with _client() as http:
        return _guarded_send(http, path, params).content
