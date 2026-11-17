#!/usr/bin/env bash
# Cron entry point for the daily clips run (python -m app.clips daily).
# Installation and operation: scripts/schedule_clips.md.
#
# Cron calls this every 30 minutes; the script decides whether to run:
# - not before 08:00 in Sao Paulo, whatever the WSL clock's zone is;
# - once per day: a successful run leaves <state>/<day>.done;
# - at most CLIPS_CRON_MAX_ATTEMPTS failed tries per day (default 4), so a
#   persistent failure stops calling the billed classifier;
# - never two at once (flock on <state>/lock).
# A day missed because WSL was not running is caught up on the first tick
# after it starts, as long as it is still the same day in Sao Paulo.
#
# State and logs: data/clips-cron/ (git-ignored). One log file per day.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${CLIPS_CRON_STATE:-$REPO/data/clips-cron}"
MAX_ATTEMPTS="${CLIPS_CRON_MAX_ATTEMPTS:-4}"
START_HOUR=8
PYTHON="${CLIPS_CRON_PYTHON:-$REPO/.venv/Scripts/python.exe}"

day="$(TZ=America/Sao_Paulo date +%F)"
hour="$(TZ=America/Sao_Paulo date +%H)"

[ "$((10#$hour))" -lt "$START_HOUR" ] && exit 0
[ -e "$STATE/$day.done" ] && exit 0

mkdir -p "$STATE/logs"
exec 9>"$STATE/lock"
flock -n 9 || exit 0  # a run is in progress

# Re-read under the lock: the run that held it may have just finished the day.
[ -e "$STATE/$day.done" ] && exit 0
attempts="$(cat "$STATE/$day.attempts" 2>/dev/null || echo 0)"
[ "$attempts" -ge "$MAX_ATTEMPTS" ] && exit 0
attempt=$((attempts + 1))
echo "$attempt" > "$STATE/$day.attempts"

log="$STATE/logs/$day.log"
cd "$REPO" || exit 1
echo "=== $(date -Is) attempt $attempt/$MAX_ATTEMPTS for $day" >> "$log"
"$PYTHON" -m app.clips daily --date "$day" "$@" >> "$log" 2>&1
rc=$?
echo "=== $(date -Is) exit $rc" >> "$log"
[ "$rc" -eq 0 ] && touch "$STATE/$day.done"
exit "$rc"
