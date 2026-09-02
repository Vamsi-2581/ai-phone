#!/usr/bin/env bash
# scripts/pjsua_peer.sh <play.wav> <rec.wav> [duration_s] [target]
# Places a PCMU call from this box to the daemon and records what it hears.
set -euo pipefail
PLAY="$1"; REC="$2"; DUR="${3:-6}"; TARGET="${4:-sip:ai@127.0.0.1:5060}"
exec pjsua --local-port 5062 --null-audio --auto-answer 200 \
  --play-file "$PLAY" --auto-play --auto-play-hangup \
  --rec-file "$REC" --auto-rec \
  --dis-codec speex --dis-codec ilbc --dis-codec gsm --dis-codec g722 --dis-codec opus \
  --duration "$DUR" --app-log-level 2 --log-level 2 "$TARGET"
