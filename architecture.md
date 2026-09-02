# ai-phone — Architecture (full)

| | |
|---|---|
| **Status** | v1.0 baseline, 2026-09-02. Companion to `PRD.md` and `architecture-essentials.md`. |
| **Reading order** | `architecture-essentials.md` first. Come here when it does not answer the question. |
| **Precedence** | Owner's decisions → this doc → PRD. Where this doc and the PRD disagree on an architectural detail, this doc wins; where they disagree on a *decision*, the decision log in §15 wins. |
| **Notation** | **[D]** decided; do not change without the owner. **[P]** proposed default; change if Milestone 1 measurement says so, and record the change in §15. **[V]** verified against library source, PyPI, Hugging Face, or Homebrew on 2026-09-02 by an independent reviewer. |

---

## 1. Purpose and shape

ai-phone is a single daemon that owns one SIP identity and one phone conversation at a time. It registers to a SIP trunk or PBX, answers and places calls, and runs a streaming speech-to-text → language model → text-to-speech pipeline entirely on the host machine. It makes no network connections other than SIP signalling and RTP media to the configured peer. **[D]**

Shape in one line: **a telephony layer that knows nothing about models, a model layer that knows nothing about telephony, and an asyncio pipeline between them that owns the conversation.**

Non-goals that shape the architecture (from the PRD): single-call, English-only, no web UI, no cloud fallback, no external model server, no SMS through launch.

### 1.1 Context diagram

```
                    PSTN / other SIP endpoints
                              │
                       SIP trunk / PBX / LAN softphone / pjsua test peer
                              │  SIP over UDP/TCP/TLS (signalling)
                              │  RTP G.711 PCMU/PCMA, 20 ms frames (media), SRTP when offered
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  ai-phone daemon (one host, one SIP identity, one call at a time)         │
 │                                                                           │
 │   Layer 1 Telephony ── Layer 2 Pipeline ── Layer 3 Model services         │
 │        (PJSIP)         (asyncio, main)     (3 child processes)            │
 │                              │                                            │
 │                       Layer 4 Control plane                               │
 │              CLI · YAML config · doctor · HTTP API (M4) · TUI (M4)        │
 └──────────────────────────────────────────────────────────────────────────┘
                              ▲
                              │  localhost only
                  operator (terminal) · Home Assistant integration (post-launch)
```

## 2. Runtime topology

### 2.1 Processes **[D]**

| Process | Role | Why a separate process |
|---|---|---|
| `ai-phoned` (main) | PJSIP endpoint, asyncio event loop, pipeline, control plane, supervisor of workers | Owns the 20 ms media clock; must never block on model compute |
| `stt-worker` | Speech-to-text backend (parakeet) | GIL isolation; a slow decode cannot stall the media thread |
| `llm-worker` | Language model backend (mlx-lm / llama-cpp-python) | Largest memory; isolated crash domain |
| `tts-worker` | Text-to-speech backend (Kokoro) | Same |

Workers are children of the main process (`multiprocessing`, spawn start method on both platforms for consistency). There is no external model server: Ollama, vLLM, and similar are excluded by decision **[D]** (they conflict with one-command install and would appear in the isolation firewall log).

VAD runs **in the main process** **[P]**: Silero is small (~2 MB) and its per-chunk cost (~1 ms for 32 ms of audio on Apple Silicon) is far below the frame budget; keeping it in-process removes an IPC hop from the endpointing path, which is 300 of the 900 ms budget.

### 2.2 Threads in the main process

| Thread | Owner | Does | Must not |
|---|---|---|---|
| PJSIP worker thread(s) | PJSIP (`uaConfig.threadCnt`, default 1) | SIP signalling, RTP send/receive, conference bridge ticks, calls our `AudioMediaPort` callbacks every 20 ms | Block; touch Python objects outside the ring buffers and an event queue; touch models |
| asyncio loop thread (main thread) | us | Everything else: pipeline, IPC with workers, timers, control plane | Call into pjsua2 without `libRegisterThread` having been called for that thread (main thread is registered by `libCreate`) |

