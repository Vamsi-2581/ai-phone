# ai-phone — Architecture essentials

Read this first. If it does not answer your question, the table at the bottom says which section of `architecture.md` does. Decisions here are settled; do not reopen them without the owner. Baseline 2026-09-02.

## What it is

One daemon, one SIP identity, one phone call at a time. It registers to a SIP trunk or PBX, answers and places calls, and runs speech-to-text → language model → text-to-speech entirely on the host. No cloud AI. During a call, no network connections other than SIP signalling and RTP media to the configured peer. Numbers come from lines (see below): a carrier key may be used once at setup to buy one, never on the call path.

## The five invariants (never break these)

1. **During a call, no network except SIP/RTP to the peer.** No model downloads at runtime (`HF_HUB_OFFLINE=1`), no telemetry, no update checks, no external model server. `ai-phone doctor` pre-installs everything, including the spaCy model Kokoro's phonemizer would otherwise fetch from GitHub. Buying a number or sending an SMS may use a carrier API, but only from setup commands, never from the call path (see `architecture.md` §14a).
2. **The PJSIP thread never blocks and never touches models.** Media callbacks only copy 640-byte frames into ring buffers. Model work lives in three child processes.
3. **Everything internal is 16 kHz int16, 20 ms frames (320 samples).** The wire is 8 kHz G.711 (PCMU/PCMA only; all other codecs disabled). PJSIP resamples 8↔16; we resample TTS 24→16.
4. **Every generation has a `gen_id`.** Cancel means: all workers drop that id, the outbound ring is flushed, late results with a stale id are discarded, the partial reply stays in history marked `[interrupted]`.
5. **Latency is measured, not claimed.** t0 = last voice-active chunk; t5 = first non-silent RTP frame handed to PJSIP. Budget 900 ms p50, gate 1000 ms. Every turn writes a JSON record with per-stage times.

## Process and layer map

```
 SIP peer ◀── SIP/RTP ──▶ ┌─ ai-phoned (main process) ─────────────────────────────┐
                          │ L1 Telephony: PJSIP endpoint, account, call, media port │  PJSIP thread
                          │        │ rx_ring / tx_ring (shared memory, 2 s)          │
                          │ L2 Pipeline: asyncio TurnController, VAD, endpointing,   │  main thread
                          │        barge-in, clause chunker, resampler, timers       │
                          │ L4 Control: CLI over Unix socket, config, doctor,        │
                          │        HTTP API (M4), TUI (M4)                           │
                          └───────┬────────────┬────────────┬────────────────────────┘
                     JSON pipe + shm ▼          ▼            ▼
                              stt-worker   llm-worker   tts-worker      (L3, child processes, one model each)
```

L1 knows nothing about models. L3 knows nothing about SIP. L2 owns the conversation.

## One turn, in order

caller audio → `rx_ring` → VAD (Silero, 32 ms chunks) → **speech_end after 300 ms silence (t0)** → STT final (100 ms) → LLM streams tokens (prompt cache hit; 250 ms to first clause, zero slack) → clause chunker (punctuation, or 12 tokens) → TTS first chunk (150 ms) → 24→16 kHz → `tx_ring` → RTP (**t5**). Barge-in: ≥ 250 ms of caller speech above a playback-raised energy floor → cancel, flush, next turn; stop ≤ 300 ms from onset.

## The stack (decided)

| Layer | Choice | Not this |
|---|---|---|
| SIP/RTP | PJSIP 2.17 via pjsua2, extension vendored in our wheel, null audio device, media port at 16 kHz | pyVoIP, Asterisk, LiveKit |
| Pipeline | hand-rolled asyncio | Pipecat (only if it ever deletes more code than it adds) |
| STT (Mac) | mlx-audio parakeet `mlx-community/parakeet-tdt-0.6b-v2` | parakeet-mlx (fallback only) |
| LLM (Mac) | mlx-lm, `mlx-community/Qwen3.5-9B-MLX-4bit` (24 GB+) or `-4B-` (16 GB), `enable_thinking=False`, in the daemon's worker process | Ollama or any model server |
| TTS (Mac) | mlx-audio Kokoro `mlx-community/Kokoro-82M-bf16`, voice `af_heart`, 24 kHz | — |
| VAD | Silero, 16 kHz, in the main process | — |
| Linux | sherpa-onnx or faster-whisper · llama-cpp-python CUDA (wheel index, not PyPI) · kokoro-onnx · silero-vad | — |
| Install | `uv tool install ai-phone` on Mac (uv, not pip); `docker run --network host …:cuda` on Linux | pip, bridge networking |
| License | AGPLv3 (PJSIP is GPLv2-or-later; §13 permits) | MIT/Apache |

