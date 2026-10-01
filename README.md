# ai-phone

A phone line answered by an AI that runs on your own machine. Give an agent a real number (a new one
bought with your own Telnyx or Twilio key, or one already on that account), divert your own phone to it,
or reach it at a SIP address; speech-to-text, the language model and text-to-speech run locally by
default, and during a call the daemon talks to nothing but SIP/RTP (and one brain only if you chose
one that is not on this Mac: your own endpoint, or the Claude or ChatGPT plan you already pay for).

**Status: Milestone 1 (latency proof) passed; Milestone 2 (lines) code complete, its live gate pending; Milestone 3 (agent tool) code complete, its gate pending; Milestone 4 (get started: install script and setup wizard) in progress.**
English only, one call at a time. See `architecture-essentials.md` for how it works.

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
git clone https://github.com/oxnw/ai-phone.git && cd ai-phone
./scripts/install.sh        # the SIP library and the Python environment (needs Homebrew and uv)
uv run ai-phone setup       # pick a line, a brain and a voice; downloads the models; checks it all works
uv run ai-phone run         # start ai-phone
```

`setup` saves everything under `~/.ai-phone/` (`config.yaml`, and keys and passwords in `secrets.env`, both
readable only by you) and changes only what you pick when you run it again. `uv run ai-phone setup --plain` asks the
same questions as plain prompts. The models go to the Hugging Face cache; `uv run ai-phone models pull` fetches them
without the wizard. The commands below are written as `ai-phone …`; from the repo folder that is `uv run ai-phone …`.

No carrier yet? Pick "A SIP address" in setup and talk to your agent from this Mac with
`pjsua --local-port 5070 sip:agent@127.0.0.1` (free).

Real numbers, diverts and SIP addresses are Milestone 2, below.

## Three ways to call it (Milestone 2)

```bash
# 1. Its own number (a Telnyx or Twilio account and API key; about £1/month for the number)
export TELNYX_API_KEY=…
ai-phone lines add telnyx --key-env TELNYX_API_KEY --country GB --area 020
ai-phone lines add twilio --key-env TWILIO_ACCOUNT_SID --secret-env TWILIO_AUTH_TOKEN --existing +15550100000   # a number you already have: nothing is bought, and removing the line keeps it
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

# The brain on this Mac: one of t4, t9, qwen2b, llama3b, gemma4b, mistral7b, gemma12b (the catalog in models.py),
# auto to pick by memory, or any mlx-lm 4-bit repo id
#   models: {tier: gemma4b}
# Or a brain that is not on this Mac (see "Any brain" below)
#   brain: {kind: claude, model: haiku}                 # your Claude plan, through the Claude Code login
#   brain: {kind: chatgpt, model: gpt-5.6-luna}         # your ChatGPT plan, through the Codex login
#   brain: {kind: openai_compatible, base_url: "http://127.0.0.1:1234/v1", model: "…"}
# And the voice
#   tts: {voice: bf_emma, speed: 1.0}   (English voices: af_/am_ American, bf_/bm_ British)
```

`--key-env` and `--secret-env` name variables in your environment or in `~/.ai-phone/secrets.env` (where setup saves them).

During a call the daemon opens no network connection except SIP/RTP to the peer and, only if you configured one, HTTP(S) to that brain endpoint. Buying or connecting a number talks to the carrier once, from the `lines` command or `setup`, and the daemon never loads the carrier code. Provisioning is journaled: `ai-phone lines resume` picks up where a crash left off, `ai-phone lines remove` deletes what ai-phone created at the carrier (a number you brought is disconnected and stays on your account) and lists exactly what is left for you to remove in the portal if a deletion fails — or, if the journal recording what was created has gone missing, refuses rather than delete the local record of a number that is still billing. Configuration applies at restart. The NAT and transport checks run before calling it done are in [docs/nat-checklist.md](docs/nat-checklist.md); the 24 h registration soak is `scripts/soak.sh`.

What an incoming call hears is set by one `inbound:` section of the config: a greeting that plays on every incoming
call, and the instructions ai-phone's own model follows when no AI is waiting in `ai-phone listen` (the example config
takes a message).

## Any brain

The agent's words can come from a model on this Mac, from a plan you already pay for, or from any
OpenAI-compatible server. Speech-to-text and the voice always run here, whichever you pick. `ai-phone setup`
asks, and the daemon says at start where the words of a call go.

| Brain | First word (measured here) | What leaves this Mac | What it needs |
|---|---|---|---|
| **On this Mac** (default) | 874 ms p50 on the 4B tier | nothing: no socket is opened | about 4 GB of models |
| **Your Claude subscription** | 2.4–5.5 s on a call's first turn, about 1 s after | what callers say, the call's instructions and the conversation go to Anthropic (not audio) | `claude` installed and logged in; your plan's own limits apply |
| **Your ChatGPT subscription** | 3.9–4.4 s on a call's first turn, 1.1–1.9 s after | the same, to OpenAI | `codex` installed and logged in; your plan's own limits apply |
| **Your own endpoint** | whatever that server does | the same, to that server | an OpenAI-compatible URL, `https` unless it is on this Mac; a key if it wants one |

A subscription brain uses no API key: ai-phone drives the CLI you are already logged into (`claude -p`, or
`codex app-server`) in an empty private directory, with no tools and none of your own project settings, and
streams its words to the caller. It asks each CLI what its login is before it uses it, and refuses to run on
an API key: a key is billed per request, and on the Claude side `ANTHROPIC_API_KEY` beats the login you have,
so neither it nor anything else that would re-route a CLI (`OPENAI_*`, a `*_BASE_URL`) is passed on to one.
What callers say is model input, so neither session gets tools: Claude Code is run with none and asked what it
came up with, and Codex is given a `CODEX_HOME` of ai-phone's own, so none of your MCP servers, hooks, plugins
or `AGENTS.md` is in a call, with every tool feature turned off and a read-only sandbox that approves nothing.
A session that reaches for a tool anyway does not answer the caller: the turn ends and the session goes.
Install them with `npm install -g @anthropic-ai/claude-code` (then `claude`
and `/login`) or `npm install -g @openai/codex` (then `codex login`). A plan is a second or two slower to the
first word than the model on this Mac, and it is not private; what it buys is a much stronger brain on the
phone than 24 GB of memory can hold. The session belongs to one call: it is started while the greeting plays
and let go when the call ends. Proving the login costs one tiny turn of your plan every time ai-phone starts
(and again if the brain worker has to be restarted mid-run), on top of the calls themselves.

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

Transcripts leave ai-phone only through the command that asked for them, and, if you chose a brain that is not on
this Mac (`brain.kind: openai_compatible`, `claude` or `chatgpt`), through that endpoint — or through your own
logged-in CLI, to Anthropic or OpenAI — during a phone agent call: what callers say and the conversation so far go
there on every turn, with the call's instructions. If your agent runs on a cloud model, that service sees the
transcript.

## License

AGPL-3.0-or-later. `native/pjsua2/` carries PJSIP's GPLv2-or-later interface files; see its README.
