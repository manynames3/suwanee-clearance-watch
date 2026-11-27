#!/bin/sh
# Run one scan, then sleep. Three scans a day by default, the same polite pace
# as the scheduled install. A scan that hits a block records its own cooldown
# and the next scan honours it, so looping is safe.
set -u
INTERVAL_HOURS="${SCAN_INTERVAL_HOURS:-8}"

if [ -z "${STORES:-}" ] && [ ! -f /data/.env ]; then
  echo "No STORES set and no /data/.env. Run: docker compose run --rm scanner hd init --zip YOUR_ZIP --no-schedule"
  exit 1
fi
# Settings reads /data/.env itself (the working directory), so it is not sourced here.

while true; do
  hd run-once || echo "scan reported a problem; will try again after the interval"
  sleep "$(( INTERVAL_HOURS * 3600 ))"
done
