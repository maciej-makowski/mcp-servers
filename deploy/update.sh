#!/usr/bin/env bash
#
# deploy/update.sh — lightweight rolling update for the MCP quadlets.
#
# RUN AS THE MCP USER (or root acting for it, e.g. sudo -u mcp deploy/update.sh).
#
# For each quadlet in this directory:
#   1. pull the image named in the .container file (multi-arch, arm64 resolved);
#   2. compare the freshly pulled image ID against the image the RUNNING
#      container was started from (`podman inspect <name> -> .Image`);
#   3. only if they differ, recreate the container (stop + rm + start — a bare
#      restart reuses the old container); no-op recreations
#      are skipped so the servers don't blip on every run;
#   4. verify each restarted port the same way install.sh does: initialize
#      with the correct bearer -> 200, with a wrong bearer -> 401.
#
# NOTE: the bearer-verification logic below duplicates the one in install.sh
# deliberately — keeping update.sh standalone (no sourced shared file) means
# it can be copied onto the host and run without dragging install.sh along.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/containers/systemd"
ENV_DIR="$HOME/env"

# svc:port:envfile triples — same as install.sh (proton on 8010).
SERVICES=(
  "mcp-google:8011:google.env"
  "mcp-outlook:8012:outlook.env"
  "mcp-proton:8010:proton.env"
  "mcp-notion:8013:notion.env"
)

log() { printf '[update] %s\n' "$*"; }
fail() { printf '[update] FATAL: %s\n' "$*" >&2; exit 1; }

# image: the Image= line of the quadlet; name: the ContainerName= line.
image_of() { grep -E '^Image=' "$1" | tail -1 | cut -d= -f2-; }
name_of() { grep -E '^ContainerName=' "$1" | tail -1 | cut -d= -f2-; }

# ------------------------------------------------- pull + recreate-if-changed
restarted=()
for svc_port_env in "${SERVICES[@]}"; do
  IFS=':' read -r svc port envname <<<"$svc_port_env"
  quadlet="$SCRIPT_DIR/$svc.container"
  img="$(image_of "$quadlet")"
  cname="$(name_of "$quadlet")"

  log "$svc: pulling $img..."
  podman pull "$img" || fail "podman pull $img"
  pulled_id="$(podman image inspect -f '{{.Id}}' "$img")" || fail "inspect image $img"

  running_id=""
  if podman container exists "$cname"; then
    running_id="$(podman container inspect -f '{{.Image}}' "$cname")"
  fi

  if [ -n "$running_id" ] && [ "$running_id" = "$pulled_id" ]; then
    log "$svc: container already on the latest image ($pulled_id) — skipping"
    continue
  fi

  if [ -n "$running_id" ]; then
    log "$svc: image changed ($running_id -> $pulled_id); recreating..."
  else
    log "$svc: no running container found; starting unit..."
  fi

  # Install the (possibly newer) quadlet so systemd uses the current file.
  cp "$quadlet" "$UNIT_DIR/$svc.container" || fail "installing $svc.container"
  chmod 600 "$UNIT_DIR/$svc.container"
  systemctl --user daemon-reload || fail "daemon-reload"
  # A bare `systemctl --user restart` reuses the existing container — it does
  # NOT pick up a new image or changed mounts (proven live Oct 27: the proton
  # session-mount fix needed an explicit rm). Remove the container first so
  # the next start re-creates it from the freshly pulled image/quadlet.
  systemctl --user stop "$svc.service" 2>/dev/null || true
  podman rm -f "$svc" 2>/dev/null || true
  systemctl --user start "$svc.service" || fail "start $svc.service"
  log "  recreated $svc.service"
  restarted+=("$svc:$port:$envname")
done

if [ "${#restarted[@]}" -eq 0 ]; then
  log "nothing to do — all containers already on the latest images. Done."
  exit 0
fi

# ------------------------------------------------------------------- verify
# initialize with the CORRECT bearer must be 200; with a WRONG bearer must be
# 401. The bearer is read from the service's env file — never printed.
# (Duplicated from install.sh on purpose — see the NOTE in the header.)
INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"update-check","version":"0"}}}'

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
    -H "Authorization: Bearer definitely-wrong-token" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$INIT_BODY" "http://127.0.0.1:${port}/mcp" || true)"

  if [ "$good" = "200" ] && [ "$wrong" = "401" ]; then
    log "  OK $name (127.0.0.1:$port): correct bearer 200, wrong bearer 401"
    return 0
  fi
  printf '[update] %s (127.0.0.1:%s): correct bearer -> %s (want 200), wrong bearer -> %s (want 401)\n' \
    "$name" "$port" "$good" "$wrong" >&2
  return 1
}

for svc_port_env in "${restarted[@]}"; do
  IFS=':' read -r svc port envname <<<"$svc_port_env"
  ok=""
  for attempt in 1 2 3 4 5; do
    if verify_one "$svc" "$port" "$ENV_DIR/$envname"; then ok=1; break; fi
    log "  $svc not ready (attempt $attempt/5); retrying in 6s..."
    sleep 6
  done
  [ -n "$ok" ] || fail "$svc did not become ready within 30s after recreate"
done

log "recreated + verified: ${restarted[*]%%:*}. Done."
