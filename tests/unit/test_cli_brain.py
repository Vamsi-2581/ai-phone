"""The subscription brains, driven against a fake CLI that speaks the real protocols from a scripted list of turns.

Nothing here runs the real `claude` or `codex` (tests/integration/test_cli_brains.py does that once, for one turn):
the fake records everything it is asked to do, so the tests can state what the backends send, what they reuse and
what they refuse.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

import pytest

from ai_phone.services.backends.cli_brain import ClaudeCodeLLM, CodexLLM, codex_models, make_claude, make_codex
from ai_phone.services.errors import BrainError

FAKE_CLI = r'''#!/usr/bin/env python3
"""A fake Claude Code / Codex CLI. It follows the JSON script named by AI_PHONE_FAKE_CLI and writes everything it was
asked to do to that script's log file, one JSON object per line."""
import json
import os
import queue
import sys
import threading
import time

SPEC = json.load(open(os.environ["AI_PHONE_FAKE_CLI"]))
TURNS = SPEC.get("turns") or [{}]


def note(**what):
    with open(SPEC["log"], "a") as f:
        f.write(json.dumps(what) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def directive():
    """The next scripted turn. The count is kept on disk, so a session that starts again carries on down the list."""
    path = SPEC["log"] + ".turns"
    n = int(open(path).read()) if os.path.exists(path) else 0
    with open(path, "w") as f:
        f.write(str(n + 1))
    return TURNS[n] if n < len(TURNS) else TURNS[-1]


def flag(name, default=None):
    argv = sys.argv[1:]
    return argv[argv.index(name) + 1] if name in argv else default


def init():
    """Claude Code's own `system`/`init` line. The real CLI prints nothing until the first turn arrives and then one
    of these per turn (measured); `init_at_start` in the script asks for the other ordering, where the line is already
    queued by the time the first turn is sent."""
    out({"type": "system", "subtype": "init", "model": flag("--model"), "tools": SPEC.get("tools", [])})


def deltas(d, emit, interrupted=lambda: False):
    """The turn's text, chunk by chunk, with the script's pauses. Returns what was said."""
    time.sleep(d.get("before_ms", 0) / 1000)
    if d.get("die"):
        os._exit(7)
    said = []
    for i, chunk in enumerate(d.get("deltas", ["OK"])):
        if i:
            time.sleep(d.get("gap_ms", 0) / 1000)
        if interrupted():
            break
        said.append(chunk)
        emit(chunk)
    return "".join(said)


if "--version" in sys.argv:
    print(SPEC.get("version", "9.9.9 (fake)"))
    raise SystemExit(0)

note(event="start", argv=sys.argv[1:], cwd=os.getcwd(), entries=sorted(os.listdir(".")),
     claude_env=sorted(k for k in os.environ if k.startswith("CLAUDE")))

if SPEC["protocol"] == "claude":
    if SPEC.get("init_at_start"):
        init()
    for line in sys.stdin:
        if not line.strip():
            continue
        note(event="turn", text=json.loads(line).get("message", {}).get("content"),
             system=flag("--system-prompt"), model=flag("--model"))
        d = directive()
        if not SPEC.get("init_at_start"):
            init()
        said = deltas(d, lambda c: out({"type": "stream_event", "event": {"type": "content_block_delta",
                                                                         "delta": {"type": "text_delta", "text": c}}}))
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": said}]}})
        if d.get("fail"):
            out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": d["fail"]})
        else:
            out({"type": "result", "subtype": "success", "is_error": False, "result": said})
else:
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: ([lines.put(l) for l in sys.stdin], lines.put(None)), daemon=True).start()
    n, waiting, stop = 0, [], False
    while not stop:
        msg = json.loads(waiting.pop(0)) if waiting else None
        if msg is None:
            line = lines.get()
            if line is None:
                break
            msg = json.loads(line)
    # ---- one request or notification ----
        method, mid, prm = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            note(event="initialize", client=prm.get("clientInfo"))
            out({"jsonrpc": "2.0", "id": mid, "result": {"userAgent": "fake-codex"}})
        elif method == "initialized":
            note(event="initialized")
        elif method == "model/list":
            out({"jsonrpc": "2.0", "id": mid, "result": {"data": SPEC.get("models", [])}})
        elif method == "thread/start":
            note(event="thread", system=prm.get("baseInstructions"), model=prm.get("model"), cwd=prm.get("cwd"),
                 sandbox=prm.get("sandbox"), approval=prm.get("approvalPolicy"), ephemeral=prm.get("ephemeral"))
            if SPEC.get("thread_error"):
                out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": SPEC["thread_error"]}})
            else:
                out({"jsonrpc": "2.0", "id": mid, "result": {"thread": {"id": "th1"}, "model": prm.get("model")}})
        elif method == "turn/interrupt":
            note(event="interrupt", turn=prm.get("turnId"))
            out({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "turn/start":
            note(event="turn", text=(prm.get("input") or [{}])[0].get("text"), effort=prm.get("effort"))
            d, n = directive(), n + 1
            turn_id = f"t{n}"
            out({"jsonrpc": "2.0", "id": mid, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
            cut = []

            def interrupted():
                while True:
                    try:
                        line = lines.get_nowait()
                    except queue.Empty:
                        return bool(cut)
                    if line is None:
                        return True
                    if json.loads(line).get("method") == "turn/interrupt":
                        cut.append(line)
                    else:
                        waiting.append(line)

            said = deltas(d, lambda c: out({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                                            "params": {"threadId": "th1", "turnId": turn_id, "itemId": "i1", "delta": c}}),
                          interrupted)
            for line in cut:
                waiting.append(line)
            if d.get("fail"):
                out({"jsonrpc": "2.0", "method": "error",
                     "params": {"threadId": "th1", "turnId": turn_id, "willRetry": False, "error": {"message": d["fail"]}}})
            else:
                status = "interrupted" if cut else "completed"
                out({"jsonrpc": "2.0", "method": "turn/completed",
                     "params": {"threadId": "th1", "turn": {"id": turn_id, "items": [], "status": status}}})
'''

PROMPT = "You are a phone assistant."
OTHER = "You are a phone assistant. This call: take a message."
MODELS = [{"id": "gpt-5.6-luna", "displayName": "GPT-5.6-Luna", "description": "fast", "hidden": False, "isDefault": False},
          {"id": "gpt-6-astra", "displayName": "GPT-6-Astra", "description": "the default", "hidden": False, "isDefault": True},
          {"id": "secret", "displayName": "Hidden", "description": "", "hidden": True, "isDefault": False}]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A fake CLI on disk, the home directory the session's private folder lives in, and the script it follows."""
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path / "home"))
    exe = tmp_path / "fake-cli"
    exe.write_text(FAKE_CLI)
    exe.chmod(0o755)

    class Fake:
        path = str(exe)
        spec_path = tmp_path / "script.json"
        log_path = tmp_path / "log.jsonl"

        def script(self, protocol, turns=None, **extra):
            self.spec_path.write_text(json.dumps({"protocol": protocol, "log": str(self.log_path),
                                                  "turns": turns or [{}], **extra}))
            monkeypatch.setenv("AI_PHONE_FAKE_CLI", str(self.spec_path))
            return self

        @property
        def log(self) -> list[dict]:
            return [json.loads(l) for l in self.log_path.read_text().splitlines()] if self.log_path.exists() else []

        def events(self, what: str) -> list[dict]:
            return [r for r in self.log if r["event"] == what]

        def wait(self, what: str, count: int, timeout: float = 10.0) -> list[dict]:
            """Starting a session does not wait for the CLI itself, so a test that counts sessions waits here."""
            end = time.monotonic() + timeout
            while time.monotonic() < end and len(self.events(what)) < count:
                time.sleep(0.02)
            return self.events(what)

    return Fake()


def talk(*pairs: str) -> list[dict]:
    """[system, user, assistant, user, …] the way the turn controller builds it."""
    out = [{"role": "system", "content": PROMPT}]
    for i, text in enumerate(pairs):
        out.append({"role": "user" if i % 2 == 0 else "assistant", "content": text})
    return out


def claude_brain(cli, **kw):
    kw.setdefault("first_token_timeout_ms", 4000)
    kw.setdefault("stream_idle_timeout_ms", 4000)
    return ClaudeCodeLLM(model="haiku", cli=cli.path, **kw)


def codex_brain(cli, **kw):
    kw.setdefault("first_token_timeout_ms", 4000)
    kw.setdefault("stream_idle_timeout_ms", 4000)
    return CodexLLM(model="gpt-5.6-luna", cli=cli.path, **kw)


# ---- Claude Code --------------------------------------------------------------------------------

def test_claude_runs_the_command_the_spike_proved(cli, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")  # this session's own variables must not reach the child
    cli.script("claude", [{"deltas": ["Hel", "lo."]}])
    brain = claude_brain(cli)
    try:
        assert "".join(brain.stream(1, talk("Hi"), 120)) == "Hello."
    finally:
        brain.close()
    argv = cli.events("start")[0]["argv"]
    assert argv[:3] == ["-p", "--verbose", "--no-session-persistence"]
    assert argv[argv.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"alwaysThinkingEnabled": False}
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--include-partial-messages" in argv and "--bare" not in argv
    assert argv[argv.index("--system-prompt") + 1] == PROMPT
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[-2:] == ["--tools", ""]  # last: --tools takes everything that follows it
    assert cli.events("start")[0]["claude_env"] == []
    assert cli.events("turn") == [{"event": "turn", "text": "Hi", "system": PROMPT, "model": "haiku"}]


def test_a_second_turn_in_the_same_call_sends_only_the_newest_thing_the_caller_said(cli):
    cli.script("claude", [{"deltas": ["One."]}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        assert "".join(brain.stream(1, talk("First"), 120)) == "One."
        assert "".join(brain.stream(2, talk("First", "One.", "Second"), 120)) == "Two."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1, "the session was restarted"
    assert [r["text"] for r in cli.events("turn")] == ["First", "Second"]


def test_a_reply_the_caller_talked_over_still_continues_the_session(cli):
    """The controller marks a reply it cut short; the session itself holds what it really said, so the mark is no
    reason to start again."""
    cli.script("claude", [{"deltas": ["One."]}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        list(brain.stream(1, talk("First"), 120))
        assert "".join(brain.stream(2, talk("First", "One. [interrupted]", "Second"), 120)) == "Two."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1


def test_a_window_that_has_scrolled_past_the_oldest_turns_still_continues(cli):
    cli.script("claude", [{"deltas": ["a"]}, {"deltas": ["b"]}, {"deltas": ["c"]}])
    brain = claude_brain(cli)
    try:
        list(brain.stream(1, talk("one"), 120))
        list(brain.stream(2, talk("one", "a", "two"), 120))
        # the controller keeps only the last few turns: "one"/"a" have scrolled out of the window
        assert "".join(brain.stream(3, talk("two", "b", "three"), 120)) == "c"
    finally:
        brain.close()
    assert len(cli.events("start")) == 1
    assert [r["text"] for r in cli.events("turn")] == ["one", "two", "three"]


def test_a_new_system_prompt_starts_a_new_session(cli):
    cli.script("claude", [{"deltas": ["One."]}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        list(brain.stream(1, talk("First"), 120))
        messages = [{"role": "system", "content": OTHER}, {"role": "user", "content": "Second"}]
        assert "".join(brain.stream(2, messages, 120)) == "Two."
    finally:
        brain.close()
    starts = cli.events("start")
    assert len(starts) == 2
    assert [s["argv"][s["argv"].index("--system-prompt") + 1] for s in starts] == [PROMPT, OTHER]


def test_a_history_that_is_not_a_continuation_starts_a_new_session(cli):
    cli.script("claude", [{"deltas": ["One."]}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        list(brain.stream(1, talk("First"), 120))
        assert "".join(brain.stream(2, talk("Something else", "Not ours", "Second"), 120)) == "Two."
    finally:
        brain.close()
    assert len(cli.events("start")) == 2
    assert [r["text"] for r in cli.events("turn")] == ["First", "Second"]


def test_prepare_starts_the_session_without_spending_a_turn(cli):
    cli.script("claude", [{"deltas": ["Hello."]}])
    brain = claude_brain(cli)
    try:
        brain.prepare(PROMPT)
        assert len(cli.wait("start", 1)) == 1 and cli.events("turn") == []
        assert "".join(brain.stream(1, talk("Hi"), 120)) == "Hello."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1, "the prepared session was not the one used"


def test_prepare_starts_a_fresh_session_for_the_next_call(cli):
    """The same instructions on a new call: the words of the last call must not be in the session."""
    cli.script("claude", [{"deltas": ["One."]}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        list(brain.stream(1, talk("First"), 120))
        brain.prepare(PROMPT)
        assert len(cli.wait("start", 2)) == 2
        assert "".join(brain.stream(2, talk("Second"), 120)) == "Two."
    finally:
        brain.close()
    assert len(cli.events("start")) == 2


def test_a_cancelled_turn_is_drained_before_the_next_one(cli):
    """Claude Code cannot be interrupted, so the turn runs on; the next one waits for it (turns are never mixed)."""
    cli.script("claude", [{"deltas": ["One", " more", " word."], "gap_ms": 150}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        said = []
        for piece in brain.stream(1, talk("First"), 120):
            said.append(piece)
            brain.cancel(1)
        assert said == ["One"]
        assert "".join(brain.stream(2, talk("First", "One", "Second"), 120)) == "Two."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1
    assert [r["text"] for r in cli.events("turn")] == ["First", "Second"]


def test_a_consumer_that_walks_away_mid_turn_leaves_the_session_usable(cli):
    cli.script("claude", [{"deltas": ["One", " more."], "gap_ms": 100}, {"deltas": ["Two."]}])
    brain = claude_brain(cli)
    try:
        stream = brain.stream(1, talk("First"), 120)
        assert next(stream) == "One"
        stream.close()  # the worker breaks out of the loop: the generator is closed at the yield
        assert "".join(brain.stream(2, talk("First", "One", "Second"), 120)) == "Two."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1


def test_a_reply_longer_than_the_cap_ends_the_turn(cli):
    cli.script("claude", [{"deltas": ["x" * 40] * 10}, {"deltas": ["Next."]}])
    brain = claude_brain(cli, max_response_chars=100)
    try:
        said = "".join(brain.stream(1, talk("Go on"), 120))
        assert 100 <= len(said) <= 140
        assert "".join(brain.stream(2, talk("Go on", said, "And now?"), 120)) == "Next."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1


def test_no_first_delta_within_the_deadline_is_a_timeout(cli):
    cli.script("claude", [{"before_ms": 2000, "deltas": ["Late."]}, {"deltas": ["Soon."]}])
    brain = claude_brain(cli, first_token_timeout_ms=300, drain_timeout_ms=8000)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "first_token_timeout"
        # the session survives a slow turn: the next one waits for the one still running, then answers
        assert "".join(brain.stream(2, talk("Hi", "Late.", "Again"), 120)) == "Soon."
    finally:
        brain.close()
    assert len(cli.events("start")) == 1


def test_a_turn_that_will_not_finish_in_the_drain_budget_loses_its_session(cli):
    """Claude Code cannot be interrupted, so a cancelled turn runs on. Waiting it out comes out of the time this turn
    has before the caller is apologised to, so a turn that is too slow to finish loses the session instead."""
    cli.script("claude", [{"deltas": ["One", " two", " three"], "gap_ms": 400}, {"deltas": ["Fresh."]}])
    brain = claude_brain(cli, drain_timeout_ms=100)
    try:
        for piece in brain.stream(1, talk("First"), 120):
            brain.cancel(1)
        assert "".join(brain.stream(2, talk("First", "One", "Second"), 120)) == "Fresh."
    finally:
        brain.close()
    assert len(cli.events("start")) == 2, "the session that was still talking was kept"


def test_the_drain_budget_is_the_daemon_s_to_set(cli):
    assert claude_brain(cli, drain_timeout_ms=3500).drain_s == 3.5
    assert CodexLLM(model="m").drain_s == 2.0  # the backend's own default, for anything that does not pass one


def test_a_cli_that_floods_its_stdout_is_stopped(cli):
    """`max_tokens` cannot be imposed on a CLI, so the producer stops a runaway long before the queue eats the
    worker's memory."""
    cli.script("claude", [{"deltas": ["x" * 2_000_000]}, {"deltas": ["Fresh."]}])
    brain = claude_brain(cli, max_response_chars=100)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Go on"), 120))
        assert e.value.kind == "too_long"
        assert "".join(brain.stream(2, talk("Again"), 120)) == "Fresh."  # a new session; that one is gone
    finally:
        brain.close()
    assert len(cli.events("start")) == 2


def test_a_stream_that_stops_mid_reply_is_an_idle_timeout(cli):
    cli.script("claude", [{"deltas": ["Half", " a sentence"], "gap_ms": 1500}])
    brain = claude_brain(cli, first_token_timeout_ms=4000, stream_idle_timeout_ms=300)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "idle_timeout"
    finally:
        brain.close()


def test_a_cli_that_is_not_logged_in_says_so(cli):
    cli.script("claude", [{"deltas": [], "fail": "Not logged in · run claude and /login"}])
    brain = claude_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "not_logged_in"
    finally:
        brain.close()


def test_a_cli_that_is_not_installed_says_so(cli, tmp_path):
    cli.script("claude")
    for brain in (ClaudeCodeLLM(model="haiku", cli=str(tmp_path / "no-such-cli")), CodexLLM(model="m", cli="no-such-cli-at-all")):
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "cli_missing"
        assert brain.cli in str(e.value)


def test_a_session_whose_process_died_fails_the_turn_and_the_next_one_restarts_it(cli):
    cli.script("claude", [{"die": True}, {"deltas": ["Back."]}])
    brain = claude_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "failed"
        assert "".join(brain.stream(2, talk("Hi", "", "Again"), 120)) == "Back."
    finally:
        brain.close()
    assert len(cli.events("start")) == 2


def test_an_init_line_queued_before_the_turn_is_read_in_its_turn(cli):
    """prepare() starts the CLI, so anything it prints is waiting on the queue when the first turn is sent. Whether
    the init line comes then or with the turn, the reply is the same and tools are still refused."""
    cli.script("claude", [{"deltas": ["Hello."]}], init_at_start=True)
    brain = claude_brain(cli)
    try:
        brain.prepare(PROMPT)
        cli.wait("start", 1)
        assert "".join(brain.stream(1, talk("Hi"), 120)) == "Hello."
    finally:
        brain.close()
    cli.script("claude", [{"deltas": ["Hello."]}], init_at_start=True, tools=["Bash"])
    brain = claude_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "failed" and "tools" in str(e.value)
    finally:
        brain.close()


def test_a_cli_that_came_up_with_tools_of_its_own_is_refused(cli):
    cli.script("claude", [{"deltas": ["Hello."]}], tools=["Bash", "Read"])
    brain = claude_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "failed" and "tools" in str(e.value)
    finally:
        brain.close()


def test_the_session_runs_in_a_private_empty_directory(cli, tmp_path):
    cli.script("claude")
    brain = claude_brain(cli)
    try:
        brain.prepare(PROMPT)
    finally:
        brain.close()
    where = Path(cli.events("start")[0]["cwd"]).resolve()
    assert where == (tmp_path / "home" / "brain").resolve()
    assert cli.events("start")[0]["entries"] == [], "a CLAUDE.md or AGENTS.md in there would be read"
    assert stat.S_IMODE(where.stat().st_mode) == 0o700
    # this is often the first thing to create the ai-phone home (a plan is probed before any config is written), so
    # the home must not be left at the umask's 0755 beside the config and the secrets file
    assert stat.S_IMODE(where.parent.stat().st_mode) == 0o700


def test_warmup_proves_the_login_and_reports_the_model_and_version(cli):
    cli.script("claude", [{"deltas": ["OK"]}], version="2.1.286 (Claude Code)")
    brain = claude_brain(cli)
    try:
        t = brain.warmup(PROMPT)
    finally:
        brain.close()
    assert t["model"] == "haiku" and t["cli_version"] == "2.1.286 (Claude Code)" and t["warm_ms"] > 0
    assert [r["text"] for r in cli.events("turn")] == ["Say OK."]


def test_a_warmup_that_is_not_logged_in_says_so(cli):
    cli.script("claude", [{"deltas": [], "fail": "Not logged in"}])
    brain = claude_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            brain.warmup(PROMPT)
        assert e.value.kind == "not_logged_in"
    finally:
        brain.close()


def test_the_probe_turn_is_never_part_of_a_call(cli):
    cli.script("claude", [{"deltas": ["OK"]}, {"deltas": ["Hello."]}])
    brain = claude_brain(cli)
    try:
        brain.warmup(PROMPT)
        assert "".join(brain.stream(1, talk("Hi"), 120)) == "Hello."
    finally:
        brain.close()
    starts, turns = cli.events("start"), cli.events("turn")
    assert len(starts) == 2 and [r["text"] for r in turns] == ["Say OK.", "Hi"]


def test_nothing_anyone_said_reaches_the_log(cli, caplog):
    cli.script("claude", [{"deltas": ["A secret the caller told us."]}])
    brain = claude_brain(cli)
    with caplog.at_level(logging.DEBUG, logger="ai-phone"):
        try:
            said = "".join(brain.stream(1, talk("My card number is 4111 1111 1111 1111"), 120))
        finally:
            brain.close()
    assert said and "4111" not in caplog.text and "secret" not in caplog.text


def test_timings_are_reported_for_the_turn_log(cli):
    cli.script("claude", [{"deltas": ["One", " two"], "before_ms": 60}])
    brain = claude_brain(cli)
    try:
        list(brain.stream(1, talk("Hi"), 120))
    finally:
        brain.close()
    t = brain.last_timings
    assert t["tokens"] == 2 and t["first_token_ms"] >= 60 and t["gen_ms"] >= 0


# ---- Codex -------------------------------------------------------------------------------------

def test_codex_speaks_the_handshake_and_one_turn_per_message(cli):
    cli.script("codex", [{"deltas": ["Hel", "lo."]}, {"deltas": ["Again."]}])
    brain = codex_brain(cli)
    try:
        assert "".join(brain.stream(1, talk("Hi"), 120)) == "Hello."
        assert "".join(brain.stream(2, talk("Hi", "Hello.", "More"), 120)) == "Again."
    finally:
        brain.close()
    assert [r["event"] for r in cli.log][:4] == ["start", "initialize", "initialized", "thread"]
    assert cli.events("initialize")[0]["client"]["name"] == "ai-phone"
    thread = cli.events("thread")[0]
    assert thread["system"] == PROMPT and thread["model"] == "gpt-5.6-luna" and thread["ephemeral"] is True
    assert thread["sandbox"] == "read-only" and thread["approval"] == "never"
    argv = cli.events("start")[0]["argv"]
    assert argv[0] == "app-server"
    assert "mcp_servers={}" in argv, "the person's own MCP servers would start with every thread"
    assert 'model_reasoning_effort="low"' in argv
    assert [r["text"] for r in cli.events("turn")] == ["Hi", "More"]
    assert len(cli.events("thread")) == 1


def test_codex_asks_for_the_model_every_time_because_the_saved_default_may_not_be_allowed(cli):
    cli.script("codex", [{"deltas": ["Hi."]}])
    brain = codex_brain(cli)
    try:
        list(brain.stream(1, talk("Hi"), 120))
    finally:
        brain.close()
    assert cli.events("thread")[0]["model"] == "gpt-5.6-luna"


def test_codex_interrupts_a_cancelled_turn(cli):
    cli.script("codex", [{"deltas": ["One", " more", " word."], "gap_ms": 150}, {"deltas": ["Two."]}])
    brain = codex_brain(cli)
    try:
        said = []
        for piece in brain.stream(1, talk("First"), 120):
            said.append(piece)
            brain.cancel(1)
        assert said == ["One"]
        assert "".join(brain.stream(2, talk("First", "One", "Second"), 120)) == "Two."
    finally:
        brain.close()
    assert cli.events("interrupt") and cli.events("interrupt")[0]["turn"] == "t1"
    assert len(cli.events("thread")) == 1


def test_codex_restarts_its_thread_for_a_new_system_prompt(cli):
    cli.script("codex", [{"deltas": ["One."]}, {"deltas": ["Two."]}])
    brain = codex_brain(cli)
    try:
        list(brain.stream(1, talk("First"), 120))
        list(brain.stream(2, [{"role": "system", "content": OTHER}, {"role": "user", "content": "Second"}], 120))
    finally:
        brain.close()
    assert [r["system"] for r in cli.events("thread")] == [PROMPT, OTHER]
    assert len(cli.events("start")) == 2


def test_codex_prepare_starts_the_thread_without_a_turn(cli):
    cli.script("codex", [{"deltas": ["Hello."]}])
    brain = codex_brain(cli)
    try:
        brain.prepare(PROMPT)
        assert len(cli.wait("thread", 1)) == 1 and cli.events("turn") == []
        assert "".join(brain.stream(1, talk("Hi"), 120)) == "Hello."
    finally:
        brain.close()
    assert len(cli.events("thread")) == 1


def test_codex_model_that_the_plan_does_not_allow_says_which_one(cli):
    cli.script("codex", [{"deltas": [], "fail": "400 model is not supported when using Codex with a ChatGPT account"}])
    brain = codex_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "model_not_allowed" and "gpt-5.6-luna" in str(e.value)
    finally:
        brain.close()


def test_codex_that_is_not_logged_in_says_so(cli):
    cli.script("codex", [{"deltas": [], "fail": "401 Unauthorized: please run codex login"}])
    brain = codex_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "not_logged_in"
    finally:
        brain.close()


def test_codex_whose_thread_cannot_start_fails_the_turn(cli):
    cli.script("codex", thread_error="401 Unauthorized")
    brain = codex_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, talk("Hi"), 120))
        assert e.value.kind == "not_logged_in"
    finally:
        brain.close()


def test_codex_warmup_proves_the_login(cli):
    cli.script("codex", [{"deltas": ["OK"]}], version="codex-cli 0.157.1")
    brain = codex_brain(cli)
    try:
        t = brain.warmup(PROMPT)
    finally:
        brain.close()
    assert sorted(t) == ["cli_version", "model", "warm_ms"]
    assert t["model"] == "gpt-5.6-luna" and t["cli_version"] == "codex-cli 0.157.1" and t["warm_ms"] > 0
    assert [r["text"] for r in cli.events("turn")] == ["Say OK."]


def test_codex_models_are_the_ones_the_plan_lists(cli):
    cli.script("codex", models=MODELS)
    found = codex_models(cli=cli.path)
    assert [m.id for m in found] == ["gpt-5.6-luna", "gpt-6-astra"], "a hidden model is not offered"
    assert found[0].name == "GPT-5.6-Luna" and found[1].note == "the default"
    assert cli.events("turn") == [], "listing the models never spends a turn"


def test_models_from_a_cli_that_is_not_installed_say_so(cli):
    cli.script("codex", models=MODELS)
    with pytest.raises(BrainError) as e:
        codex_models(cli="no-such-cli-at-all")
    assert e.value.kind == "cli_missing"


def test_the_factories_build_what_the_daemon_asks_for(cli):
    claude = make_claude(model="sonnet", cli=cli.path, first_token_timeout_ms=12000, stream_idle_timeout_ms=4000,
                         max_response_chars=2000)
    codex = make_codex(model="gpt-6-astra", cli=None, first_token_timeout_ms=12000, stream_idle_timeout_ms=4000,
                       max_response_chars=2000)
    assert isinstance(claude, ClaudeCodeLLM) and claude.model == "sonnet" and claude.first_token_s == 12.0
    assert isinstance(codex, CodexLLM) and codex.model == "gpt-6-astra" and codex.cli == "codex"
    # a subscription brain's own default deadline is the one a plan needs, not the four seconds a local model gets
    assert CodexLLM(model="m").first_token_s == 12.0 and ClaudeCodeLLM(model="haiku").first_token_s == 12.0
    claude.close()
    codex.close()


def test_a_turn_with_nothing_for_the_caller_to_say_is_refused(cli):
    cli.script("claude")
    brain = claude_brain(cli)
    try:
        with pytest.raises(BrainError) as e:
            list(brain.stream(1, [{"role": "system", "content": PROMPT}], 120))
        assert e.value.kind == "malformed"
        assert cli.events("start") == [], "no session is started for a turn that cannot be sent"
    finally:
        brain.close()


def test_closing_the_brain_ends_the_cli(cli):
    cli.script("claude")
    brain = claude_brain(cli)
    brain.prepare(PROMPT)
    proc = brain._proc
    assert os.getpgid(proc.pid) != os.getpgid(0), "a session of its own, so closing it takes its children too"
    brain.close()
    assert proc.poll() is not None, "the CLI is still running"
    assert brain._proc is None and brain.close() is None  # closing twice is how the worker shuts down
