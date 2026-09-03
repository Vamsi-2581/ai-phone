#!/usr/bin/env bash
# Wait until the host is quiet (free memory high, swap low), then run the gate once and write the report.
# Usage: scripts/quiet_gate.sh <report-file> [min_unused_mb=3000] [max_swap_used_mb=4000]
set -u
OUT="$1"; MIN_UNUSED="${2:-3000}"; MAX_SWAP="${3:-4000}"
cd "$(dirname "$0")/.."
while true; do
  unused=$(top -l 1 2>/dev/null | awk '/^PhysMem/ {for(i=1;i<=NF;i++) if($i=="unused.") {gsub(/M/,"",$(i-1)); print $(i-1)}}')
  swap=$(sysctl -n vm.swapusage | awk '{gsub(/M/,"",$6); print int($6)}')
  case "$unused" in *G) unused=$(( ${unused%G} * 1024 ));; esac
  if [ "${unused:-0}" -ge "$MIN_UNUSED" ] && [ "${swap:-99999}" -le "$MAX_SWAP" ]; then break; fi
  echo "waiting: unused=${unused:-?}MB swap_used=${swap:-?}MB (need unused>=${MIN_UNUSED}, swap<=${MAX_SWAP})"
  sleep 120
done
echo "quiet: unused=${unused}MB swap_used=${swap}MB — starting gate at $(date)"
HF_HUB_OFFLINE=1 nohup uv run ai-phone run --config ~/.ai-phone/config.yaml --lan --tier t4 > /tmp/ai-phone-quiet-daemon.log 2>&1 &
DPID=$!; for i in $(seq 1 120); do grep -q "^ready" /tmp/ai-phone-quiet-daemon.log && break; sleep 1; done
AI_PHONE_LOG_DIR=~/Library/Logs/ai-phone ./scripts/gate_run.sh > "$OUT" 2>&1; rc=$?
cp ~/Library/Logs/ai-phone/turns.jsonl "${OUT%.txt}-turns.jsonl" 2>/dev/null; rm -f ~/Library/Logs/ai-phone/turns.jsonl
for f in scripts/bargein/*.wav; do printf '' | { sleep 22; echo q; } | bash scripts/pjsua_peer.sh "$f" /tmp/ai-phone-rec.wav 22 >/dev/null 2>&1 || true; sleep 1; done
uv run python scripts/latency_report.py ~/Library/Logs/ai-phone/turns.jsonl >> "$OUT" 2>&1; bargein_rc=$?
kill $DPID 2>/dev/null
echo "GATE_DONE rc=$rc bargein_rc=$bargein_rc"; grep -E "first_audible|GATE|turns:" "$OUT" || true
[ "$rc" -eq 0 ] && [ "$bargein_rc" -eq 0 ]
