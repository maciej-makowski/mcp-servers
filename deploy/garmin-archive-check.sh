#!/usr/bin/env bash
# garmin-archive-check.sh — daily layer-2 staleness check for the garmin
# archive backup (garmin-local-archive-design-final.md Rev 9 §6.1 step 9;
# the Drive listing rides the proton MCP relay, M9).
#
# INDEPENDENT of the backup pipeline (the matrix doc's layer-2 shape: a
# separate process, so a dead backup pipeline is caught by an alive checker).
# It asserts THREE of:
#   1. today's sentinel line "UPLOADED garmin-archive-<date>" in
#      /var/log/garmin-archive.log,
#   2. the newest Drive-side garmin-archive-*.age is under 48 h old, and
#   3. no retained garmin-archive-*.age is older than 14 d (the prune's own
#      promise; the listing comes through the proton MCP relay, because a
#      silently-expired Proton session fails exactly here).
# It writes the result to state/backup-status.json in the store dir on BOTH
# outcomes (mode 0644, atomic temp+rename so a mid-write read never serves a
# truncated file), and on failure notifies via the Hermes bot hook
# (/etc/garmin-archive/notify.sh, installed at deploy time and wired to the
# Hermes bot path; when the hook is absent the failure still lands in the
# log and the status file, and cron MAILTO picks it up).
#
# Deploy (root):
#   install -o root -g root -m 0700 garmin-archive-check.sh /usr/local/sbin/
#   install -o root -g root -m 0700 garmin-mcp-relay.py /usr/local/sbin/
#   /etc/cron.d/garmin-archive-check:  43 9 * * * root /usr/local/sbin/garmin-archive-check.sh >> /var/log/garmin-archive-check.log 2>&1
set -u
# Full PATH export (inherited errata lesson; see garmin-archive-backup.sh).
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

STORE_DIR=/home/mcp/garmin-store
LOG=/var/log/garmin-archive.log
STATUS_FILE="$STORE_DIR/state/backup-status.json"
RELAY_PY=/usr/local/sbin/garmin-mcp-relay.py
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

# Assertion 2 (and the Rev 9 retention assertion): the Drive-side listing
# comes through the proton MCP relay (design Rev 9 M9; the CLI is offboarded).
MCP_RELAY_BEARER="$(grep -E '^MCP_BEARER=' /home/mcp/env/proton.env 2>/dev/null | tail -1 | cut -d= -f2-)"
export MCP_RELAY_BEARER
DRIVE_JSON=""
NEWEST_ITEM=""
NEWEST_DATE=""
NEWEST_OK=0
if LIST_OUT="$(python3 "$RELAY_PY" '{"action":"list","params":{"dir":"/garmin-backups"}}' 2>/tmp/garmin-drive-list.$$.err)" \
   && printf '%s\n' "$LIST_OUT" > /tmp/garmin-drive-list.$$.json; then
  DRIVE_JSON=/tmp/garmin-drive-list.$$.json
else
  log "WARN: proton relay drive list failed (transport, auth, or folder problem)"
  DRIVE_JSON=/tmp/garmin-drive-list.$$.err
fi
# The newest item is read from the list; name format garmin-archive-<date>.age.
# The same pass emits the oldest retained stamp date for the Rev 9 retention
# assertion (no garmin-archive-*.age older than 14 d).
eval "$(python3 - "$DRIVE_JSON" <<'PY' 2>/dev/null || echo "NEWEST_ITEM=; NEWEST_DATE=; OLDEST_DATE="
import json, re, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("NEWEST_ITEM=''; NEWEST_DATE=''; OLDEST_DATE=''"); raise SystemExit
items = data if isinstance(data, list) else data.get("items") or data.get("results") or []
best = ""
best_date = ""
oldest = ""
for item in items:
    if not isinstance(item, dict):
        continue
    name = str(item.get("name") or item.get("path") or "")
    match = re.fullmatch(r"garmin-archive-(\d{4}-\d{2}-\d{2})\.age", name)
    if not match:
        continue
    day = match.group(1)
    if day > best_date:
        best_date = day
        best = name
    if not oldest or day < oldest:
        oldest = day
print(f"NEWEST_ITEM={best!r}; NEWEST_DATE={best_date!r}; OLDEST_DATE={oldest!r}")
PY
)"
rm -f /tmp/garmin-drive-list.$$.json /tmp/garmin-drive-list.$$.err
if [ -n "$NEWEST_DATE" ] && { [ "$NEWEST_DATE" = "$TODAY" ] || [ "$NEWEST_DATE" = "$YESTERDAY" ]; }; then
  NEWEST_OK=1
fi
# Rev 9 retention assertion: no garmin-archive-*.age older than 14 d (the
# prune's own promise; a dead prune otherwise drifts invisibly).
CUTOFF=$(date -d "14 days ago" +%F 2>/dev/null || date -v-14d +%F)
RETENTION_OK=0
if [ -z "$OLDEST_DATE" ] || [ "$OLDEST_DATE" \> "$CUTOFF" ] || [ "$OLDEST_DATE" = "$CUTOFF" ]; then
  RETENTION_OK=1
fi

if [ "$SENTINEL_OK" = 1 ] && [ "$NEWEST_OK" = 1 ] && [ "$RETENTION_OK" = 1 ]; then
  HEALTHY=true
  REASON="ok"
else
  HEALTHY=false
  REASON=""
  [ "$SENTINEL_OK" = 1 ] || REASON="today's UPLOADED sentinel missing in $LOG"
  [ "$NEWEST_OK" = 1 ] || REASON="$REASON${REASON:+; }newest Drive item not under 48 h ($NEWEST_DATE)"
  [ "$RETENTION_OK" = 1 ] || REASON="$REASON${REASON:+; }oldest retained stamp older than 14 d ($OLDEST_DATE; cutoff $CUTOFF)"
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
# The payload values cross into Python as ARGV, and the heredoc delimiter is
# quoted: zero shell interpolation inside Python. The old inline form
# interpolated $HEALTHY straight into the source, producing the bash literals
# false/true — NameError on every run, on BOTH outcomes, so backup-status.json
# was never written once since deploy (live-verified Nov 15-16 on rpi5-0:
# "NameError: name 'false' is not defined" in every checker log).
python3 - "$TMP_STATUS" "$TODAY" "$HEALTHY" "$REASON" "$LAST_UPLOAD" \
  "$NEWEST_ITEM" "$AGE_HOURS" <<'PY'
import json, sys
_tmp, _today, _healthy, _reason, _last, _item, _age = sys.argv[1:8]
payload = {
    "date": _today,
    "off_host_healthy": _healthy == "true",
    "reason": _reason,
    "last_upload": _last or None,
    "newest_drive_item": _item or None,
    "age_hours": int(_age) if _age else None,
}
with open(_tmp, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=1, sort_keys=True)
    fh.write("\n")
PY
if [ -s "$TMP_STATUS" ]; then
  chmod 0644 "$TMP_STATUS"
  mv -f "$TMP_STATUS" "$STATUS_FILE"
  # The store belongs to the mcp user (the container reads and writes its
  # own state there); hand the root-owned writes over. Without this the
  # root-owned state/ blocks the sync engine's own state writes (backfill
  # stalls) and garmin_api_status fails with Permission denied. A failed
  # chown must stay loud: silence here re-introduces the exact bug this
  # guards against, invisibly.
  chown mcp:mcp "$(dirname "$STATUS_FILE")" "$STATUS_FILE" \
    || log "WARN: chown to mcp failed (the container may not read/write state)"
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
