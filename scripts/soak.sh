#!/usr/bin/env bash
# scripts/soak.sh <hours> [extra ai-phone run args]
# Runs the daemon for N hours with its log in a file, sends it SIGINT, appends "soak end" and runs the report.
set -euo pipefail
HOURS="${1:?hours}"; shift || true
LOG=~/Library/Logs/ai-phone/soak-$(date +%Y%m%d-%H%M).log
mkdir -p "$(dirname "$LOG")"
echo "soak: $HOURS h -> $LOG   (follow it with: tail -f $LOG)"
export AI_PHONE_SOAK=1
uv run ai-phone run "$@" > "$LOG" 2>&1 &
PID=$!                      # the daemon itself (no pipeline, so the PID is the process we signal)
trap 'kill -INT "$PID" 2>/dev/null || true' EXIT
sleep $(( HOURS * 3600 ))
kill -INT "$PID" 2>/dev/null || true
for _ in $(seq 1 30); do    # the daemon stops its workers and PJSIP on SIGINT; give it up to 30 s
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
kill -KILL "$PID" 2>/dev/null || true
trap - EXIT
echo "$(date '+%Y-%m-%d %H:%M:%S'),000 INFO soak end" >> "$LOG"
uv run python scripts/soak_report.py "$LOG" --line "${AI_PHONE_SOAK_LINE:-main}"
