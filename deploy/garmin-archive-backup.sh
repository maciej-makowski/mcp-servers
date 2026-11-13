#!/usr/bin/env bash
# garmin-archive-backup.sh — nightly off-host backup of the Garmin local archive.
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
PROTON=/home/hermes/.hermes/bin/proton
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
# chown hermes:hermes on the .age right after encryption: the hermes-side
# proton CLI must read it (inherited errata lesson, matrix doc step 4).
chown hermes:hermes "$AGE_FILE"

# Step 5: upload as hermes via the proton CLI. The Drive folder is created
# once when missing; without it the first upload fails not-found (matrix doc
# §3.1 prerequisite, same lesson).
if ! runuser -u hermes -- env PROTON_NO_UPDATE_CHECK=1 "$PROTON" drive items list \
    /garmin-backups --output json >/dev/null 2>&1; then
  log "note: /garmin-backups not listable; creating it"
  runuser -u hermes -- env PROTON_NO_UPDATE_CHECK=1 "$PROTON" drive items create \
    /garmin-backups --output json --no-input >>"$LOG" 2>&1 || true
fi
if runuser -u hermes -- env PROTON_NO_UPDATE_CHECK=1 "$PROTON" drive items upload \
    "$AGE_FILE" /garmin-backups --if-exists replace --output json --no-input \
    >>"$LOG" 2>&1; then
  rm -f -- "$AGE_FILE"
  log "UPLOADED garmin-archive-$DATE"
else
  # §6.1 step 7: the local stamp survives; the missing UPLOADED line for
  # today is exactly what the 09:43 checker alarms on.
  log "ERROR: proton upload failed (local stamp kept: $STAMP)"
  rm -f -- "$AGE_FILE"
  exit 1
fi

# Step 6: Drive-side prune, 14 d (decided M1/OD-5), age predicate included,
# scoped to /garmin-backups so the matrix patterns never touch these names
# and vice versa (P8: two pipelines, disjoint namespaces, one Drive account).
runuser -u hermes -- env PROTON_NO_UPDATE_CHECK=1 "$PROTON" drive items delete \
  --scope /garmin-backups --pattern 'garmin-archive-*.age' --older-than 14d \
  --yes --output json >>"$LOG" 2>&1 || log "WARN: drive prune failed (non-fatal)"

log "=== garmin-archive-backup done $DATE"
