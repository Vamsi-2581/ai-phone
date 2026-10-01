#!/usr/bin/env bash
# scripts/pjsua_peer.sh <play.wav> <rec.wav> [duration_s] [target]
# Places a PCMU call from this box to the daemon and records what it hears.
# pjsua is interactive and quits the moment its stdin reaches EOF, so from a script (no terminal) give it a stdin that
# stays open for the whole call, as scripts/gate_run.sh does:
#   { sleep 18; echo q; } | ./scripts/pjsua_peer.sh scripts/utt/00.wav /tmp/rec.wav 18
set -euo pipefail
PLAY="$1"; REC="$2"; DUR="${3:-6}"; TARGET="${4:-sip:ai@127.0.0.1:${AI_PHONE_TEST_SIP_PORT:-5060}}"
exec pjsua --local-port "${PJSUA_PEER_PORT:-$(( ${AI_PHONE_TEST_SIP_PORT:-5060} + 2 ))}" --null-audio --auto-answer 200 \
  --play-file "$PLAY" --auto-play --auto-play-hangup \
  --rec-file "$REC" --auto-rec \
  --dis-codec speex --dis-codec ilbc --dis-codec gsm --dis-codec g722 --dis-codec opus \
  --duration "$DUR" --app-log-level 2 --log-level 2 "$TARGET"
