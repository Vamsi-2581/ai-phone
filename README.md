<p align="center">
  <img src="docs/assets/header.svg" alt="ai-phone — Give your AI a phone." width="100%">
</p>

<p align="center">
  <strong>A real phone number. A local voice. A command line your agent can use.</strong>
</p>

<p align="center">
  <a href="#get-a-dial-tone">Quickstart</a> ·
  <a href="#hand-over-the-controls">Agent commands</a> ·
  <a href="#pick-the-brain-pick-the-voice">Models</a> ·
  <a href="#how-fast-does-it-talk-back">Measurements</a> ·
  <a href="docs/usage.md">Full guide</a>
</p>

Give an AI a number people can call. Let it take a message, make a call for you, or hand the conversation
to another agent with `listen` and `say`. Speech recognition, the language model and the voice run on
your computer by default.

**Currently: macOS on Apple Silicon · English · one call at a time.**

## Yes, it can make the call

Once setup is done, give it a job:

```bash
uv run ai-phone call +15555550100 --json \
  --prompt "Ask whether they have a table for two at eight tonight."
```

The phone agent holds the conversation and returns the transcript when the call ends.
Replace the example number with the number you want to call. Every outgoing call starts by saying
it's an AI assistant.

Or let the phone ring. Incoming calls get your greeting and instructions: take a message or answer a
question. If another agent is already waiting with `listen`, it can handle the conversation instead.

| Give it… | How it works |
|---|---|
| **Its own number** | Buy or connect a number using your own Telnyx or Twilio account. |
| **Your number** | Forward incoming calls to it; verify a number you own to use it as outgoing caller ID. |
| **A SIP address** | Call it directly, including free local calls without a carrier. |

## Get a dial tone

You'll need an Apple Silicon Mac, [Homebrew](https://brew.sh) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/oxnw/ai-phone.git
cd ai-phone
./scripts/install.sh
uv run ai-phone setup
uv run ai-phone run
```

The setup wizard handles the choices: **receive calls, place calls, or both**; a brain recommended
for your computer; a line; a voice; and what to say when someone calls. It downloads the models you
need and lets you preview the voice.

Leave `run` open and use call commands in another terminal, from the same repo folder.
Receiving calls means keeping it running. Only making calls? Start it when you need it and stop it
with Ctrl-C afterward. Setup doesn't install a background service.

> **Try it without buying a number.** Enable incoming calls, choose **A SIP address**, and keep the
> name `agent` in setup. With ai-phone running, open another terminal and call
> `pjsua --local-port 5070 sip:agent@127.0.0.1`.

Run `uv run ai-phone setup` again to change things, including **call directions** and **caller ID**.
Restart the daemon after changing its settings. Prefer plain prompts? Add `--plain`.

## Hand over the controls

Any agent that can run shell commands can hold the conversation itself. Start a relay call, then
alternate between speaking and listening:

```bash
uv run ai-phone call +15555550100 --relay --json
uv run ai-phone say "Hi, I'd like to book a table for two." --json
uv run ai-phone listen --json
uv run ai-phone say "Eight o'clock, please." --json
uv run ai-phone hangup --say "Thank you. Goodbye." --json
```

With no call in progress, `uv run ai-phone listen --json` waits for an incoming call.
With `--json`, each command returns one JSON object on stdout; `call` also sends progress events
to stderr. Say the first line promptly after an outbound relay call answers: an agent that leaves
the caller waiting for 25 seconds is disconnected.

The [command guide](docs/usage.md#agent-commands) covers timeouts, transcripts, call IDs and exit codes.

## Pick the brain. Pick the voice.

| Brain | Where the thinking happens |
|---|---|
| **Local model** · default | On your computer, directly through MLX. Setup recommends a model for your memory. |
| **Claude** | Through your logged-in Claude Code CLI and subscription. |
| **ChatGPT** | Through your logged-in Codex CLI and subscription. |
| **Your own endpoint** | An OpenAI-compatible server you configure. |

Speech recognition and synthesis stay local with every option. Choose **Phonon** for its bundled
English voice, or **Kokoro** for American and British voices with adjustable speed. Both run directly
through MLX; no separate speech server is needed.

Local mode keeps model inference on your machine. Phone audio still travels to the SIP peer.
If you choose a remote brain, the call's text, instructions and conversation history go to that
provider; if an external agent uses a cloud model, its provider sees the transcripts you pass it.
Carrier charges and subscription limits still apply.

[Brain configuration and privacy details →](docs/usage.md#brains-and-privacy)

## Under the receiver

```text
Caller → SIP/RTP → Parakeet → your chosen brain → Phonon / Kokoro → SIP/RTP → Caller
                  hears         thinks              speaks
```

One Python daemon handles the call. PJSIP handles telephony; workers handle the models.
Interruptions cancel the current reply and clear queued speech so the caller can take their turn.
Models load before calls begin. During a call, network traffic is limited to SIP/RTP and the brain
you configured, if it isn't local.

[Architecture in five minutes](architecture-essentials.md) · [Full design](architecture.md)

## How fast does it talk back?

The latest local run completed **20/20 calls** on an Apple M5 Pro with 24 GB RAM, Qwen3.5 4B and
Phonon, on **2026-10-27**:

| Measurement | Median | p95 |
|---|---:|---:|
| First audible response | **1.50 s** | **1.94 s** |
| TTS first chunk | 52 ms | 84 ms |

First audible response is measured from the caller's last speech chunk to the first non-silent
audio frame sent to the SIP stack, including endpoint wait. Network and handset buffering come on
top. The host was under heavy memory pressure; this is a measured run, not a speed promise.

All five interruption tests cancelled and resumed correctly. Four stopped within 300 ms; one took
396 ms. The one-second response target and the interruption timing target remain unmet in this run.

[Full Phonon results](docs/phonon-2026-10-27.md) · [Kokoro rerun](docs/latency-2026-10-26.md) ·
[Earlier measurements](docs/usage.md#latency-history)

## Keep digging

Install and setup have been verified from a fresh clone, and local calls and interruptions have
been exercised. Live carrier and end-to-end agent acceptance checks are still pending.

- [Usage guide](docs/usage.md): numbers, forwarding, model configuration and the full CLI contract.
- [Example configuration](config.example.yaml): the knobs, with comments.
- [NAT checklist](docs/nat-checklist.md): getting calls through a real network.
- [Architecture](architecture-essentials.md): start here if you want to work on the code.

AGPL-3.0-or-later. See [LICENSE](LICENSE). The bundled PJSIP interface files have their own
[license notes](native/pjsua2/README.md) and [GPLv2-or-later license](native/pjsua2/COPYING).
