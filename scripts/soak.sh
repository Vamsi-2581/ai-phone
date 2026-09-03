#!/usr/bin/env bash
# scripts/soak.sh <hours> [extra ai-phone run args]
# Runs the daemon for N hours with the registration log to a file, then appends "soak end" and runs the report.
set -euo pipefail
HOURS="${1:?hours}"; shift || true
LOG=~/Library/Logs/ai-phone/soak-$(date +%Y%m%d-%H%M).log
mkdir -p "$(dirname "$LOG")"
echo "soak: $HOURS h -> $LOG"
( uv run ai-phone run "$@" 2>&1 | tee "$LOG" ) &
PID=$!
sleep $(( HOURS * 3600 ))
kill -INT $PID 2>/dev/null || true
wait $PID 2>/dev/null || true
echo "$(date '+%Y-%m-%d %H:%M:%S'),000 INFO soak end" >> "$LOG"
uv run python scripts/soak_report.py "$LOG" --line "${AI_PHONE_SOAK_LINE:-main}"