PJSIP → Python callbacks are SWIG *director* calls; the extension **must be built with `swig -threads`** so the callback re-acquires the GIL correctly **[V]** (pjproject's own `swig/python/Makefile` sets `-threads`). Any *other* Python thread that calls into pjsua2 must call `Endpoint.libRegisterThread(name)` first **[V]**; in this design no thread other than the main thread does.

### 2.3 Startup sequence **[D]**

1. Parse and validate config (§8). Refuse to start on any error; print the field and the fix.
2. Verify model weights and the spaCy G2P model are present on disk (registry manifest, §7.5). Refuse to start if anything is missing; `doctor` explains how to fetch. No network here or later.
3. Set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` in the environment of every process.
4. Spawn the three workers. Each loads its model and replies `ready` with warm-up timings. Warm-up = one real inference each (a 1 s silent-plus-tone buffer for STT, a 16-token generation for the LLM with the system prompt as prefix so the prompt cache is primed, one short sentence for TTS).
5. Create the PJSIP endpoint: `libCreate` → `libInit(EpConfig)` → transports → `audDevManager().setNullDev()` → codec priorities → `libStart`.
6. Create the account and register (unless `accept_invites_from: lan` with no trunk configured, in which case run registrar-less).
7. Start the control plane (CLI is already running; HTTP API in M4 binds `127.0.0.1` only by default).
8. Log `ready` with: SIP registration state, resident memory per process, warm-up latencies, invite-acceptance mode.

Total cold start target: under 15 s on the 9B tier on an M5 Pro **[P]**.

### 2.4 Shutdown sequence

`SIGTERM` or `ai-phone stop` → if a call is active, play nothing, send BYE, wait ≤ 2 s for the call to release → unregister (REGISTER with Expires 0) → `libDestroy` → send `shutdown` to workers, join with 5 s timeout, then kill → exit 0. Crash of any worker while idle → restart it (max 3 restarts in 10 minutes, then exit non-zero so the service manager notices). Crash mid-call → drop the current turn, say the fixed apology sentence from a cached TTS buffer, restart the worker, continue the call.

## 3. Layer 1: Telephony (PJSIP via pjsua2)

### 3.1 Why PJSIP **[D]**

The only mature, maintained, self-contained SIP+RTP stack with a Python binding. Alternatives rejected: pyVoIP (stale since 2025-03, LAN-toy quality), Asterisk/FreeSWITCH (a second server to install), LiveKit (framework, WebRTC-first). pjproject 2.17, released 2026-04-22 **[V]**.

### 3.2 Endpoint configuration

```python
ep = pj.Endpoint(); ep.libCreate()
cfg = pj.EpConfig()
cfg.uaConfig.threadCnt = 1                      # [P] one PJSIP worker thread
cfg.uaConfig.mainThreadOnly = False
cfg.uaConfig.stunServer = [...]                 # from config.nat.stun_servers, may be empty
cfg.medConfig.clockRate = 16000                 # [V] PJSUA default is already 16000
cfg.medConfig.sndClockRate = 16000
cfg.medConfig.audioFramePtime = 20              # [V] default 20 ms
cfg.medConfig.ecTailLen = 0                     # no echo canceller: null device, no acoustic path
cfg.medConfig.noVad = True                      # PJSIP's own silence suppression off; we do VAD
cfg.medConfig.jbInit / jbMinPre / jbMaxPre      # [P] defaults; revisit if RTP jitter shows in logs
cfg.logConfig.level = config.log.pjsip_level    # default 3
ep.libInit(cfg)
```

Transports **[D]**: UDP on `config.sip.bind.port` (default 5060) bound to `config.sip.bind.addr` (default `0.0.0.0`), plus TCP on the same port, plus TLS on `port+1` if `config.sip.tls` is set. `TransportConfig.publicAddress` = `config.sip.public_addr` when set (behind NAT without STUN).

Null audio device **[D][V]**: `ep.audDevManager().setNullDev()` immediately after `libInit`. Without it a headless process has no conference-bridge clock and the media port never ticks; on macOS 26 a default CoreAudio device would also trigger a microphone permission prompt. `setNullDev2(avoidClockGap=True)` **[V]** is the knob if the null device shows clock gaps under load.

Codecs **[D]**: after `libStart`, enumerate with `ep.codecEnum2()` and set priority 255 for `PCMU/8000` and `PCMA/8000`, 0 for everything else (Opus, G.722, speex, iLBC, GSM). Both directions of the SDP offer then carry only G.711, so every measured number is over 8 kHz phone audio (premise 3).

### 3.3 Account and registration

```python
acc_cfg = pj.AccountConfig()
acc_cfg.idUri = f"sip:{user}@{domain}"
acc_cfg.regConfig.registrarUri = f"sip:{domain}"          # absent in registrar-less LAN mode
acc_cfg.regConfig.timeoutSec = config.sip.register_expiry  # [P] 300
acc_cfg.regConfig.retryIntervalSec = 30                    # [P]; see 3.3.1 for backoff
acc_cfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", user, 0, password))
acc_cfg.sipConfig.proxies = [outbound_proxy] if configured
acc_cfg.natConfig.sipStunUse = acc_cfg.natConfig.mediaStunUse = PJSUA_STUN_USE_DEFAULT if stun else DISABLE
acc_cfg.natConfig.contactRewriteUse = 1                    # learn public contact from REGISTER response
acc_cfg.natConfig.udpKaIntervalSec = config.nat.keepalive_sec   # [P] 25
acc_cfg.mediaConfig.transportConfig.port = config.rtp.port_min
acc_cfg.mediaConfig.transportConfig.portRange = config.rtp.port_max - config.rtp.port_min
acc_cfg.mediaConfig.srtpUse = PJMEDIA_SRTP_OPTIONAL          # accept SRTP when offered [D]
acc_cfg.mediaConfig.srtpSecureSignaling = 0
```

#### 3.3.1 Registration state machine **[D]**

```
 UNREGISTERED ──REGISTER──▶ REGISTERING ──200──▶ REGISTERED ──expiry/2──▶ (refresh) ──▶ REGISTERED
       ▲                        │ 401/407 (challenge, automatic)                │ timeout / 5xx / transport error
       │                        │ 403 / 404 (fatal: log, stop retrying)         ▼
       └────────── backoff ◀────┴────────────────────────────────── DEGRADED ──retry──▶ REGISTERING
```

Backoff on non-fatal failure: 5 s, 10 s, 20 s, 40 s, 60 s, then every 60 s, with ±20 % jitter **[P]**. The `onRegState` callback posts `RegEvent(state, code, reason)` to the asyncio queue; the pipeline never reads PJSIP state directly. `doctor` and the M4 API surface the current state. Success criterion: registered 24 h unattended, which this machine satisfies by construction if refresh runs at `expiry/2`.

### 3.4 Call handling

#### 3.4.1 Inbound policy **[D]**

`onIncomingCall(prm)` runs on the PJSIP thread. It must decide fast and without models:

1. Read the source address from `prm.rdata.srcAddress` (`"ip:port"`).
2. Evaluate `accept_invites_from`:
   - `registered_trunk`: accept only if the request arrived via the registered account's transport **and** the source IP matches the registrar/proxy address learned at registration (or an explicit `sip.trusted_peers` list). Otherwise reply **403 Forbidden**.
   - `lan`: accept if source IP ∈ RFC 1918 ∪ `127.0.0.0/8` ∪ `::1` ∪ `fe80::/10` **[D]** (loopback is included so the `pjsua` test peer and CI work). Otherwise 403.
   - `any`: accept from anywhere (documented as unsafe; `doctor` warns).
3. If a call is already active or ending: reply **486 Busy Here**.
4. Optional caller allowlist (M4): if configured and the From user is not listed, 403.
5. Otherwise: create the `Call` object, answer **180 Ringing** immediately, then post `IncomingCall(call_id, from, source)` to the asyncio queue. The pipeline answers **200 OK** when it is ready to speak the greeting (normally within one loop iteration; the 180 covers the gap).

Per-IP auth-failure lockout (M2): count 401/403 responses per source IP over a sliding 10 min window; after 5, drop SIP messages from that IP for 15 min **[P]**. Implemented in the same callback using a small in-memory table; persisted nowhere.

#### 3.4.2 Outbound

`Call.makeCall(uri, CallOpParam)` from the asyncio thread (the main thread is registered). Triggered by the M2 CLI (`ai-phone call <number>`, which talks to the daemon over the control socket, §8.4) and by the M4 HTTP API. Dial string → `sip:<number>@<domain>`; E.164 normalisation is the caller's job, ai-phone only strips whitespace. Outbound calls use the same pipeline; the greeting is replaced by `outbound.opening_line` from config or the API request.

#### 3.4.3 Call state machine **[D]**

```
 IDLE ──INVITE accepted / makeCall──▶ RINGING ──200 OK + media active──▶ ACTIVE ──BYE (either side) / timer──▶ ENDING ──released──▶ IDLE
                                        │ CANCEL / 4xx-6xx                                                        ▲
                                        └───────────────────────────────────────────────────────────────────────────┘
```

`onCallState` and `onCallMediaState` post `CallEvent`s; the pipeline's `TurnController` (§4.2) is created on `ACTIVE` and destroyed on `IDLE`. State lives in exactly one place: `telephony/call.py`. Timers owned by the pipeline, not PJSIP: silence prompt (8 s), silence hangup (8 s more), max duration warning (9:30), max duration hangup (10:00), RTP timeout (10 s without a received frame → BYE).

Second INVITE while not `IDLE` → 486 (3.4.1 step 3). BYE mid-TTS → `ENDING`, pipeline cancels every in-flight generation (§4.6), flushes the outbound ring, logs the partial turn.

### 3.5 Media port **[D]**

`onCallMediaState`: when the call's audio media becomes active, get it with `call.getAudioMedia(idx)` and connect both directions to our port: `call_media.startTransmit(port)` (caller → us) and `port.startTransmit(call_media)` (us → caller).

Our port subclasses `pj.AudioMediaPort` **[V]** and is created with:

```python
fmt = pj.MediaFormatAudio(); fmt.type = PJMEDIA_TYPE_AUDIO
fmt.clockRate = 16000; fmt.channelCount = 1; fmt.bitsPerSample = 16; fmt.frameTimeUsec = 20000
port.createPort("ai-phone", fmt)
```

Declaring 16 kHz costs nothing: the PJSUA conference bridge already runs at 16 kHz **[V]**, so the only resampler PJSIP inserts is on the 8 kHz G.711 stream port, which exists regardless. Every frame we see is **320 samples of int16 at 16 kHz = 640 bytes per 20 ms**.

Callbacks (PJSIP thread; keep them trivial):

```python
def onFrameReceived(self, frame):   # caller audio, 640 bytes
    self.rx_ring.push(bytes(frame.buf))       # never blocks; overwrites oldest on overflow and increments a counter
    self.last_rx_ns = monotonic_ns()          # RTP-timeout watchdog reads this
def onFrameRequested(self, frame):  # audio to caller, must fill 640 bytes
    buf = self.tx_ring.pop() or SILENCE_640   # underrun → silence (comfort noise later, [P])
    frame.buf = pj.ByteVector(buf); frame.size = 640; frame.type = PJMEDIA_FRAME_TYPE_AUDIO
```

Ring buffers: two single-producer/single-consumer rings of 100 frames (2 s) backed by `multiprocessing.shared_memory` **[D]** so the same memory can later be handed to a worker without a copy; head/tail indices are 64-bit atomics via `ctypes` in the same block. Overwrite-on-overflow on RX (a stalled consumer must not stall PJSIP); silence-on-underflow on TX. Both counters are exported to the latency record (§9).

DTMF: `onDtmfDigit` → `DtmfEvent(digit)` to the queue. Unused through launch except by `doctor`'s codec check.

### 3.6 Address and NAT

| Config | PJSIP field | Notes |
|---|---|---|
| `sip.bind.addr` / `.port` | `TransportConfig.boundAddress` / `.port` | default `0.0.0.0:5060` |
| `sip.public_addr` | `TransportConfig.publicAddress` | when behind NAT without STUN |
| `nat.stun_servers[]` | `uaConfig.stunServer` | any STUN server the user names; none by default (a STUN server *is* a network connection outside SIP/RTP, so `doctor` flags it against the isolation criterion) |
| `nat.keepalive_sec` | `natConfig.udpKaIntervalSec` | 25 default |
| `rtp.port_min` / `port_max` | `mediaConfig.transportConfig.port/portRange` | default 10000–10100; must be reachable; Docker requires `--network host` because bridge networking advertises the container IP in SDP **[V]** |

### 3.7 Build and packaging of the extension

- macOS dev **[V]**: `brew install swig pjproject` gives the 2.17 bottle (static `.a` libs, headers, `libpjproject.pc`, `bin/pjsua`; **no `.i` files**). Fetch the 2.17 source tarball for `pjsip-apps/src/swig/pjsua2.i` and `symbols.i` (or use the tree's `pjsip-apps/src/swig/python/setup.py`). Build: `swig -c++ -python -threads -w312 -I$(brew --prefix pjproject)/include pjsua2.i`, then compile and link with `pkg-config --static --cflags --libs libpjproject` (pulls openssl@3 and the Apple frameworks the bottle was built with, including AVFoundation/VideoToolbox because the bottle has video enabled).
- Linux CI **[V]**: build pjproject 2.17 from source with `--enable-shared` or `CFLAGS=-fPIC`; the static default is not position-independent and the extension will not link.
- The compiled module ships **inside the `ai-phone` platform wheel** as `ai_phone/_pjsua2.so` **[D]** (PyPI name `pjsua2` is taken by a stale 2023 sdist **[V]**). Wheel matrix: cp311–cp313 × macos-arm64 × manylinux x86_64; `requires-python >=3.11,<3.14`.
- Licensing **[D][V]**: PJSIP is GPLv2-or-later; ai-phone is AGPLv3 (GPLv3 §13). The extension's corresponding source (interface files, build script, pjproject version) is published as an sdist beside the wheel (GPLv3 §6(d)).

## 4. Layer 2: Pipeline (asyncio, main process)

### 4.1 Overview of one turn

```
  caller RTP ──▶ rx_ring ──▶ [drain task, 20 ms] ──▶ VAD (32 ms chunks) ──▶ endpointer
                                     │                                        │ speech_end (t0)
                                     └──▶ STT partials (display only)         ▼
                                                                   STT final (t1) ──▶ LLM stream (t2 first token)
                                                                                          │
                                                                        clause chunker (t3 first clause) ──▶ TTS stream (t4 first chunk)
                                                                                          │
                                                       24→16 kHz resample ──▶ tx_ring ──▶ PJSIP ──▶ RTP (t5 first non-silent packet)
```

Timestamps t0…t5 are the latency record (§9). Budget: t5 − t0 ≤ 900 ms p50, gate 1000 ms.

### 4.2 TurnController state machine **[D]**

```
            greeting done
 GREETING ───────────────▶ LISTENING ──speech_end──▶ THINKING ──first TTS chunk──▶ SPEAKING ──tts done──▶ LISTENING
                               ▲                        │                              │
                               │                        │ barge-in (rare: caller talks  │ barge-in
                               │                        │ during the 250 ms after end)  ▼
                               └──────────────── INTERRUPTED (cancel gen, flush tx, keep partial) ──┘
 any state ──BYE / timers──▶ ENDING
```

One `TurnController` per call. Every generation carries a monotonically increasing `gen_id`; any worker result whose `gen_id` is not current is dropped on receipt (§4.6).

### 4.3 VAD and endpointing

- VAD **[D]**: Silero, 16 kHz, 512-sample chunks (32 ms). The drain task accumulates 20 ms frames into 32 ms chunks (LCM 160 ms; a small carry buffer). Speech probability threshold 0.5 **[P]**; `speech_start` after 2 consecutive positive chunks (64 ms); `speech_end` after `endpoint_silence_ms` (300 default, range 250–350 **[D]**) of consecutive negative chunks.
- **t0 is the timestamp of the last VAD-positive chunk**, not the moment the endpointer fires **[D]**. The 300 ms of silence is inside the budget, not excluded from it.
- Utterance buffer: from `speech_start − 200 ms` (pre-roll, ring already has it) to `speech_end`. Sent to the STT worker as it accumulates (every 320 ms **[P]**) so the final decode on `speech_end` only covers the tail.
- Noise floor: exponential average of chunk RMS during non-speech, used by barge-in.

### 4.4 STT

- Partials: the worker returns partial text for each pushed chunk; the pipeline shows them in the terminal only. **Partials never reach the LLM** **[D]**.
- Final: on `speech_end`, send `Finalize(gen_id)`; the worker decodes the remaining audio and returns `Final(gen_id, text, timings)`. Budget 100 ms.
- Empty final (noise, cough): return to LISTENING silently; do not count as a turn.

### 4.5 LLM and clause chunking

- Messages: `[system, ...history(≤ 6 turns), user(final_text)]` **[D]**. System prompt ≤ 60 tokens, fixed at start, includes hostname and chip (so "which machine are you running on" is answerable), and is a **stable prefix** so the worker's prompt cache hits every turn.
- Worker streams tokens. The clause chunker emits a TTS unit at the first of: sentence punctuation (`. ! ?`), clause punctuation (`, ; :`) once ≥ 6 tokens are pending, or 12 tokens with no punctuation **[D]**. Subsequent units are sentence-sized. Each unit is sent to TTS immediately; the LLM keeps streaming.
- Caps: `max_tokens` 120 per turn **[P]** (phone answers are short); stop sequences from the chat template.
- Thinking is disabled through the chat template: `tokenizer.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)` **[V]**.

### 4.6 TTS, output, and cancellation

- The TTS worker returns 24 kHz float32 chunks per unit **[V]** (Kokoro's native rate). The pipeline resamples to 16 kHz int16 with `soxr` (VHQ) **[P]** and pushes 640-byte frames to `tx_ring`. First frame pushed = t5 candidate; the actual t5 is stamped by the media port when it hands the first non-silent frame to PJSIP (a flag on the ring entry).
- Underrun: if `tx_ring` empties mid-utterance (TTS slower than real time), the port sends silence and increments `tx_underruns`; the latency record shows it. Persistent underrun → `doctor` recommends the lighter tier.
- **Cancellation semantics [D]**: `cancel(gen_id)` is sent to all three workers; they abandon work for that id at their next check (≤ 1 chunk). The pipeline flushes `tx_ring` immediately (the caller stops hearing us within one frame plus the phone's jitter buffer), drops any in-flight chunks whose `gen_id` is stale, and keeps the partial assistant text in history marked `[interrupted]` so the model knows what the caller heard.

### 4.7 Barge-in **[D]**

While `SPEAKING`, the drain task keeps running VAD on caller audio. Trigger = **≥ 250 ms of continuous VAD-positive chunks whose RMS exceeds `noise_floor × k_playback`** (k = 3.0 **[P]**; the floor is raised only while the tx ring is non-empty, which is the speakerphone-leak guard). On trigger: transition to `INTERRUPTED`, cancel, flush, then treat the audio since the first positive chunk as the start of the next utterance. Stop target: ≤ 300 ms from caller speech onset measured at RTP egress (250 detect + ≤ 50 flush). If PSTN hybrid echo causes false triggers on a real trunk (M2), add cross-correlation of the inbound chunk against the last 200 ms of our own output and require correlation < 0.3 **[P]**.

### 4.8 Timers and edge behaviour (owned by the pipeline)

| Event | Behaviour |
|---|---|
| Greeting | Fixed sentence from config, pre-synthesised at startup and cached as 16 kHz frames; plays within one loop iteration of `ACTIVE` |
| Silence ≥ 8 s after our turn | Say the cached "Still there?" once; silence ≥ 8 s more → BYE |
| Max duration | Cached warning at 9:30; BYE at 10:00 |
| RTP timeout | No `onFrameReceived` for 10 s → BYE |
| BYE mid-turn | Cancel all, flush, log turn as partial |
| Empty STT final | Ignore, stay LISTENING |
| Worker crash mid-turn | Cached apology sentence, restart worker, continue |

All cached sentences are synthesised at startup by the TTS worker so these paths never wait on a model.

## 5. Layer 3: Model services

### 5.1 Service interface **[D]**

Engine-agnostic so a later Pipecat adoption (or any other engine) consumes the same workers.

```python
class STTBackend(Protocol):
    def warmup(self) -> Timings
    def push(self, gen_id: int, pcm16k: memoryview) -> str | None   # returns partial text or None
    def finalize(self, gen_id: int) -> tuple[str, Timings]
    def cancel(self, gen_id: int) -> None

