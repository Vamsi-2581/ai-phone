# ai-phone

A self-hosted phone line answered by an AI that runs entirely on your own machine. Point any SIP
trunk or PBX at it; speech-to-text, the language model and text-to-speech all run locally. No cloud
AI, no API keys. The only external thing is SIP.

**Status: Milestone 1 (latency proof) complete.** LAN calls only; no real trunk yet, English only,
one call at a time. See `PRD.md` for scope and `architecture-essentials.md` for how it works.

## Milestone 1 numbers (Apple M5 Pro, 24 GB, 4B tier, 20 LAN calls over 8 kHz PCMU)

| Measure | p50 | p95 |
|---|---|---|
| First audible response | **977 ms** | 1577 ms |
| Barge-in stop after the caller starts talking (5 interruptions) | 221 ms | 721 ms |
| Endpoint wait / STT / LLM to first clause / TTS first chunk / egress | 299 / 94 / 259 / 199 / 12 ms | |

"First audible" is measured from the last voice-active 20 ms frame of the caller to the first
non-silent RTP frame handed to the SIP stack; it includes the 300 ms endpoint wait. Add the phone's
own jitter buffer (60–100 ms) for mouth-to-ear. Measured while the host was also swapping 8 GB of
other applications; a quiet machine does better.

## Try it (macOS, Apple Silicon)

```bash
brew install swig pjproject && ./native/pjsua2/build.sh
mkdir -p src/ai_phone/vendor && cp native/pjsua2/build/pjsua2.py native/pjsua2/build/_pjsua2*.so src/ai_phone/vendor/
uv sync
# weights (one-off, ~4.3 GB): Qwen3.5-4B-MLX-4bit, parakeet-tdt-0.6b-v2, Kokoro-82M-bf16, plus voices from prince-canuma/Kokoro-82M
mkdir -p ~/.ai-phone && cp config.example.yaml ~/.ai-phone/config.yaml && chmod 600 ~/.ai-phone/config.yaml
HF_HUB_OFFLINE=1 uv run ai-phone run --lan
```

Then call `sip:ai@<your-mac-ip>` from any SIP softphone on the same network, or run the test
caller: `scripts/pjsua_peer.sh scripts/utt/00.wav /tmp/rec.wav 18`.

Packaging (`uv tool install`, Homebrew tap, Docker) and real trunk registration are Milestones 2–3.

## License

AGPL-3.0-or-later. `native/pjsua2/` carries PJSIP's GPLv2-or-later interface files; see its README.