## Call rules

Answer with a fixed cached greeting. Second INVITE while busy → 486. INVITE from a disallowed source → 403. Acceptance modes: `registered_trunk` (default), `lan` (RFC 1918 + loopback + link-local; used by `ai-phone run --lan`, the test peer, and CI), `any` (warned). Silence 8 s → "Still there?", 8 s more → BYE. Max call 10 min. RTP silent 10 s → BYE. Single call through launch.

## Where things live

```
src/ai_phone/  telephony/ (endpoint, account, call, media_port, policy, rings)
               pipeline/  (turn, vad, bargein, chunker, audio_out, timing)
               services/  (base, ipc, worker, registry, backends/)
               control/   (socket, doctor, api, tui, service)
native/pjsua2/ SWIG interface + build script (GPL source offer)
scripts/       latency_report.py, pjsua_peer.sh, pf/ ruleset, utterances.txt
packaging/     Homebrew formula, Dockerfile.cuda, launchd plist
config:        ~/.ai-phone/config.yaml (0600) · models: ~/.ai-phone/models · logs: ~/Library/Logs/ai-phone
```

## Lines (Milestone 2)

An agent is reachable through lines: a number bought with your own Twilio/Telnyx key (belongs to the agent; direct dial and outbound), your existing number forwarded to it, a free SIP address for agent-to-agent calls, or a SIP trunk you bring. The daemon sees only SIP either way. Flagship job: the personal receptionist. Full sketch in `architecture.md` §14a.

## Milestone gates

M1 latency proof: p50 < 1.0 s at 8 kHz over the 20-utterance script, else the 4B tier becomes default. M2 real trunk: 24 h registered. M3 packaging: install-to-first-call under 10 min on both platforms. M4 control plane: API drives a call. M5 launch. Nothing is built before its gate.

## Test peer (same box)

```
pjsua --local-port 5062 --null-audio --play-file utt.wav --auto-play --auto-play-hangup sip:ai@127.0.0.1:5060
```

## When to open `architecture.md`

| Question | Section |
|---|---|
| Exact PJSIP config, transports, codecs, registration backoff | §3.2, §3.3 |
| What `onIncomingCall` does, policy evaluation, lockout | §3.4.1 |
| Call and registration state machines | §3.3.1, §3.4.3 |
| Media port callbacks, ring buffer layout, overflow/underflow | §3.5 |
| NAT, STUN, RTP ports, Docker networking | §3.6 |
| Building the SWIG extension on Mac and in CI | §3.7, §10.5 |
| TurnController states, VAD and endpointing parameters | §4.2, §4.3 |
| Clause chunking rules, token caps, thinking off | §4.5 |
| Cancellation semantics, underrun handling | §4.6 |
| Barge-in algorithm and the echo fallback | §4.7 |
| Timers and every edge behaviour | §4.8 |
| Worker interfaces (`STTBackend` etc.) and IPC messages | §5.1, §5.2 |
| Model ids, packages, per-platform backends, memory budget | §5.3–§5.6 |
| Sample rates at every hop | §6 |
| CLI commands, control socket, doctor checks, HTTP API | §7 |
| Full config schema | §8 |
| Turn record fields and latency definitions | §9 |
| Repo layout, wheels, Docker, launchd, CI | §10 |
| Tests at each level | §11 |
| Security model | §12 |
| Failure modes and recovery | §13 |
| Post-launch extension points | §14 |
| Which facts were verified against source | §16 |