class LLMBackend(Protocol):
    def warmup(self, system_prompt: str) -> Timings
    def stream(self, gen_id: int, messages: list[Message], max_tokens: int) -> Iterator[str]  # tokens
    def cancel(self, gen_id: int) -> None

class TTSBackend(Protocol):
    def warmup(self) -> Timings
    def stream(self, gen_id: int, seq: int, text: str) -> Iterator[np.ndarray]   # float32 @ native rate
    def cancel(self, gen_id: int) -> None
    native_rate: int
```

### 5.2 IPC **[D]**

- **Audio** travels through `multiprocessing.shared_memory` rings (same layout as §3.5): main → stt-worker (utterance audio), tts-worker → main (synthesised audio at 24 kHz). No pickling of audio, ever.
- **Control** travels over a `multiprocessing.Pipe` per worker carrying newline-delimited JSON messages (small, human-readable in logs):

| Direction | Message | Fields |
|---|---|---|
| main → stt | `push` | gen_id, ring offset, nbytes |
| main → stt | `finalize` | gen_id |
| stt → main | `partial` / `final` | gen_id, text, timings |
| main → llm | `generate` | gen_id, messages, max_tokens |
| llm → main | `token` / `done` | gen_id, text / usage + timings (prompt_eval_ms, gen_ms, tokens) |
| main → tts | `synth` | gen_id, seq, text |
| tts → main | `chunk` / `done` | gen_id, seq, ring offset, nframes |
| main → any | `cancel` | gen_id |
| any → main | `ready` / `error` / `metrics` | timings / message / resident_mb |

Workers check for `cancel` between chunks/tokens; a cancelled `gen_id` is remembered so late results are discarded on both ends.

### 5.3 macOS backends **[D][V]**

| Role | Package | Model | Notes |
|---|---|---|---|
| STT | `mlx-audio[stt]` parakeet (has streaming) | `mlx-community/parakeet-tdt-0.6b-v2` (English) | v3 is multilingual, post-launch. Standalone `parakeet-mlx` is the fallback only if measurably faster. |
| LLM | `mlx-lm` | `mlx-community/Qwen3.5-9B-MLX-4bit` (default, 24 GB+); `mlx-community/Qwen3.5-4B-MLX-4bit` (16 GB tier) | Both checkpoints include a vision tower; mlx-lm discards it at load (`sanitize()`), so only the text model is resident. No text-only checkpoint exists. |
| TTS | `mlx-audio[tts]` Kokoro | `mlx-community/Kokoro-82M-bf16`, voice `af_heart`, 24 kHz | Needs `misaki[en]` (G2P), which pulls spaCy and PyTorch (~80 packages) and **downloads `en_core_web_sm` from GitHub at first use if absent**. `doctor` pre-installs and asserts it. |
| VAD | `mlx-audio` Silero (or `silero-vad`) | — | 16 kHz, 512-sample chunks |

LLM worker specifics: `mlx_lm.load(model_id)`; `make_prompt_cache(model)` primed with the system prompt at warm-up; `stream_generate(model, tokenizer, prompt, max_tokens=..., prompt_cache=cache)`. Because history is a suffix of a stable prefix, the cache is reused every turn; the worker reports `prompt_eval_ms` and `gen_ms` separately so the LLM budget row (250 ms, zero slack) is diagnosable.

### 5.4 Linux backends **[D]**

| Role | Package | Notes |
|---|---|---|
| STT | `sherpa-onnx` (parakeet or Nemotron via ONNX) or `faster-whisper` | pick by measured latency in M3 |
| LLM | `llama-cpp-python` with CUDA | PyPI has sdist only **[V]**; the Docker image installs from the maintainer's `cu12x` wheel index. GGUF 4-bit Qwen3.5 of the same tier. |
| TTS | `kokoro-onnx` | requires Python < 3.14 |
| VAD | `silero-vad` | — |

### 5.5 Model registry, weights, and verification **[D]**

- Weights live under `$AI_PHONE_HOME/models/` (default `~/.ai-phone/models`; `/var/lib/ai-phone/models` in the container). Fetched only by `ai-phone doctor --fetch` or `ai-phone models pull <tier>`; never at daemon runtime.
- A `manifest.json` per tier lists every expected file with size and SHA-256. `doctor` verifies; the daemon refuses to start on mismatch.
- Tiers: `t9` (9B LLM, ~7 GB resident total) and `t4` (4B LLM, ~4 GB). Selection: explicit `models.tier` in config, else auto by total memory (≥ 24 GB → t9, else t4).
- Environment in every process: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `TOKENIZERS_PARALLELISM=false`.

### 5.6 Memory budget (macOS, t9) **[P]**

| Component | Resident |
|---|---|
| Qwen3.5-9B 4-bit weights + KV for ≤ 2k tokens | ~5.5 GB |
| parakeet-tdt-0.6b-v2 | ~1.2 GB |
| Kokoro-82M + misaki/spaCy/torch | ~0.6 GB |
| PJSIP, rings, Python | ~0.2 GB |
| **Total** | **~7.5 GB** |

`doctor` reports actual per-process RSS after warm-up; the README states the number.

## 6. Audio path, end to end

| Hop | Rate / format | Who converts | Notes |
|---|---|---|---|
| Wire → PJSIP | G.711 μ/A-law, 8 kHz, 20 ms | PJSIP stream port | jitter buffer here |
| PJSIP bridge | 16 kHz int16 | PJSIP resampler (8→16) | bridge default rate **[V]** |
| Bridge → our port | 16 kHz int16, 320 samples/frame | none | `rx_ring` |
| Port → VAD/STT | 16 kHz | none | VAD chunks of 512 samples |
| TTS → pipeline | 24 kHz float32 | none | Kokoro native |
| Pipeline → port | 16 kHz int16 | `soxr` 24→16 | `tx_ring` |
| Port → PJSIP → wire | 8 kHz G.711 | PJSIP resampler (16→8) + codec | |

Two resamples on the outbound path (24→16 in Python, 16→8 in PJSIP) and one inbound (8→16 in PJSIP). The 100 ms "transport" budget row covers all of them plus one 20 ms frame of jitter.

## 7. Layer 4: Control plane

### 7.1 CLI (M2 unless noted)

| Command | Does |
|---|---|
| `ai-phone run [--lan]` | Start the daemon in the foreground. `--lan` overrides `accept_invites_from` to `lan` and skips registration if no trunk is configured (dev and the timed install test) |
| `ai-phone call <number> [--say "…"]` | Ask the running daemon (control socket) to place a call |
| `ai-phone hangup` | End the current call |
| `ai-phone status` | Registration state, current call, last turn latency |
| `ai-phone doctor [--fetch]` | §7.3 |
| `ai-phone models pull <tier>` | Download and verify a tier (the only networked command besides doctor --fetch) |
| `ai-phone install-service` (M3) | Write and load the macOS LaunchAgent (§10.4) |
| `ai-phone latency` (M4) | The TUI latency view |

### 7.2 Control socket **[D]**

A Unix domain socket at `$AI_PHONE_HOME/run/control.sock` (mode 0600) carrying the same JSON-lines protocol as the worker pipes. The CLI is a thin client. The M4 HTTP API is a second client of the same internal commands, so there is exactly one command implementation.

### 7.3 `doctor` checks **[D]**

Config valid · weights present and hashes match (per tier) · spaCy `en_core_web_sm` importable (`spacy.util.is_package`) · `import ai_phone._pjsua2` works · null audio device set · GPU in use (MLX device / CUDA visible) · resident memory after warm-up vs tier expectation · warm-start latency of each worker · SIP registration state and last error · codec list offered (must be PCMU/PCMA only) · invite-acceptance mode with a warning for `any` · STUN configured (warning: outside the isolation criterion) · outbound connectivity check *disabled* (doctor itself must not phone home) · macOS: LaunchAgent loaded; Linux: `--network host` detected. Exit code non-zero on any hard failure.

### 7.4 HTTP API (M4) **[D]**

Bound to `127.0.0.1:8321` by default; a bearer token from config is required if `api.bind` is not loopback. Endpoints: `POST /calls {to, say?}` → `{call_id}`; `GET /calls/current`; `DELETE /calls/current`; `GET /status`; `GET /turns?last=n` (latency records). No streaming, no webhooks through launch (webhooks are M6). The Home Assistant integration (M6) is a client of this API.

### 7.5 Configuration file

See §8.

## 8. Configuration schema **[D]**

`$AI_PHONE_HOME/config.yaml`, mode 0600 (contains the SIP password). Validated with a typed schema at startup; unknown keys are errors.

```yaml
sip:
  user: "1001"
  domain: "sip.example.net"          # registrar/trunk domain; omit for registrar-less LAN mode
  password: "…"                      # or password_env: AI_PHONE_SIP_PASSWORD
  outbound_proxy: null               # "sip:proxy.example.net:5061;transport=tls"
  register_expiry_sec: 300
  bind: { addr: "0.0.0.0", port: 5060 }
  public_addr: null
  tls: { enabled: false, ca_file: null, verify_server: true }
  trusted_peers: []                  # extra IPs allowed under registered_trunk
  accept_invites_from: registered_trunk   # registered_trunk | lan | any
  allowlist: []                      # M4: From-user allowlist; empty = everyone the mode permits
