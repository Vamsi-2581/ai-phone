<h1 align="center">ai-phone</h1>
<p align="center">An AI phone agent that runs on your computer.</p>
<p align="center">
  <a href="docs/usage.md">Documentation</a> ·
  <a href="architecture-essentials.md">Architecture</a> ·
  <a href="LICENSE">AGPL-3.0</a>
</p>

![ai-phone setup running in a terminal](docs/assets/setup.png)

### Installation

| Requirement | Details |
|---|---|
| Platform | macOS on Apple Silicon. Intel Macs, Linux and Windows are not currently supported. |
| Memory | 16 GB recommended for the default 4B model; 32 GB for 9B. Setup also offers a smaller 2B model for 8 GB systems. |
| Dependencies | Git, [Homebrew](https://brew.sh) and [uv](https://docs.astral.sh/uv/). The installer handles Python packages and the native SIP library. |
| Storage | Several GB for model downloads, depending on the models selected. Setup checks available disk space. |
| Phone connection | A SIP address for direct calls, or your own SIP trunk, Telnyx or Twilio account for telephone numbers. Carrier charges apply. |

```bash
git clone https://github.com/oxnw/ai-phone.git
cd ai-phone
./scripts/install.sh
uv run ai-phone setup
```

Setup lets you choose incoming calls, outgoing calls, or both, then configures the model, line and voice.
It recommends a local model based on detected RAM and downloads the required weights.
Use `uv run ai-phone setup --plain` for plain prompts.

The current speech backends use MLX on Apple Silicon, even when a cloud model handles the conversation.
Supporting Linux or Windows would require different speech backends and changes to installation and platform
integration. The platform requirement applies to the computer running ai-phone; callers can use any phone or SIP client.

### Usage

Start the daemon after setup:

```bash
uv run ai-phone run
```

Keep it running while you want to receive calls. For outgoing calls only, start it when needed and stop it
with Ctrl-C afterward. Setup does not install a background service.

From another terminal in the repo, give the phone agent instructions for a call:

```bash
uv run ai-phone call +15555550100 --json \
  --prompt "Ask whether they have a table for two at eight tonight."
```

Replace the example number with the number you want to call. The command returns the transcript when the call
ends. Every outgoing call starts with an AI disclosure. Incoming calls use the greeting and instructions you
set during setup. English and one call at a time are currently supported.

For a free local test, enable incoming calls, select **A SIP address** in setup, and keep the name `agent`:

```bash
pjsua --local-port 5070 sip:agent@127.0.0.1
```

Run setup again to change the voice, model, call directions or caller ID, then restart the daemon.

### Agent integration

An agent with shell access can use relay mode to control each turn:

```bash
uv run ai-phone call +15555550100 --relay --json
uv run ai-phone say "Hi, I'd like to book a table for two." --json
uv run ai-phone listen --json
uv run ai-phone say "Eight o'clock, please." --json
uv run ai-phone hangup --say "Thank you. Goodbye." --json
```

With no active call, `listen --json` waits for an incoming call and handles it in relay mode.
Each command with `--json` returns one JSON object on stdout; `call` also sends progress events to stderr.
An unanswered relay turn times out after 25 seconds by default. See the [command reference](docs/usage.md#agent-commands)
for call IDs, transcript retrieval, timeouts and exit codes.

### Models

| Component | Options |
|---|---|
| Speech recognition | Parakeet, running locally through MLX. |
| Language model | A local MLX model, your Claude Code or Codex subscription, or an OpenAI-compatible endpoint. |
| Voice | Phonon with its bundled English voice, or Kokoro with American and British voices and adjustable speed. |

Speech recognition and synthesis stay local. A remote language model receives the call's text, instructions and
conversation history. An external agent's cloud provider also sees any transcripts passed to it. Phone audio
travels over SIP/RTP; carrier charges and subscription limits apply.

### Documentation

- [Usage guide](docs/usage.md): lines, forwarding, caller ID, models and CLI reference.
- [Example configuration](config.example.yaml): available settings.
- [Architecture](architecture-essentials.md): components and call flow.
- [NAT checklist](docs/nat-checklist.md): network configuration.
- [Measurements](docs/phonon-2026-10-27.md): latest local call and interruption results.

Install and setup have been checked from a fresh clone. Local call and interruption tests have run;
live carrier and end-to-end agent acceptance checks remain pending. The latest local run measured 1.50 s median
first-audible response under heavy memory pressure; it missed the one-second target. Full conditions and
[earlier results](docs/usage.md#latency-history) are documented.

### License

[AGPL-3.0-or-later](LICENSE). The bundled PJSIP interface files use
[GPLv2-or-later](native/pjsua2/COPYING); see the [license notes](native/pjsua2/README.md).
