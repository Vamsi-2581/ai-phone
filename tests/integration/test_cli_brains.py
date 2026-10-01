"""One real turn through each subscription CLI: the protocols, the login and the timings, against the live thing.

Each test is skipped unless that CLI is installed and logged in. When it runs it spends one tiny turn of the owner's
own plan (and prints what it measured, so `-s` shows how fast the first word was).
"""
import shutil
import subprocess
import time

import pytest

from ai_phone.services.backends.cli_brain import ClaudeCodeLLM, CodexLLM, codex_models
from ai_phone.services.errors import BrainError

pytestmark = pytest.mark.integration

PROMPT = "You are a phone assistant. Answer in one short sentence."
ASK = "Say hello in five words."
LIMIT_SEC = 20.0


def _turn(brain, label: str) -> str:
    """prepare, then one turn, with the timings printed. A CLI nobody is logged into skips the test."""
    try:
        t0 = time.monotonic()
        brain.prepare(PROMPT)
        ready = time.monotonic() - t0
        t1 = time.monotonic()
        said = "".join(brain.stream(1, [{"role": "system", "content": PROMPT}, {"role": "user", "content": ASK}], 60))
        took = time.monotonic() - t1
    except BrainError as e:
        if e.kind in ("not_logged_in", "cli_missing"):
            pytest.skip(f"{label}: {e}")
        raise
    finally:
        brain.close()
    print(f"\n{label}: session ready in {ready * 1000:.0f} ms, reply in {took * 1000:.0f} ms, "
          f"first word after {brain.last_timings['first_token_ms']:.0f} ms, said {said.strip()!r}")
    assert said.strip(), "the CLI said nothing"
    assert took < LIMIT_SEC, f"the reply took {took:.1f} s"
    return said


def test_a_claude_subscription_answers_a_phone_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    if shutil.which("claude") is None:
        pytest.skip("claude is not installed")
    brain = ClaudeCodeLLM(model="haiku", first_token_timeout_ms=int(LIMIT_SEC * 1000), stream_idle_timeout_ms=10000)
    _turn(brain, f"claude haiku {brain.cli_version()}")


def test_a_chatgpt_subscription_answers_a_phone_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    if shutil.which("codex") is None:
        pytest.skip("codex is not installed")
    status = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=30)
    if "logged in" not in (status.stdout + status.stderr).lower():
        pytest.skip("codex is not logged in")
    models = codex_models()
    assert models and all(m.id and m.name for m in models)
    print("\ncodex models:", ", ".join(m.id for m in models))
    model = "gpt-5.6-luna" if any(m.id == "gpt-5.6-luna" for m in models) else models[0].id
    brain = CodexLLM(model=model, first_token_timeout_ms=int(LIMIT_SEC * 1000), stream_idle_timeout_ms=10000)
    _turn(brain, f"codex {model} {brain.cli_version()}")
