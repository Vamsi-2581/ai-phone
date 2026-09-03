#!/usr/bin/env bash
# scripts/gate_run.sh — place one LAN call per utterance file against a running daemon, then report.
# Usage: AI_PHONE_LOG_DIR=~/Library/Logs/ai-phone ./scripts/gate_run.sh
set -euo pipefail
LOG="${AI_PHONE_LOG_DIR:-$HOME/Library/Logs/ai-phone}"
rm -f "$LOG/turns.jsonl"
for f in scripts/utt/*.wav; do
  # keep pjsua's stdin open (it quits on EOF); it hangs up itself after the file plays
  printf '' | { sleep 18; echo q; } | bash scripts/pjsua_peer.sh "$f" /tmp/ai-phone-rec.wav 18 >/dev/null 2>&1 || true
  sleep 1
done
uv run python scripts/latency_report.py "$LOG/turns.jsonl" --last 20 --expect 20
