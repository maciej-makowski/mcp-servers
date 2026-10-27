#!/usr/bin/env bash
#
# deploy/install.sh — install the three MCP quadlets for the dedicated `mcp`
# user on rpi5-0 (rootless podman 5.4.2, arm64).
#
# RUN AS THE MCP USER (or as root acting for it, e.g.
#   sudo -u mcp deploy/install.sh
# — root acting for mcp is fine; podman is rootless either way).
#
# What it does:
#   1. pulls the 4 multi-arch images from ghcr.io (google/outlook/notion for
#      the quadlets, plus mcp-proton for the later proton quadlet);
#   2. installs the .container files into ~/.config/containers/systemd/;
#   3. systemd --user daemon-reload;
#   4. starts the three units;
#   5. verifies each port: initialize with the right bearer -> 200, wrong
#      bearer -> 401.
#
# PREREQUISITES (operator one-time steps — see deploy/README.md): ~mcp/env/*.env
# files exist (0600), token volumes are seeded, log dirs exist, linger is on.
#
# No secrets in this file or its output: bearer tokens are read from the env
# files and never echoed.

set -euo pipefail

# sudo -u mcp doesn't set the user-scope bus vars — systemctl --user needs them.
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
  XDG_RUNTIME_DIR="/run/user/$(id -u)"
  DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
  export XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
fi

GHCR=ghcr.io/maciej-makowski
IMAGES=("$GHCR/mcp-google:latest" "$GHCR/mcp-outlook:latest" "$GHCR/mcp-notion:latest" "$GHCR/mcp-proton:latest")

SERVICES=(
  "mcp-google:8011:%h/env/google.env"
  "mcp-outlook:8012:%h/env/outlook.env"
  "mcp-proton:8010:%h/env/proton.env"
  "mcp-notion:8013:%h/env/notion.env"
)

UNIT_DIR="$HOME/.config/containers/systemd"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Expand %h the way systemd would (the env file names in SERVICES use %h).
expand_h() { printf '%s\n' "${1/\%h/$HOME}"; }

log() { printf '[install] %s\n' "$*"; }

fail() { printf '[install] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- pull images
log "pulling 4 images from ghcr (multi-arch, arm64 resolved)..."
for img in "${IMAGES[@]}"; do
  podman pull "$img" || fail "podman pull $img"
  log "  pulled $img"
done

# ------------------------------------------------------------ install quadlets
log "installing quadlets to $UNIT_DIR..."
mkdir -p "$UNIT_DIR"
chmod 700 "$UNIT_DIR"
for svc_port_env in "${SERVICES[@]}"; do
  svc="${svc_port_env%%:*}"
  cp "$SCRIPT_DIR/$svc.container" "$UNIT_DIR/$svc.container" \
    || fail "installing $svc.container"
  chmod 600 "$UNIT_DIR/$svc.container"
done

# -------------------------------------------------------- reload + start units
systemctl --user daemon-reload || fail "daemon-reload"
for svc_port_env in "${SERVICES[@]}"; do
  svc="${svc_port_env%%:*}"
  systemctl --user start "$svc.service" || fail "start $svc.service"
  log "  started $svc.service"
done

# ------------------------------------------------------------------- verify
# initialize with the CORRECT bearer must be 200; with a WRONG bearer must be
# 401. The bearer is read from the service's env file — never printed.
INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"install-check","version":"0"}}}'

verify_one() {
  local name="$1" port="$2" envfile="$3" bearer good wrong
  # Strip optional surrounding quotes ("value" or 'value' -> value). The
  # bearer is never printed.
  bearer="$(grep -E '^MCP_BEARER=' "$envfile" | tail -1 | cut -d= -f2-)" \
    || fail "no MCP_BEARER in $envfile"
  bearer="${bearer%\"}"; bearer="${bearer#\"}"
  bearer="${bearer%\'}"; bearer="${bearer#\'}"
  [ -n "$bearer" ] || fail "MCP_BEARER empty in $envfile"

  good="$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $bearer" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$INIT_BODY" "http://127.0.0.1:${port}/mcp" || true)"
  wrong="$(curl -s -o /dev/null -w '%{http_code}' \
    -H 'Authorization: Bearer definitely-wrong-token' \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$INIT_BODY" "http://127.0.0.1:${port}/mcp" || true)"

  if [ "$good" = "200" ] && [ "$wrong" = "401" ]; then
    log "  OK $name (127.0.0.1:$port): correct bearer 200, wrong bearer 401"
    return 0
  fi
  # Mismatch = not ready yet (or misconfigured); return, don't exit — the
  # caller retries up to 5x before giving up.
  printf '[install] %s (127.0.0.1:%s): correct bearer -> %s (want 200), wrong bearer -> %s (want 401)\n' \
    "$name" "$port" "$good" "$wrong" >&2
  return 1
}

# Units may take a few seconds to come up (npm startup inside the container).
# Poll each endpoint up to 30s (5 attempts x 6s) before declaring failure.
for svc_port_env in "${SERVICES[@]}"; do
  IFS=':' read -r svc port envpath <<<"$svc_port_env"
  ok=""
  for attempt in 1 2 3 4 5; do
    if verify_one "$svc" "$port" "$(expand_h "$envpath")"; then ok=1; break; fi
    log "  $svc not ready (attempt $attempt/5); retrying in 6s..."
    sleep 6
  done
  [ -n "$ok" ] || fail "$svc did not become ready within 30s"
done

log "all three MCP servers verified. Done."
