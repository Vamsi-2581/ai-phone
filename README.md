# ai-phone

A phone line answered by an AI that runs on your own machine. Give an agent a real number (bought with
your own Telnyx or Twilio key, once, from the command line), divert your own phone to it, or reach it at a
SIP address; speech-to-text, the language model and text-to-speech run locally by default, and during a
call the daemon talks to nothing but SIP/RTP (and a brain endpoint only if you configured one).

**Status: Milestone 1 (latency proof) passed; Milestone 2 (lines) code complete, its live gate pending.**
English only, one call at a time. See `PRD.md` for scope and `architecture-essentials.md` for how it works.

## Milestone 1 numbers (Apple M5 Pro, 24 GB, 4B tier, 20 LAN calls over 8 kHz PCMU)

| Measure | p50 | p95 |
|---|---|---|
| First audible response | **874 ms** | 1438 ms |
| Barge-in stop after the caller starts talking (5 interruptions) | 219 ms | 259 ms |
| Endpoint wait / STT / LLM to first clause / TTS first chunk / egress | 297 / 96 / 312 / 164 / 9 ms | |

"First audible" is measured from the last voice-active 20 ms frame of the caller to the first
non-silent RTP frame handed to the SIP stack; it includes the 300 ms endpoint wait. Add the phone's
own jitter buffer (60–100 ms) for mouth-to-ear. Measured while the host was also swapping about
10 GB of other applications; a quiet machine does better. Callers who talk over the greeting are
heard in full.

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

Packaging (`uv tool install`, Homebrew tap, Docker) is Milestone 3. Real numbers, diverts and SIP addresses are Milestone 2, below.

## Three ways to call it (Milestone 2)

```bash
# 1. Its own number (a Telnyx or Twilio account and API key; about £1/month for the number)
export TELNYX_API_KEY=…
ai-phone lines add telnyx --key-env TELNYX_API_KEY --country GB --area 020
ai-phone run

# 2. Your own number, diverted when you don't answer
ai-phone lines forward telnyx --when no-answer --from +447700900123
#   … then dial the printed **61* code on your phone (##002# cancels every divert)

# 3. A SIP address, no carrier (agents calling agents)
ai-phone lines add sip --id agents --user agents --host home.example.org --trusted 203.0.113.7
ai-phone call sip:agents@friend.example.org --line agents

# Outbound from the agent's number, or from your mobile once it is verified
ai-phone call +447700900123
ai-phone lines verify-caller-id telnyx +447700900123 --key-env TELNYX_API_KEY
ai-phone status

# Any brain: LM Studio, llama.cpp, or your own agent behind an OpenAI-compatible endpoint
#   brain: {kind: openai_compatible, base_url: "http://127.0.0.1:1234/v1", model: "…"}
```

During a call the daemon opens no network connection except SIP/RTP to the peer and, only if you configured one, HTTP(S) to that brain endpoint. Buying a number talks to the carrier once, from the `lines` command, and the daemon never loads the carrier code. Provisioning is journaled: `ai-phone lines resume` picks up where a crash left off, `ai-phone lines remove` deletes what the carrier created and, if a deletion fails, lists exactly what is left for you to remove in the portal. Configuration applies at restart. The NAT and transport checks run before calling it done are in [docs/nat-checklist.md](docs/nat-checklist.md); the 24 h registration soak is `scripts/soak.sh`.

## License

AGPL-3.0-or-later. `native/pjsua2/` carries PJSIP's GPLv2-or-later interface files; see its README.
