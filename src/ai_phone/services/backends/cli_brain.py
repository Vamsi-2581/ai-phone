"""A person's own Claude or ChatGPT plan as the brain, through the CLI they are already logged into.

`ClaudeCodeLLM` drives `claude -p` in stream-json mode; `CodexLLM` drives `codex app-server`, which speaks JSON-RPC
2.0 over stdio. Neither needs an API key: the CLI uses the login the person already has, and the plan's limits are
the limits.

One session per call. The session remembers the instructions it was started with and the conversation it has been
given, so the next turn of the same call sends only the newest thing the caller said; different instructions, or a
history that is not the continuation of what this session has heard, start a new session. `prepare()` starts the
session for a call before the greeting plays, so the first reply does not pay the startup.

Failures raise BrainError with a kind the controller logs, exactly like the endpoint brain; there are no retries,
because the caller is waiting on the line. What leaves this Mac is the instructions, what callers say and the
conversation (not audio), through the person's own CLI to their provider. Nothing here logs any of it: only timings,
the model, and whatever the CLI itself writes to stderr, at debug level and truncated.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ai_phone import paths
from ai_phone.services.errors import BrainError

if TYPE_CHECKING:  # only for the annotations: the setup wizard imports this module and needs no array library
    from ai_phone.services.base import Message

log = logging.getLogger("ai-phone")

PROBE = "Say OK."              # the one tiny turn warm-up spends to prove the login works
PROBE_PROMPT = "You are a phone assistant. Answer in one short sentence."
CODEX_EFFORT = "low"           # measured: the first word takes about four seconds at any reasoning effort, so take
                               # the one every current model accepts rather than the person's own configured default
DRAIN_SEC = 10.0               # how long a turn we stopped reading may take to finish before the session is dropped
HANDSHAKE_SEC = 10.0           # how long starting a session may take, when that is longer than the token deadline
CLOSE_SEC = 2.0                # how long the CLI may take to exit after its stdin closes
VERSION_SEC = 15.0             # how long `<cli> --version` may take
STDERR_CHARS = 400             # how much of one of the CLI's own stderr lines reaches the log


@dataclass(frozen=True)
class CliModel:
    """A model the person's plan offers, for the setup wizard's menu."""
    id: str
    name: str
    note: str


# Claude Code takes an alias or a full model id; these three are what setup offers (the measured first-word times are
# in README "Any brain").
CLAUDE_MODELS = (CliModel("haiku", "Haiku", "fastest to answer · the one for a phone call"),
                 CliModel("sonnet", "Sonnet", "thinks more, about two seconds slower to the first word"),
                 CliModel("opus", "Opus", "the strongest, and the slowest to start talking"))


def _client_version() -> str:
    try:
        from importlib.metadata import version
        return version("ai-phone")
    except Exception:
        return "0"


def _split(messages: list[Message]) -> tuple[str, list[Message], Message]:
    """The instructions, what the session has already been given, and the newest thing the caller said."""
    system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
    rest = [m for m in messages if m.get("role") != "system"]
    if not rest or rest[-1].get("role") != "user":
        raise BrainError("malformed", "the newest message is not the caller's, so there is no turn to send")
    return system, rest[:-1], rest[-1]


def _same(sent: Message, shown: Message) -> bool:
    """Is `shown` (what the controller shows now) the message this session was already given?

    A reply the caller talked over comes back marked, and a long one comes back trimmed, so an assistant turn is
    matched by its place in the conversation: the session itself holds what it really said.
    """
    if sent.get("role") != shown.get("role"):
        return False
    return sent.get("role") == "assistant" or sent.get("content") == shown.get("content")


def _pump(stream, q: queue.Queue) -> None:
    """Every line the CLI prints, onto the queue, until it stops."""
    try:
        for line in stream:
            q.put(("line", line))
    except (OSError, ValueError):
        pass
    q.put(("eof", None))


def _pump_stderr(stream, name: str) -> None:
    try:
        for line in stream:
            line = line.strip()
            if line:
                log.debug("%s: %s", name, line[:STDERR_CHARS])
    except (OSError, ValueError):
        pass


class CliBrain:
    """The part both CLIs share: the process, the deadlines, and what makes a session reusable.

    A subclass says how to start the CLI (`_args`, `_opened`), how to send a turn (`_send`), what a line of its
    output means (`_event`) and how to stop a turn early (`_interrupt`).
    """

    cli_name = ""      # the executable to look for on PATH
    provider = ""      # who the words reach, for the daemon's start-up warning

    def __init__(self, model: str, cli: str | None = None, first_token_timeout_ms: int = 12000,
                 stream_idle_timeout_ms: int = 4000, max_response_chars: int = 2000):
        self.model = model
        self.cli = cli or self.cli_name
        self.first_token_s = first_token_timeout_ms / 1000
        self.idle_s = stream_idle_timeout_ms / 1000
        self.max_chars = max_response_chars
        self.last_timings: dict = {}
        self._cancelled: set[int] = set()
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue | None = None
        self._system: str | None = None    # the instructions this session was started with
        self._seen: list[Message] = []     # the conversation this session has been given, as the controller gave it
        self._in_flight = False            # a turn we stopped reading: drain it before sending the next one

    # ---- the brain protocol ----------------------------------------------------------------------
    def warmup(self, system_prompt: str = "") -> dict:
        """Prove the CLI is installed and the person is logged in, with one tiny turn. The probe is thrown away with
        its session, so no call inherits it."""
        t = time.monotonic_ns()
        version = self.cli_version()
        self.close()
        try:
            said = "".join(self.stream(0, [{"role": "system", "content": system_prompt or PROBE_PROMPT},
                                           {"role": "user", "content": PROBE}], 16))
        finally:
            self.close()
        if not said.strip():
            raise BrainError("failed", f"{self.cli} answered nothing")
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6, "model": self.model, "cli_version": version}

    def prepare(self, system_prompt: str) -> None:
        """Have a session ready for a call with these instructions, without spending a turn. A call always gets a
        session of its own: the same instructions on a new call still start again, so nothing carries over."""
        if self._alive() and self._system == system_prompt and not self._seen and not self._in_flight:
            return
        self.close()
        self._start(system_prompt)

    def stream(self, gen_id: int, messages: list[Message], max_tokens: int):
        """One turn. `max_tokens` cannot be imposed on a CLI, so a reply past max_response_chars ends the turn
        instead; the first delta must arrive within first_token_timeout_ms and each later one within
        stream_idle_timeout_ms."""
        system, prior, newest = _split(messages)
        proc = None
        t0, t_first, n, said, done = time.monotonic_ns(), None, 0, [], False
        try:
            if gen_id in self._cancelled:
                return  # cancelled before it began: never spend a turn of the person's plan on it
            self._begin(system, prior, newest)
            proc = self._proc
            deadline = time.monotonic() + self.first_token_s
            while True:
                if gen_id in self._cancelled:
                    return
                what, value = self._event(self._take(deadline - time.monotonic(),
                                                     "first_token_timeout" if t_first is None else "idle_timeout"))
                if what == "end":
                    done = True
                    return
                if what == "error":
                    done = True
                    raise value
                if what != "delta" or not value:
                    continue
                if t_first is None:
                    t_first = time.monotonic_ns()
                n += 1
                said.append(value)
                deadline = time.monotonic() + self.idle_s
                yield value
                if sum(len(s) for s in said) >= self.max_chars:
                    log.info("%s brain: the reply passed %d characters; ending the turn", self.cli_name, self.max_chars)
                    return
        finally:
            if proc is not None and self._proc is proc:
                self._in_flight = not done
                if not done:
                    self._interrupt()
                self._seen.append({"role": "assistant", "content": "".join(said)})
            t_end = time.monotonic_ns()
            first = t_first or t_end
            self.last_timings = {"prompt_eval_ms": (first - t0) / 1e6, "first_token_ms": (first - t0) / 1e6,
                                 "gen_ms": (t_end - first) / 1e6, "tokens": n}
            self._cancelled.discard(gen_id)

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)

    def close(self) -> None:
        """End the session. The CLI exits when its stdin closes; one that does not is killed."""
        p, self._proc, self._q, self._system = self._proc, None, None, None
        self._seen, self._in_flight = [], False
        if p is None:
            return
        try:
            if p.stdin is not None:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=CLOSE_SEC)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=CLOSE_SEC)
            except subprocess.TimeoutExpired:
                pass
        for pipe in (p.stdout, p.stderr):  # the pump threads see the end of their stream and stop
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    # ---- sessions --------------------------------------------------------------------------------
    def _begin(self, system: str, prior: list[Message], newest: Message) -> None:
        if self._alive() and system == self._system and self._continues(prior) and self._settle():
            self._send(str(newest.get("content") or ""))
            self._seen.append(dict(newest))
            return
        self.close()
        self._start(system)
        self._send(str(newest.get("content") or ""))
        self._seen = [dict(newest)]

    def _continues(self, prior: list[Message]) -> bool:
        """Is this the next turn of the conversation this session has had? The controller keeps only the last few
        turns, so what it shows is the end of what we sent, not all of it."""
        if len(prior) > len(self._seen):
            return False
        if not prior:
            return not self._seen  # nothing sent yet: the first turn of a session that is waiting for one
        tail = self._seen[len(self._seen) - len(prior):]
        return all(_same(sent, shown) for sent, shown in zip(tail, prior))

    def _settle(self) -> bool:
        """Let a turn we stopped reading finish before the next one starts, so two are never in flight (Claude Code
        cannot be interrupted at all). False means it never finished and this session is no good."""
        if not self._in_flight:
            return True
        end = time.monotonic() + DRAIN_SEC
        try:
            while True:
                what, _ = self._event(self._take(end - time.monotonic(), "idle_timeout"))
                if what in ("end", "error"):
                    self._in_flight = False
                    return True
        except BrainError:
            return False

    def _start(self, system_prompt: str | None) -> None:
        self._spawn(system_prompt)
        t = time.monotonic_ns()
        try:
            self._opened()
        except BrainError:
            self.close()
            raise
        log.info("%s brain session ready in %.0f ms model=%s", self.cli_name, (time.monotonic_ns() - t) / 1e6, self.model)

    def _spawn(self, system_prompt: str | None) -> None:
        exe = self._executable()
        try:
            p = subprocess.Popen([exe, *self._args(system_prompt or "")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, bufsize=1, env=self._env(), cwd=str(self._workdir()))
        except OSError as e:
            raise BrainError("cli_missing", f"{self.cli} would not start: {type(e).__name__}") from None
        self._proc, self._q, self._system, self._seen, self._in_flight = p, queue.Queue(), system_prompt, [], False
        threading.Thread(target=_pump, args=(p.stdout, self._q), name=f"{self.cli_name}-brain", daemon=True).start()
        threading.Thread(target=_pump_stderr, args=(p.stderr, self.cli_name), name=f"{self.cli_name}-brain-err",
                         daemon=True).start()

    def _executable(self) -> str:
        found = shutil.which(self.cli)
        if found is None:
            raise BrainError("cli_missing", f"{self.cli} is not installed (not on PATH)")
        return found

    def _workdir(self) -> Path:
        """An empty private directory of our own: nothing of the person's (a CLAUDE.md, an AGENTS.md, project
        settings) is read from the place a session runs in."""
        d = paths.home() / "brain"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(d.stat().st_mode) != 0o700:
            d.chmod(0o700)
        return d

    def _env(self) -> dict[str, str]:
        return dict(os.environ)

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def cli_version(self) -> str:
        exe = self._executable()
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=VERSION_SEC,
                               env=self._env(), cwd=str(self._workdir()))
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        said = (r.stdout or r.stderr or "").strip()
        return said.splitlines()[0][:60] if said else "unknown"

    # ---- talking to the CLI ----------------------------------------------------------------------
    def _write(self, line: str) -> None:
        p = self._proc
        if p is None or p.stdin is None:
            raise BrainError("failed", f"{self.cli} has no session")
        try:
            p.stdin.write(line + "\n")
            p.stdin.flush()
        except (OSError, ValueError):
            self.close()
            raise BrainError("failed", f"{self.cli} stopped") from None

    def _take(self, timeout_s: float, on_timeout: str) -> dict:
        """The next object the CLI printed. A line that is not JSON is not protocol, so it counts as nothing."""
        q = self._q
        if q is None:
            raise BrainError("failed", f"{self.cli} has no session")
        try:
            kind, payload = q.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            raise BrainError(on_timeout) from None
        if kind == "eof":
            self.close()
            raise BrainError("failed", f"{self.cli} stopped")
        try:
            obj = json.loads(payload)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    # ---- what a subclass provides ----------------------------------------------------------------
    def _args(self, system_prompt: str) -> list[str]:
        raise NotImplementedError

    def _opened(self) -> None:
        """Whatever the protocol needs before the first turn."""

    def _send(self, text: str) -> None:
        raise NotImplementedError

    def _event(self, obj: dict) -> tuple[str, object]:
        """One object from the CLI as ("delta", text) | ("end", None) | ("error", BrainError) | ("note", None)."""
        raise NotImplementedError

    def _interrupt(self) -> None:
        """Stop the turn that is running, if the protocol can."""


class ClaudeCodeLLM(CliBrain):
    """Claude Code in print mode with stream-json on both sides: one process for the call, one line per turn.

    `--bare` must not be used (it skips the keychain login) and every CLAUDE* variable leaves the child's
    environment, or the CLI takes itself for a nested session.
    """

    cli_name = "claude"
    provider = "Anthropic"

    def _args(self, system_prompt: str) -> list[str]:
        return ["-p", "--verbose", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--settings", json.dumps({"alwaysThinkingEnabled": False}),
                "--input-format", "stream-json", "--output-format", "stream-json", "--include-partial-messages",
                "--system-prompt", system_prompt, "--model", self.model,
                "--tools", ""]  # last: --tools takes every argument that follows it

    def _env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

    def _send(self, text: str) -> None:
        self._write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}))

    def _event(self, obj: dict) -> tuple[str, object]:
        what = obj.get("type")
        if what == "stream_event":
            delta = (obj.get("event") or {}).get("delta") or {}
            text = delta.get("text")
            if delta.get("type") == "text_delta" and isinstance(text, str):
                return "delta", text
            return "note", None
        if what == "result":
            if obj.get("is_error"):
                said = " ".join(str(obj.get("result") or obj.get("subtype") or "").split())[:160]
                low = said.lower()
                if "not logged in" in low or "log in" in low or "/login" in low:
                    return "error", BrainError("not_logged_in", "claude is not logged in")
                return "error", BrainError("failed", said)
            return "end", None
        if what == "system" and obj.get("subtype") == "init":
            if obj.get("tools"):
                return "error", BrainError("failed", "the CLI came up with tools of its own; a phone brain gets none")
        return "note", None


class CodexLLM(CliBrain):
    """`codex app-server`: JSON-RPC 2.0 over stdio, one ephemeral thread per call.

    The model is always named, because the person's own default may be one their plan does not allow; their MCP
    servers are kept out of every thread, because they start with it and cost a second.
    """

    cli_name = "codex"
    provider = "OpenAI"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._id = 0
        self._thread: str | None = None
        self._turn_req: int | None = None
        self._turn_id: str | None = None

    def _args(self, system_prompt: str) -> list[str]:
        return ["app-server", "-c", "mcp_servers={}", "-c", f'model_reasoning_effort="{CODEX_EFFORT}"']

    def _opened(self) -> None:
        self._handshake()
        r = self._await(self._request("thread/start", {"ephemeral": True, "cwd": str(self._workdir()),
                                                      "baseInstructions": self._system or PROBE_PROMPT,
                                                      "sandbox": "read-only", "approvalPolicy": "never",
                                                      "model": self.model}))
        self._thread = ((r.get("result") or {}).get("thread") or {}).get("id")
        self._turn_req = self._turn_id = None
        if not self._thread:
            raise BrainError("failed", "codex started no thread")

    def _handshake(self) -> None:
        self._await(self._request("initialize", {"clientInfo": {"name": "ai-phone", "version": _client_version()}}))
        self._notify("initialized", {})

    def list_models(self) -> list[CliModel]:
        """The models this plan allows. No thread is started, so no turn is spent."""
        self.close()
        self._spawn(None)
        try:
            self._handshake()
            r = self._await(self._request("model/list", {}))
            data = (r.get("result") or {}).get("data") or []
            return [CliModel(m["id"], str(m.get("displayName") or m["id"]), str(m.get("description") or ""))
                    for m in data if isinstance(m, dict) and m.get("id") and not m.get("hidden")]
        finally:
            self.close()

    def _request(self, method: str, params: dict) -> int:
        self._id += 1
        self._write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}))
        return self._id

    def _notify(self, method: str, params: dict) -> None:
        self._write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def _await(self, rid: int) -> dict:
        end = time.monotonic() + max(self.first_token_s, HANDSHAKE_SEC)
        while True:
            obj = self._take(end - time.monotonic(), "first_token_timeout")
            if obj.get("id") == rid:
                if obj.get("error"):
                    raise self._failure(obj["error"])
                return obj
            what, value = self._event(obj)
            if what == "error":
                raise value

    def _send(self, text: str) -> None:
        self._turn_id = None
        self._turn_req = self._request("turn/start", {"threadId": self._thread, "input": [{"type": "text", "text": text}]})

    def _event(self, obj: dict) -> tuple[str, object]:
        method, prm = obj.get("method"), obj.get("params") or {}
        if method == "item/agentMessage/delta":
            return "delta", prm.get("delta") or ""
        if method == "turn/completed":
            turn = prm.get("turn") or {}
            status = turn.get("status")
            if status in ("completed", "interrupted"):  # interrupted is our own doing: the turn is over either way
                return "end", None
            return "error", self._failure(turn.get("error") or {"message": f"the turn {status}"})
        if method == "error":
            return "error", self._failure(prm.get("error") or {})
        if obj.get("id") is not None and obj.get("id") == self._turn_req:
            if obj.get("error"):
                return "error", self._failure(obj["error"])
            self._turn_id = ((obj.get("result") or {}).get("turn") or {}).get("id")
        return "note", None

    def _failure(self, err: dict) -> BrainError:
        said = " ".join(str(err.get("message") if isinstance(err, dict) else err or "").split())[:200]
        low = said.lower()
        if "not supported when using codex with a chatgpt account" in low:
            return BrainError("model_not_allowed", f"{self.model} is not on this plan")
        if any(w in low for w in ("401", "unauthorized", "not logged in", "codex login", "log in", "authenticat")):
            return BrainError("not_logged_in", "codex is not logged in")
        return BrainError("failed", said)

    def _interrupt(self) -> None:
        if self._thread and self._turn_id:
            try:
                self._request("turn/interrupt", {"threadId": self._thread, "turnId": self._turn_id})
            except BrainError:
                pass  # a session that will not take an interrupt is dropped by the next turn anyway


def codex_models(cli: str | None = None) -> list[CliModel]:
    """What `ai-phone setup` offers as the ChatGPT brain's model: the models this plan lists."""
    return CodexLLM(model="", cli=cli).list_models()


def make_claude(**kw) -> ClaudeCodeLLM:
    return ClaudeCodeLLM(**kw)


def make_codex(**kw) -> CodexLLM:
    return CodexLLM(**kw)
