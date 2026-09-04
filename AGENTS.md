# Working on ai-phone with an agent

Rules for any AI agent (Claude Code, Codex, CodeRabbit, anything else) working in this repository.
They apply on top of the tool's own instructions.

1. **Secrets never pass through the conversation.** SIP passwords, carrier API keys and tokens live in
   `~/.ai-phone/secrets.env` (0600) and are written there by the user in their own terminal. Never run a command
   whose output could print that file; to see which names it holds use `cut -d= -f1`. If a secret is ever exposed,
   say so immediately and ask the user to rotate it.
2. **Read `architecture-essentials.md` first, then `architecture.md`.** The six invariants there are not negotiable
   without the maintainer: during a call no network but SIP/RTP to the peer and the one configured brain endpoint; the
   PJSIP thread never blocks; 16 kHz int16 20 ms frames inside; every generation has a gen_id; latency is measured, not
   claimed; one daemon, one call at a time across all lines.
3. **Tests before commits.** `uv run pytest tests/unit -q` must be green; the integration suite runs with
   `AI_PHONE_TEST_SIP_PORT=5080 uv run pytest tests/integration -q` so it never dials a live daemon on 5060.
   A commit with failing tests is a defect, not a checkpoint.
4. **Do not reopen settled decisions** (Python, PJSIP via pjsua2, no Ollama or model server, AGPLv3, English-only
   and single-call through launch) and do not add product or startup framing to the docs: this is an open-source
   mechanism, not a product.
