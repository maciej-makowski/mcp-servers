"""First-authorization helper (design section 3.3, D7).

Run inside the container (the deploy/KEY-ROTATION.md and design section 3.3
podman-exec form):

    podman exec -i mcp-strava uv run --no-sync python -m strava_mcp.authorize

Flow: print the authorization URL (client id from the env file, D3 scope
list, redirect_uri inside the app's callback domain 127.0.0.1), read the
code Maciek pastes from the address bar, exchange it at the token endpoint,
write token.json atomically into the mounted token volume, and print
success without echoing any token value.

Note: this is the v3 API's plain code flow (W1 records client_id,
redirect_uri, response_type=code and a client-secret exchange; the PKCE S256
flow belongs to the option E connector's MCP-issuer endpoints, sections 2.5
and 3.7, not to this helper).
"""

from __future__ import annotations

import sys

from . import oauth

AUTHORIZE_URL = oauth.AUTHORIZE_URL  # https://www.strava.com/oauth/authorize
SCOPES = oauth.SCOPES  # D3: read,profile:read_all,activity:read_all
REDIRECT_URI = "http://127.0.0.1/exchange_token"


def build_auth_url(client_id: str) -> str:
    """The URL the browser opens; scopes comma-delimited per W1."""
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def main() -> None:
    try:
        client_id, _ = oauth._client_credentials()
    except oauth.TokenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "The env file keys STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be "
            "set (deploy/strava.env.example).",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    print("Open this URL in your browser, authorize the app, and Strava will")
    print("land you on a page that cannot load: copy the code from the")
    print("address bar (the ?code=... value) and paste it here.")
    print()
    print(f"  {build_auth_url(client_id)}")
    print()
    print("Paste the code (empty input aborts):")
    try:
        code = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        code = ""
    if not code:
        print("No code entered: nothing was written.", file=sys.stderr)
        raise SystemExit(1)

    try:
        store = oauth.exchange_code(code)
    except oauth.TokenError as exc:
        print(f"ERROR: exchange failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    athlete = store.get("athlete") or {}
    athlete_note = f" for athlete id {athlete.get('id')}" if athlete.get("id") else ""
    # Success message carries names and modes only: no token values.
    print(f"OK: token store written to {oauth.TOKEN_PATH}{athlete_note}")
    print("The server picks it up on its next call; no restart needed.")


if __name__ == "__main__":
    main()
