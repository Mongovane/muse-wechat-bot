#!/usr/bin/env bash
# watchdog: fast-fail for hook workers that never start.
# If a claimed message's worker hasn't touched its .alive marker within 75s,
# the worker never started (runtime spawn failure) -> requeue to inbox/ for
# another wake. Caps retries at 2, then leaves it for the cron fallback.
# A worker that DID start (alive marker exists) is left alone even if slow.
set -uo pipefail

BOT_DIR="${1:?bot dir required}"
id="${2:?msg id required}"

sleep 75

proc="$BOT_DIR/processing/$id.json"
alive="$BOT_DIR/processing/$id.alive"

# Already finished or picked up elsewhere.
[ -f "$proc" ] || exit 0
# Worker started -> don't touch it, even if slow. Cron is the backstop.
[ -f "$alive" ] && exit 0

# Worker never started. Check retry cap.
retries="$(jq -r '.watchdog_retries // 0' "$proc" 2>/dev/null || echo 0)"
# jq may print empty on parse failure; default to 0
case "$retries" in ''|*[!0-9]*) retries=0 ;; esac
if [ "$retries" -ge 2 ]; then
  exit 0
fi

# Mark: skip the ack on re-wake (user already got one at 30s), bump counter.
tmp="$proc.tmp"
if jq --argjson r $((retries + 1)) \
    '.watchdog_retries = $r | .watchdog_noack = true' \
    "$proc" > "$tmp" 2>/dev/null; then
  mv "$tmp" "$proc"
fi
# Requeue for the next hook poll (5s). Atomic mv; failure means someone else got it.
mv "$proc" "$BOT_DIR/inbox/$id.json" 2>/dev/null || true
rm -f "$alive"
