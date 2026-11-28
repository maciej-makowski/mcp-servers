#!/usr/bin/env bash
# garmin-archive-backup.sh — nightly off-host backup of the Garmin local archive (carriage via the proton MCP relay, Rev 9 M9).
#
# Implements garmin-local-archive-design-final.md (Rev 7 ADOPTED) §6.1 steps
# 1-8 on the matrix-offsite rails (P9): stamp dir + SHA256SUMS + manifest,
# tar (umask 077), age encryption with the matrix recipient (decided M3),
# upload as hermes via the proton CLI to /garmin-backups, the UPLOADED
# sentinel, and the Drive-side 14 d prune with disjoint patterns. The
# steps never destroy the local stamp on failure (§6.1 step 7).
#
# Deploy (root; this repo ships the script, the box gets the cron):
#   install -o root -g root -m 0700 garmin-archive-backup.sh /usr/local/sbin/
#   install -o root -g root -m 0700 garmin-mcp-relay.py /usr/local/sbin/
#   /etc/cron.d/garmin-archive:  47 4 * * * root /usr/local/sbin/garmin-archive-backup.sh >> /var/log/garmin-archive.log 2>&1
# One-time prerequisite: the /garmin-backups Drive folder exists
# (the script creates it on first run when missing; the matrix doc's lesson).
set -u
# Full PATH export: /etc/cron.d's minimal PATH lacks /usr/sbin (runuser) and
# /usr/local/bin (age) — inherited errata lesson (matrix doc §3.1 step 4).
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

STORE_DIR=/home/mcp/garmin-store
STAMP_ROOT=/srv/backups/garmin
LOG=/var/log/garmin-archive.log
AGE_RECIPIENT_FILE=/etc/matrix-backup/age-recipient
RELAY_PY=/usr/local/sbin/garmin-mcp-relay.py
KEEP_LOCAL=7
KEEP_DRIVE_DAYS=14
DATE=$(date +%F)
STAMP="$STAMP_ROOT/garmin-archive-$DATE"
TARBALL=/var/tmp/garmin-archive-$DATE.tar
AGE_FILE=/var/tmp/garmin-archive-$DATE.age

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "=== garmin-archive-backup start $DATE"

if [ ! -d "$STORE_DIR" ]; then
  log "ERROR: store dir $STORE_DIR missing (is GARMIN_STORE_DIR deployed?)"
  exit 1
fi
if [ ! -r "$AGE_RECIPIENT_FILE" ]; then
  log "ERROR: age recipient file $AGE_RECIPIENT_FILE missing (matrix rails, M3)"
  exit 1
fi

# Step 2 (first half): local retention — the newest 7 stamps, pruned first
# (the matrix §3.1 shape: the retention pass lives inside the nightly script).
ls -1dt "$STAMP_ROOT"/garmin-archive-* 2>/dev/null | tail -n +"$((KEEP_LOCAL + 1))" |
while IFS= read -r old; do
  rm -rf -- "$old"
  log "pruned local stamp $old"
done

# Step 2: stamp dir = the store content (activities/, raw/, perday/, point/,
# index.db, state/). cp -a keeps the archive's own mcp:mcp ownership so a
# restore untars with it (§6.2).
rm -rf -- "$STAMP"
mkdir -p "$STAMP_ROOT" "$STAMP"
if ! cp -a "$STORE_DIR/." "$STAMP/"; then
  log "ERROR: stamp copy from $STORE_DIR failed"
  rm -rf -- "$STAMP"
  exit 1
fi

# Step 3: per-file SHA256SUMS + manifest.txt.
cd "$STAMP" || exit 1
find . -type f ! -name SHA256SUMS ! -name manifest.txt -print0 | LC_ALL=C sort -z |
  xargs -0 sha256sum > SHA256SUMS || { log "ERROR: sha256sum failed"; exit 1; }
ACTIVITY_COUNT=$(find activities -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
SCHEMA=$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("schema_version", "unknown"))
except Exception:
    print("unknown")' "$STORE_DIR/state/sync-state.json" 2>/dev/null || echo unknown)
cat > manifest.txt <<MANIFEST
stamp_type: garmin-archive
date: $DATE
source_host: $(hostname)
store_dir: $STORE_DIR
activity_count: $ACTIVITY_COUNT
schema_version: $SCHEMA
created_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)
MANIFEST

# Step 4: tar (umask 077 already set) -> age encrypt with the matrix
# recipient (decided M3) -> shred the plaintext tar.
if ! tar -C "$STAMP" -cf "$TARBALL" .; then
  log "ERROR: tar failed"
  exit 1
fi
AGE_RECIPIENT=$(cat "$AGE_RECIPIENT_FILE")
if [ -z "$AGE_RECIPIENT" ]; then
  log "ERROR: age recipient file is empty"
  shred -u "$TARBALL" 2>/dev/null || rm -f -- "$TARBALL"
  exit 1
fi
if ! age -r "$AGE_RECIPIENT" -o "$AGE_FILE" "$TARBALL"; then
  log "ERROR: age encrypt failed"
  shred -u "$TARBALL" 2>/dev/null || rm -f -- "$TARBALL"
  exit 1
fi
shred -u "$TARBALL" 2>/dev/null || rm -f -- "$TARBALL"