nat:
  stun_servers: []                   # empty by default; each one is a network peer outside SIP/RTP
  keepalive_sec: 25
rtp:
  port_min: 10000
  port_max: 10100
codecs: [PCMU, PCMA]                 # order = priority; only these are enabled
models:
  tier: auto                         # auto | t9 | t4
  home: "~/.ai-phone/models"
conversation:
  greeting: "Hi, this is the house. What can I do for you?"
  outbound_opening_line: "Hi, this is an automated call from …"
  system_prompt: "…"                 # ≤ 60 tokens; hostname/chip appended automatically
  endpoint_silence_ms: 300
  bargein_min_speech_ms: 250
  bargein_playback_gain: 3.0
  history_turns: 6
  max_tokens: 120
  silence_prompt_sec: 8
  silence_hangup_sec: 8
  max_call_sec: 600
  rtp_timeout_sec: 10
api:                                 # M4
  enabled: false
  bind: "127.0.0.1:8321"
  token_env: AI_PHONE_API_TOKEN
log:
  level: info
  pjsip_level: 3
  dir: "~/Library/Logs/ai-phone"     # /var/log/ai-phone in the container
```

## 9. Latency instrumentation and the turn record **[D]**

Every turn writes one JSON line to `turns.jsonl`:

```json
{"call_id":"…","turn":3,"gen_id":17,
 "t0_speech_end_ns":…, "t1_stt_final_ns":…, "t2_llm_first_token_ns":…, "t3_first_clause_ns":…,
 "t4_tts_first_chunk_ns":…, "t5_rtp_first_nonsilent_ns":…,
 "stt_ms":…, "llm_prompt_eval_ms":…, "llm_first_clause_ms":…, "tts_first_chunk_ms":…, "egress_ms":…,
 "first_audible_ms":…, "bargein":false, "bargein_stop_ms":null,
 "rx_overruns":0, "tx_underruns":0, "tokens":…, "stt_text":"…", "reply_text":"…"}
