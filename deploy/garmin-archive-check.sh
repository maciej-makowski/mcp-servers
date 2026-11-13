#!/usr/bin/env bash
# garmin-archive-check.sh — daily layer-2 staleness check for the garmin
# archive backup (garmin-local-archive-design-final.md Rev 7 §6.1 step 9).
#
# INDEPENDENT of the backup pipeline (the matrix doc's layer-2 shape: a
# separate process, so a dead backup pipeline is caught by an alive checker).
# It asserts BOTH of:
#   1. today's sentinel line "UPLOADED garmin-archive-<date>" in
#      /var/log/garmin-archive.log, and
#   2. the newest Drive-side garmin-archive-*.age is under 48 h old
#      (a Drive-side check, because a silently-expired Proton session fails
#      exactly here).
# It writes the result to state/backup-status.json in the store dir on BOTH
# outcomes (mode 0644, atomic temp+rename so a mid-write read never serves a
# truncated file), and on failure notifies via the Hermes bot hook
# (/etc/garmin-archive/notify.sh, installed at deploy time and wired to the
# Hermes bot path; when the hook is absent the failure still lands in the
# log and the status file, and cron MAILTO picks it up).
#
# Deploy (root):
#   install -o root -g root -m 0700 garmin-archive-check.sh /usr/local/sbin/
#   /etc/cron.d/garmin-archive-check:  43 9 * * * root /usr/local/sbin/garmin-archive-check.sh >> /var/log/garmin-archive-check.log 2>&1
set -u
# Full PATH export (inherited errata lesson; see garmin-archive-backup.sh).
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

STORE_DIR=/home/mcp/garmin-store
LOG=/var/log/garmin-archive.log
STATUS_FILE="$STORE_DIR/state/backup-status.json"
PROTON=/home/hermes/.hermes/bin/proton
NOTIFY_HOOK=/etc/garmin-archive/notify.sh
TODAY=$(date +%F)
YESTERDAY=$(date -d yesterday +%F 2>/dev/null || date -v-1d +%F)

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

log "=== garmin-archive-check start $TODAY"

# Assertion 1: today's sentinel in the pipeline log.
SENTINEL_OK=0
if [ -r "$LOG" ] && grep -q "UPLOADED garmin-archive-$TODAY" "$LOG"; then
  SENTINEL_OK=1
fi

# Assertion 2: the newest Drive-side garmin-archive-*.age is under 48 h old.
DRIVE_JSON=""
NEWEST_ITEM=""
NEWEST_DATE=""
NEWEST_OK=0
if runuser -u hermes -- env PROTON_NO_UPDATE_CHECK=1 "$PROTON" drive items list \
    /garmin-backups --output json > /tmp/garmin-drive-list.$$.json 2>>/tmp/garmin-drive-list.$$.err; then
  DRIVE_JSON=/tmp/garmin-drive-list.$$.json
else
  log "WARN: proton drive list failed (session or folder problem)"
  DRIVE_JSON=/tmp/garmin-drive-list.$$.err
fi
# The newest item is read from the list; name format garmin-archive-<date>.age.
eval "$(python3 - "$DRIVE_JSON" <<'PY' 2>/dev/null || echo "NEWEST_ITEM=; NEWEST_DATE="
import json, re, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("NEWEST_ITEM=''; NEWEST_DATE=''"); raise SystemExit
items = data if isinstance(data, list) else data.get("items") or data.get("results") or []
best = ""
best_date = ""
for item in items:
    if not isinstance(item, dict):
        continue
    name = str(item.get("name") or item.get("path") or "")
    match = re.fullmatch(r"garmin-archive-(\d{4}-\d{2}-\d{2})\.age", name)
    if not match:
        continue
    if match.group(1) > best_date:
        best_date = match.group(1)
        best = name
print(f"NEWEST_ITEM={best!r}; NEWEST_DATE={best_date!r}")
PY
)"
rm -f /tmp/garmin-drive-list.$$.json /tmp/garmin-drive-list.$$.err
if [ -n "$NEWEST_DATE" ] && { [ "$NEWEST_DATE" = "$TODAY" ] || [ "$NEWEST_DATE" = "$YESTERDAY" ]; }; then
  NEWEST_OK=1
fi

if [ "$SENTINEL_OK" = 1 ] && [ "$NEWEST_OK" = 1 ]; then
  HEALTHY=true
  REASON="ok"
else
  HEALTHY=false
  REASON=""
  [ "$SENTINEL_OK" = 1 ] || REASON="today's UPLOADED sentinel missing in $LOG"
  [ "$NEWEST_OK" = 1 ] || REASON="$REASON${REASON:+; }newest Drive item not under 48 h ($NEWEST_DATE)"
fi

LAST_UPLOAD="$NEWEST_DATE"
AGE_HOURS=""
if [ -n "$NEWEST_DATE" ]; then
  AGE_HOURS=$(python3 -c 'import datetime,sys
try:
    day = datetime.date.fromisoformat(sys.argv[1])
    stamp = datetime.datetime.combine(day, datetime.time(5, 0), tzinfo=datetime.timezone.utc)
    print(max(0, round((datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds() / 3600)))
except Exception:
    print("")' "$NEWEST_DATE" 2>/dev/null || echo "")
fi

mkdir -p "$(dirname "$STATUS_FILE")"
TMP_STATUS="$STATUS_FILE.tmp.$$"
python3 - "$TMP_STATUS" <<PY
import json, sys
payload = {
    "date": "$TODAY",
    "off_host_healthy": $HEALTHY,
    "reason": "$REASON",
    "last_upload": "$LAST_UPLOAD" or None,
    "newest_drive_item": "$NEWEST_ITEM" or None,
    "age_hours": ${AGE_HOURS:-None},
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=1, sort_keys=True)
    fh.write("\n")
PY
if [ -s "$TMP_STATUS" ]; then
  chmod 0644 "$TMP_STATUS"
  mv -f "$TMP_STATUS" "$STATUS_FILE"
  log "backup-status written: healthy=$HEALTHY"
else
  log "ERROR: failed to write backup-status"
  rm -f "$TMP_STATUS"
fi

if [ "$HEALTHY" = true ]; then
  log "=== garmin-archive-check done $TODAY (healthy)"
  exit 0
fi

log "ALARM: garmin off-host copy unhealthy: $REASON"
if [ -x "$NOTIFY_HOOK" ]; then
  "$NOTIFY_HOOK" "garmin archive off-host copy UNHEALTHY ($TODAY): $REASON" || true
else
  log "note: notify hook $NOTIFY_HOOK not installed; failure raised via log + cron MAILTO only"
fi
log "=== garmin-archive-check done $TODAY (UNHEALTHY)"
exit 1
