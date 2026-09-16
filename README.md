# ai-phone

A phone line answered by an AI that runs on your own machine. Give an agent a real number (bought with
your own Telnyx or Twilio key, once, from the command line), divert your own phone to it, or reach it at a
SIP address; speech-to-text, the language model and text-to-speech run locally by default, and during a
call the daemon talks to nothing but SIP/RTP (and a brain endpoint only if you configured one).

**Status: Milestone 1 (latency proof) passed; Milestone 2 (lines) code complete, its live gate pending; Milestone 3 (agent tool) code complete, its gate pending.**
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

Packaging (`uv tool install`, Homebrew tap, Docker) is Milestone 4. Real numbers, diverts and SIP addresses are Milestone 2, below.

## Three ways to call it (Milestone 2)

```bash
# 1. Its own number (a Telnyx or Twilio account and API key; about £1/month for the number)
export TELNYX_API_KEY=…
ai-phone lines add telnyx --key-env TELNYX_API_KEY --country GB --area 020
ai-phone run

# 2. Your own mobile number. It stays on your SIM, so this is two halves:
#    calls OUT show your number once you have verified it (one SMS code) …
ai-phone lines verify-caller-id telnyx +447700900123 --key-env TELNYX_API_KEY
#    … and calls IN reach the agent through your carrier's divert (one dial code, printed here)
ai-phone lines forward telnyx --when no-answer --from +447700900123
#   dial the printed **61* code on your phone; ##002# cancels every divert

# 3. A SIP address, no carrier (agents calling agents)
ai-phone lines add sip --id agents --user agents --host home.example.org --trusted 203.0.113.7
ai-phone call sip:agents@friend.example.org --line agents

# Outbound from the agent's number (or from your verified mobile number). With no --line, a number goes out on the
# first registered trunk and a sip: address on the first sip line; with two trunks configured, name the one you mean.
ai-phone call +447700900123
ai-phone status

# Any brain: LM Studio, llama.cpp, or your own agent behind an OpenAI-compatible endpoint
#   brain: {kind: openai_compatible, base_url: "http://127.0.0.1:1234/v1", model: "…"}
```

During a call the daemon opens no network connection except SIP/RTP to the peer and, only if you configured one, HTTP(S) to that brain endpoint. Buying a number talks to the carrier once, from the `lines` command, and the daemon never loads the carrier code. Provisioning is journaled: `ai-phone lines resume` picks up where a crash left off, `ai-phone lines remove` deletes what the carrier created and lists exactly what is left for you to remove in the portal if a deletion fails — or, if the journal recording what was created has gone missing, refuses rather than delete the local record of a number that is still billing. Configuration applies at restart. The NAT and transport checks run before calling it done are in [docs/nat-checklist.md](docs/nat-checklist.md); the 24 h registration soak is `scripts/soak.sh`.

What an incoming call hears is set by one `inbound:` section of the config: a greeting that plays on every incoming
call, and the instructions ai-phone's own model follows when no AI is waiting in `ai-phone listen` (the example config
takes a message).

## Use it from your AI (Milestone 3)

Tell your agent it has the `ai-phone` command. It has two ways to make a call.

**Phone agent mode**: ai-phone's own model holds the conversation; your agent gets the transcript at the end:

```bash
ai-phone call +15551234567 --json \
  --prompt "Ask whether they have a table for two at 8 tonight, under the name Sam."
```

A phone agent `call` blocks until the call ends, which can take up to `conversation.max_call_sec` (600 seconds). With
`--json` it also writes each progress event to stderr as one JSON line, starting with
`{"event": "created", "call_id": "c7f3a2"}`. If your agent's shell kills the command before the call ends, the call
carries on. Take the ID from that `created` line and run `ai-phone listen --call c7f3a2 --json` once the call is over;
it prints the result with the transcript. While the call is still going, that `listen` exits 11 at once, so wait and try
again (`ai-phone status` shows the call while it is up). The daemon keeps the last 20 calls.

**Relay mode**: your agent holds the conversation itself, one turn at a time:

```bash
ai-phone call +15551234567 --relay --json     # returns once answered
ai-phone say "Hi, I'd like to book a table for two at eight tonight."
ai-phone listen --json                         # waits until they finish speaking
ai-phone say "Sam."
ai-phone hangup --say "That's all, thank you. Goodbye." --json
```

`ai-phone listen` with no call in progress waits for someone to call one of the agent's numbers (it first reports a
relay call that ended since anyone last listened). When someone calls, ai-phone answers with `inbound.greeting` and
`listen` prints `incoming call c9b1e0 on main from sip:+15551234567@sip.carrier.example` (the caller's SIP URI as the
SIP stack reports it, sometimes with a display name, not a bare number); your agent carries on with `listen` and
`say`. While a phone agent call is in progress (an outbound one, or an incoming call that arrived while nobody was
listening), `listen` without `--call` exits 11 at once instead of waiting, so an agent that polls for incoming calls
should pause before it tries again. While your agent is thinking, ai-phone says "One moment." so the line never goes silent, and "Still with you,
one moment." after 10 seconds; after 25 seconds without a reply it apologises and hangs up. On an outbound relay call
the 25 seconds count from the answer until your agent's first `say`, with no "One moment." before it, so say the first
line straight away. Every outbound call starts with "Hi, this is an AI assistant calling." Emergency numbers are never
dialled (a target whose digits look like one, such as extension 100 or 1000, is refused too; call such extensions by
name), and `calls.daily_limit` caps outbound calls per day.

With `--json`, each of these four commands (`call`, `listen`, `say`, `hangup`) prints exactly one JSON object on stdout
(`call` also writes its progress events to stderr, one JSON object per line); a
malformed command line (a missing or unknown option) is reported by the argument parser on stderr with exit code 2,
and Ctrl-C while `listen`, `say` or `hangup` is waiting exits 130 (Ctrl-C during `call` hangs the call up and prints
its result).

| Exit code | Meaning |
|---|---|
| 0 | answered (relay), or the call ran and ended; `listen` got an event; `say` was spoken or interrupted; `hangup` ended the call |
| 2 | bad usage (a missing or unknown option, `--prompt` with `--relay`, text too long) |
| 5 | busy (the other end, or a call is already in progress here) |
| 6 | no answer |
| 7 | rejected, or cancelled by `hangup` while still ringing |
| 8 | redirected |
| 9 | ai-phone is not running, went away, or sent a reply this command does not understand; from `hangup`, also: the call had still not ended 40 seconds after the hangup (ai-phone waits 30 seconds for the call to end, including any `--say` text, hangs up again, then waits 10 more), so it may still be up |
| 10 | `listen` timed out (the call, if any, goes on) |
| 11 | no such call, no call in progress, call already ended, or `listen`/`say` on a phone agent call |
| 12 | another `listen` is already waiting for calls |
| 13 | emergency number |
| 14 | daily outbound call limit reached |
| 130 | interrupted with Ctrl-C |

Transcripts leave ai-phone only through the command that asked for them, and, if you configured a remote brain
(`brain.kind: openai_compatible`), through that endpoint during a phone agent call: what callers say and the
conversation so far go there on every turn, with the call's instructions. If your agent runs on a cloud model, that
service sees the transcript.

## License

AGPL-3.0-or-later. `native/pjsua2/` carries PJSIP's GPLv2-or-later interface files; see its README.