```

Definitions **[D]**: `first_audible_ms = (t5 − t0)/1e6`. `t0` is the last VAD-positive chunk's timestamp; `t5` is stamped in `onFrameRequested` when the first non-silent frame of a generation is handed to PJSIP. `bargein_stop_ms` is from the first VAD-positive chunk of the interruption to the flush. The mouth-to-ear delta (phone speaker → Mac mic recording) is measured once per platform and stated in the README; it is *not* in the counter.

The TUI (M4) tails this file. The gate check for M1 is a script over 20 turns: `scripts/latency_report.py turns.jsonl --last 20` printing p50/p95 per stage.

## 10. Packaging and deployment

### 10.1 Repository layout **[P]**

```
ai-phone/
  PRD.md · architecture-essentials.md · architecture.md · README.md · LICENSE (AGPLv3)
  pyproject.toml                    # ai-phone; requires-python >=3.11,<3.14
  src/ai_phone/
    cli.py  config.py  daemon.py
    telephony/   endpoint.py account.py call.py media_port.py policy.py rings.py
    pipeline/    turn.py vad.py bargein.py chunker.py audio_out.py timing.py
    services/    base.py ipc.py worker.py registry.py
      backends/  mlx_parakeet.py mlx_lm_backend.py mlx_kokoro.py sherpa_stt.py llamacpp_llm.py kokoro_onnx.py silero.py
    control/     socket.py doctor.py api.py (M4) tui.py (M4) service.py
    _pjsua2.so                      # built extension, vendored into the platform wheel
  native/pjsua2/                    # pjsua2.i, symbols.i (from pjproject 2.17), build.sh, GPL notice and source offer
  scripts/     latency_report.py  pjsua_peer.sh  pf/ai-phone.conf  utterances.txt
  packaging/   Formula/ai-phone.rb  Dockerfile.cuda  launchd/com.ai-phone.daemon.plist.tmpl
  tests/       unit/ integration/ soak/
  .github/workflows/  wheels.yml  docker.yml  tests.yml