# Step 5: upload through the proton MCP relay (design Rev 9 M9). The
# hermes-side proton CLI was offboarded 2026-11-05; the artifact is staged
# root-owned 0644 into the shared spool (mounted into the proton container)
# and the upload runs as a loopback tools/call. The bearer is read from the
# proton env file as root and exported for the relay helper; it is never
# echoed. The /garmin-backups folder is a one-time manual create (the MCP
# exposes no folder-create verb); a missing folder fails loudly here and
# the 09:43 checker alarms on the missing sentinel.
if [ ! -x "$RELAY_PY" ]; then
  log "ERROR: relay helper $RELAY_PY missing (deploy/ install step)"
  rm -f -- "$AGE_FILE"
  exit 1
fi
MCP_RELAY_BEARER="$(grep -E '^MCP_BEARER=' /home/mcp/env/proton.env 2>/dev/null | tail -1 | cut -d= -f2-)"
if [ -z "$MCP_RELAY_BEARER" ]; then
  log "ERROR: MCP_BEARER missing from /home/mcp/env/proton.env"
  rm -f -- "$AGE_FILE"
  exit 1
fi
export MCP_RELAY_BEARER
STAGE="/srv/mcp_files/send/garmin-archive-$DATE.age"
EXPECTED_SIZE=$(stat -c %s "$AGE_FILE")
install -m 0644 "$AGE_FILE" "$STAGE" \
  || { log "ERROR: spool stage failed"; rm -f -- "$AGE_FILE"; exit 1; }
UP_OUT="$(python3 "$RELAY_PY" \
  "$(printf '{"action":"upload","params":{"path":"%s","dir":"/garmin-backups","if_exists":"replace"},"confirm":true}' "$STAGE")" 2>&1)"
if [ $? -ne 0 ]; then
  # §6.1 step 7: the local stamp survives; the missing UPLOADED line for
  # today is exactly what the 09:43 checker alarms on.
  log "ERROR: proton upload failed via relay (local stamp kept: $STAMP); relay: ${UP_OUT:0:300}"
  rm -f -- "$STAGE" "$AGE_FILE"
  exit 1
fi
# verify the committed name and size against the staged bytes before the
# sentinel (Rev 9 M9)
VERIFY="$(python3 - "$EXPECTED_SIZE" "garmin-archive-$DATE.age" "$UP_OUT" <<'PY'
import json, sys
size = int(sys.argv[1])
want = sys.argv[2]
try:
    stack = [json.loads(sys.argv[3])]
except ValueError:
    stack = []
found = {}
while stack:
    node = stack.pop()
    if isinstance(node, dict):
        # a gated preview envelope means nothing was committed: never bless it
        if node.get("dry_run") is True or node.get("applied") is False:
            continue
        if "name" in node and "size" in node and not found:
            found = node
        stack.extend(node.values())
    elif isinstance(node, list):
        stack.extend(node)
if found.get("name") == want and found.get("size") == size:
    print("OK")
else:
    print("MISMATCH name=%r size=%r (expected %r/%d)"
          % (found.get("name"), found.get("size"), want, size))
PY
)"
if [ "$VERIFY" != "OK" ]; then
  log "ERROR: upload verification failed: $VERIFY (local stamp kept: $STAMP)"
  rm -f -- "$STAGE" "$AGE_FILE"
  exit 1
fi
rm -f -- "$STAGE" "$AGE_FILE"
log "UPLOADED garmin-archive-$DATE"

# Step 6: Drive-side prune through the relay (Rev 9 M9). Retention 14 d
# (decided M1/OD-5); the CLI's pattern+age delete has no MCP equivalent, so
# the script composes it: one relay list, a client-side filter that parses
# the stamp date out of each garmin-archive-<date>.age name, then one gated
# trash per batch with confirm=true as the script's standing operator
# approval (recorded in the design). A failing prune never blocks anything:
# the checker's retention assertion (Rev 9) alarms on drift.
PRUNE_REFS="$(python3 "$RELAY_PY" '{"action":"list","params":{"dir":"/garmin-backups"}}' 2>/dev/null | python3 -c '
import datetime as dt, json, sys
cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
try:
    data = json.loads(sys.stdin.read())
except ValueError:
    raise SystemExit
items = data if isinstance(data, list) else data.get("items") or data.get("results") or []
refs = []
for item in items:
    if not isinstance(item, dict):
        continue
    name = str(item.get("name") or "")
    if not name.startswith("garmin-archive-") or not name.endswith(".age"):
        continue
    day = name[len("garmin-archive-"):-len(".age")]
    if day < cutoff and item.get("link_id"):
        refs.append(item["link_id"])
print(json.dumps(refs))')"
if [ -n "$PRUNE_REFS" ] && [ "$PRUNE_REFS" != "[]" ]; then
  TRASH_OUT="$(python3 "$RELAY_PY" \
    "$(printf '{"action":"trash","params":{"refs":%s},"confirm":true}' "$PRUNE_REFS")" 2>>"$LOG")"
  log "drive prune: trashed older-than-14d stamps (trash relay rc $?; out: ${TRASH_OUT:0:120})"
else
  log "drive prune: nothing older than 14 d"
fi

log "=== garmin-archive-backup done $DATE"