```

### 10.2 macOS **[D]**

`uv tool install ai-phone` is the supported path (misaki 0.9.4's `Requires-Python <3.13` is ignored by uv and enforced by pip **[V]**). The Homebrew tap formula depends on core `pjproject` and `python@3.12` and builds the SWIG module from source because homebrew-core rejects prebuilt binaries **[V]**. `doctor --fetch` on first run downloads the tier and runs `python -m spacy download en_core_web_sm`.

### 10.3 Linux **[D]**

`docker run --network host -v ~/.ai-phone:/var/lib/ai-phone ghcr.io/<owner>/ai-phone:cuda`. The image pre-installs llama-cpp-python from the cu12x index, kokoro-onnx, sherpa-onnx, silero-vad, and the spaCy model; weights are fetched into the mounted volume by `doctor --fetch`. `-cpu` image and bare-metal `uv tool install` are post-launch.

### 10.4 Service management

macOS: `ai-phone install-service` writes `~/Library/LaunchAgents/com.ai-phone.daemon.plist` (RunAtLoad, KeepAlive, stdout/stderr to `~/Library/Logs/ai-phone/`). Linux: the container's restart policy; a systemd unit is a post-launch nicety.

### 10.5 CI **[D]**

- `wheels.yml`: macOS arm64 runner builds the SWIG module per ABI (cp311–cp313) against the brew bottle, `delocate`s; manylinux x86_64 builds pjproject 2.17 from source with `-fPIC`, SWIG per ABI, `auditwheel`. Publishes wheels + the extension sdist to PyPI on tags.
- `docker.yml`: builds and pushes the `cuda` image to GHCR on tags.
- `tests.yml`: unit tests on every push; integration tests using brew's/the container's `pjsua` as the SIP peer.

## 11. Testing strategy **[D]**

| Level | What | How |
|---|---|---|
| Unit | clause chunker; barge-in detector on synthetic audio (tone under playback, speech over playback, speakerphone leak simulation); endpointer timing; policy evaluation (RFC 1918 / loopback / trusted peers); config validation; ring buffer overflow/underflow | pytest, no models, no network |
| Integration | loopback call with the fake peer: `pjsua --local-port 5062 --null-audio --play-file utt.wav --auto-play --auto-play-hangup sip:ai@127.0.0.1:5060` **[V]** (pjsua's help says `--auto-play` is for incoming calls only, but the code connects the file to any call, so outbound-from-pjsua works); echo mode proves SIP+RTP+resampling before models; full mode with real workers proves a turn end to end | pytest + subprocess, marked `integration`, requires models |
| Latency | `scripts/utterances.txt` (20 lines) played by the peer; `latency_report.py` computes p50/p95 per stage; the M1 gate | manual on the dev machine, CI-optional |
| Soak | 24 h registration against a real trunk with a call every hour | manual, M2 |
| Isolation | run the pf ruleset in `scripts/pf/`, place a call, assert the log shows only the SIP peer | manual, part of the launch demo |

## 12. Security model **[D]**

- Threat: SIP scanners on the public internet. Defaults: `registered_trunk` acceptance (unsolicited INVITEs get 403), per-IP lockout, TLS+SRTP accepted when offered, RTP port range narrow and documented.
- Secrets: config file mode 0600; `password_env` supported; never logged.
- API: loopback by default; bearer token required otherwise.
- Isolation: no outbound connections except SIP/RTP to the peer (and STUN if the user configures it, flagged by `doctor`). No telemetry, no update checks. Verified by the shipped pf ruleset during the launch demo.
- Supply chain: pinned versions; the vendored PJSIP build script records the exact pjproject tarball SHA.

## 13. Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| Worker crash | pipe EOF / process exit | restart (≤ 3 per 10 min); mid-call: cached apology, continue |
| Model missing or corrupt | manifest check at start | refuse to start; `doctor` explains |
| Trunk drop / registrar unreachable | `onRegState` error | backoff §3.3.1; calls impossible until re-registered; status shows DEGRADED |
| RTP stops mid-call | watchdog on `last_rx_ns` | BYE after 10 s |
| TTS slower than real time | `tx_underruns` > 0 | silence fill; `doctor` recommends lighter tier |
| LLM over budget | `llm_first_clause_ms` p50 > 250 | switch tier to t4 (config), re-measure |
| Memory pressure | worker RSS check at warm-up | refuse tier, suggest t4 |
| Runaway generation | `max_tokens` cap; `max_call_sec` | hard stop |
| Duplicate INVITE / retransmits | PJSIP transaction layer | handled by PJSIP |

## 14. Extension points (post-launch, do not build early)

- **Home Assistant**: HACS integration as a client of the HTTP API (calls out, status in). Not an add-on (HA hosts have no GPU).
- **Tool use**: LLM backends already stream tokens; adding tool calls means a tool schema in the system prompt and a dispatcher in `pipeline/turn.py`. Latency cost is a second LLM round trip, so it stays behind a flag.
- **Multi-call**: Linux/GPU only; one pipeline per call, workers batched or forked; PJSIP handles multiple calls natively.
- **Multilingual**: swap to `parakeet-tdt-0.6b-v3` and other Kokoro voices; the G2P dependency changes per language.
- **Full-duplex model** (Moshi-style): replaces Layers 2–3's STT→LLM→TTS with one worker; the media port and control plane stay.
- **SMS**: SIP MESSAGE method only, where the trunk supports it.

## 15. Decision log

| Date | Decision | Status |
|---|---|---|
| 2026-09-02 | Complete appliance (Approach B) with the weekend prototype as Milestone 1 inside it | [D] |
| 2026-09-02 | Python; Go rejected by owner; TS/Swift/Rust evaluated and rejected | [D] |
| 2026-09-02 | PJSIP via pjsua2, vendored into the platform wheel; AGPLv3 | [D] |
| 2026-09-02 | Hand-rolled asyncio pipeline for M1; Pipecat only if it deletes more code than it adds | [D] |
| 2026-09-02 | LLM in the daemon's own worker process; no Ollama or any external model server | [D] |
| 2026-09-02 | mlx-audio for STT/TTS/VAD on Mac; parakeet-mlx fallback only | [D] |
| 2026-09-02 | Media port at 16 kHz; VAD in main process; three workers; shared-memory audio, JSON control | [D]/[P] as marked |
| 2026-09-02 | `lan` mode includes loopback and link-local | [D] |
| 2026-09-02 | English-only, single-call, no web UI, no SMS through launch | [D] |
| 2026-09-02 | Home Assistant is an integration, not an add-on | [D] |

## 16. Verified facts (2026-09-02)

pjproject 2.17 (2026-04-22); Homebrew bottle contents (static libs, headers, `.pc`, `pjsua`, no `.i`); `pj::AudioMediaPort` with `createPort`/`onFrameRequested`/`onFrameReceived`; PJSUA conference bridge default 16 kHz, resamples mismatched ports; `Endpoint::audDevManager().setNullDev()` and `setNullDev2(bool)`; `libRegisterThread`; SWIG `-threads` in pjproject's Python Makefile; `pjsua` flags `--null-audio --play-file --auto-play --auto-play-hangup --local-port`; PyPI `pjsua2` is a stale 2.12 sdist (2023); GPLv2-or-later → AGPLv3 via GPLv3 §13; `mlx-community/parakeet-tdt-0.6b-v2` and `-v3`; `mlx-community/nemotron-3.5-asr-streaming-0.6b`; `mlx-community/Kokoro-82M-bf16` with `voices/af_heart.*`, 24 kHz; `mlx-community/Qwen3.5-9B-MLX-4bit` and `Qwen3.5-4B-MLX-4bit` (both with vision tower, dropped by mlx-lm at load); Qwen3.5 chat template honours `enable_thinking=False`; mlx-audio 0.5.1 ships parakeet (streaming), nemotron_asr, kokoro, silero VAD; misaki 0.9.4 `Requires-Python <3.13` (uv ignores, pip enforces) and runtime `spacy.cli.download` of `en_core_web_sm`; llama-cpp-python 0.3.35 sdist-only on PyPI; kokoro-onnx requires Python < 3.14; Pipecat 1.8.1 has no SIP transport; Docker bridge networking advertises the container IP in SDP.

## 17. Glossary

**SIP** Session Initiation Protocol, the signalling standard for calls. **RTP** Real-time Transport Protocol, the media stream. **G.711 / PCMU / PCMA** the 8 kHz telephone codecs (μ-law, A-law). **INVITE / BYE / REGISTER** SIP requests to start a call, end a call, and announce reachability. **403 / 486** SIP responses Forbidden and Busy Here. **DID** a phone number from a trunk provider. **PBX** a local phone switch. **Trunk** the SIP service that connects to the phone network. **STUN** a way to learn your public address behind NAT. **VAD** voice activity detection. **Endpointing** deciding the caller has finished a sentence. **Barge-in** the caller talking over the agent. **G2P** grapheme-to-phoneme, the text front end of a TTS model. **Prompt cache** reusing the model's computation for an unchanged prompt prefix. **[D]/[P]/[V]** see the notation at the top.
