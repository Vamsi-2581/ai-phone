"""`ai-phone call | listen | say | hangup`: the agent tool. Every command talks to the running daemon; `--json`
prints exactly one JSON object and nothing else."""
from __future__ import annotations

import argparse
import json
import signal
import socket
import threading
from pathlib import Path

from ai_phone import paths
from ai_phone.control.client import DaemonGone, probe, request
from ai_phone.control.protocol import EXIT, MAX_PROMPT_CHARS, MAX_SAY_CHARS

ENDED_BY = {"agent": "hung up by agent", "them": "hung up by them", "timeout": "no reply from the calling AI in time",
            "silence": "ended after silence", "max_duration": "reached the time limit", "error": "ended by an error",
            "hangup_command": "hung up on request"}
MAX_WAIT_SEC = 3600  # the daemon refuses longer ring and listen timeouts
INTERRUPTED = 130    # the shell's code for a command stopped by Ctrl-C (128 + SIGINT)


def format_result(r: dict) -> str:
    parts = [r.get("outcome") or "?"]
    if r.get("ended_by"):
        parts.append(ENDED_BY.get(r["ended_by"], r["ended_by"]))
    if r.get("duration_sec") is not None:
        m, s = divmod(int(round(r["duration_sec"])), 60)
        parts.append(f"{m}m{s:02d}s" if m else f"{s}s")
    if r.get("detail"):
        parts.append(r["detail"])
    lines = [f"call {r.get('call_id')} ended: {', '.join(parts)}"]
    for t in r.get("transcript") or []:
        label = "agent:" if t["who"] == "agent" else "them: "
        lines.append(f"  {label} {t['text']}" + (" [interrupted]" if t.get("interrupted") else ""))
    return "\n".join(lines)


def _emit(a, obj: dict, text: str) -> None:
    print(json.dumps(obj) if a.json else text, flush=True)


def _fail(a, error: str, code: int) -> int:
    _emit(a, {"ok": False, "error": error, "code": code}, f"error: {error}")
    return code


def _alive(a) -> bool:
    if probe(paths.socket_path()):
        return True
    _fail(a, "ai-phone is not running (start it with `ai-phone run`)", EXIT["daemon_gone"])
    return False


def _ask(a, msg: dict, timeout: float | None = None) -> dict | int:
    """The daemon's reply, or the exit code of a failure that has already been reported."""
    try:
        return request(paths.socket_path(), msg, timeout=timeout)
    except (DaemonGone, OSError) as e:
        return _fail(a, f"daemon went away: {e}", EXIT["daemon_gone"])
    except KeyboardInterrupt:  # closing the connection cancels a listen; a say or hangup carries on in the daemon
        return _fail(a, "interrupted", INTERRUPTED)


def _refused(a, reply: dict) -> int:
    code = reply.get("code") or (EXIT["busy"] if reply.get("error") == "busy" else EXIT["rejected"])
    return _fail(a, reply.get("error", "refused"), int(code))


# ---- call ---------------------------------------------------------------------------------------------
def cmd_call(a) -> int:
    if a.persona is not None:
        return _fail(a, "--persona was removed; describe the call with --prompt", EXIT["usage"])
    prompt = a.prompt
    if a.prompt_file:
        if prompt is not None:
            return _fail(a, "give either --prompt or --prompt-file, not both", EXIT["usage"])
        try:
            prompt = Path(a.prompt_file).expanduser().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            return _fail(a, f"cannot read --prompt-file {a.prompt_file}: {getattr(e, 'strerror', None) or e}",
                         EXIT["usage"])
    if prompt is not None and len(prompt) > MAX_PROMPT_CHARS:
        return _fail(a, f"--prompt is longer than {MAX_PROMPT_CHARS} characters", EXIT["usage"])
    if a.relay and prompt:
        return _fail(a, "--prompt has no effect with --relay", EXIT["usage"])
    to = a.target
    if paths.config_path().exists() and not a.yes and not to.startswith("sip:"):
        from ai_phone.config import load_config
        try:
            cfg = load_config(paths.config_path())
        except (OSError, ValueError) as e:  # ConfigError is a ValueError
            return _fail(a, str(e), EXIT["usage"])
        # a diverted number rings the agent whichever line dials it, so check the named line or, without --line, every line
        lines = [cfg.lines[a.line]] if a.line and a.line in cfg.lines else list(cfg.lines.values())
        hit = next((ln for ln in lines if to in ln.diverted_from), None)
        if hit is not None:
            return _fail(a, f"{to} diverts to this agent ({hit.number}); calling it would ring the agent itself. "
                            "Pass --yes to do it anyway.", EXIT["rejected"])
    if not _alive(a):
        return EXIT["daemon_gone"]

    def on_event(e: dict) -> None:
        if a.json:
            return
        kind = e.get("event")
        if kind == "turn":
            print(f"turn {e.get('turn')}: first audible {e.get('first_audible_ms')} ms", flush=True)
        elif kind == "created":
            print(f"call {e.get('call_id')}", flush=True)
        elif kind not in ("ended",) and "ok" not in e:
            print(f"{kind} {e.get('uri', '')}".strip(), flush=True)

    holder: dict = {}
    interrupted = {"flag": False}

    def on_sigint(*_):
        """Hang up, give the daemon a moment to report `ended`, then stop waiting whatever happens."""
        interrupted["flag"] = True
        try:
            request(paths.socket_path(), {"cmd": "hangup"}, timeout=2)
        except DaemonGone:
            pass

        def cut():
            sock = holder.get("socket")
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(5.0, cut)
        timer.daemon = True  # never keeps the process alive
        holder["timer"] = timer
        timer.start()

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        last = request(paths.socket_path(), {"cmd": "call", "to": to, "line": a.line, "timeout_sec": a.timeout_sec,
                                             "prompt": prompt, "relay": bool(a.relay)}, on_event, socket_holder=holder)
    except (DaemonGone, OSError) as e:
        if interrupted["flag"]:
            return _fail(a, "hung up; the daemon did not confirm within 5 s", EXIT["daemon_gone"])
        return _fail(a, f"daemon went away: {e}", EXIT["daemon_gone"])
    finally:
        signal.signal(signal.SIGINT, previous)
        timer = holder.get("timer")
        if timer is not None:
            timer.cancel()  # `ended` arrived (or the request failed): nothing left to cut
    if last.get("ok") is False:
        return _refused(a, last)
    if last.get("event") == "answered":
        _emit(a, {"ok": True, "call_id": last["call_id"], "mode": "relay", "outcome": "answered"},
              f"call {last['call_id']} answered")
        return 0
    code = int(last.get("exit_code", EXIT["rejected"]))
    result = last.get("result")
    if code == 0 and result:
        _emit(a, {"ok": True, **result}, format_result(result))
        return code
    # every other ending has one shape: ok, outcome, detail and code, plus the call record when there is one
    record = result or {}
    outcome = record.get("outcome") or last.get("reason")
    detail = record.get("detail") or last.get("detail") or ""
    _emit(a, {**record, "ok": code == 0, "outcome": outcome, "detail": detail, "code": code},
          format_result(result) if result else f"ended: {outcome}" + (f" ({detail})" if detail else ""))
    return code


# ---- listen / say / hangup ----------------------------------------------------------------------------
def cmd_listen(a) -> int:
    if not 0 < a.timeout_sec <= MAX_WAIT_SEC:  # also rejects nan, which would break the socket timeout
        return _fail(a, f"--timeout-sec must be more than 0 and at most {MAX_WAIT_SEC}", EXIT["usage"])
    if not _alive(a):
        return EXIT["daemon_gone"]
    reply = _ask(a, {"cmd": "listen", "call_id": a.call, "timeout_sec": float(a.timeout_sec)}, timeout=a.timeout_sec + 15)
    if isinstance(reply, int):
        return reply
    if not reply.get("ok"):
        return _refused(a, reply)
    kind = reply.get("kind")
    if kind == "heard":
        _emit(a, reply, f"heard: {reply['text']}")
    elif kind == "incoming":
        _emit(a, reply, f"incoming call {reply['call_id']} on {reply['line']} from {reply['remote']}")
    elif kind == "ended":
        _emit(a, {"ok": True, "kind": "ended", **reply["result"]}, format_result(reply["result"]))
    elif kind == "timeout":
        _emit(a, reply, "timeout")
        return EXIT["listen_timeout"]
    else:  # a daemon speaking another protocol version
        return _fail(a, "unexpected reply from the daemon", EXIT["daemon_gone"])
    return 0


def cmd_say(a) -> int:
    text = " ".join(a.text).strip()
    if not text or len(text) > MAX_SAY_CHARS:
        return _fail(a, f"say needs 1 to {MAX_SAY_CHARS} characters of text", EXIT["usage"])
    if not _alive(a):
        return EXIT["daemon_gone"]
    reply = _ask(a, {"cmd": "say", "call_id": a.call, "text": text})
    if isinstance(reply, int):
        return reply
    if not reply.get("ok"):
        return _refused(a, reply)
    _emit(a, reply, reply["result"])
    return 0


def cmd_hangup(a) -> int:
    if not _alive(a):
        return EXIT["daemon_gone"]
    reply = _ask(a, {"cmd": "hangup", "call_id": a.call, "say": a.say}, timeout=60)
    if isinstance(reply, int):
        return reply
    if not reply.get("ok"):
        return _refused(a, reply)
    result = reply.get("result")
    if result:
        _emit(a, {"ok": True, **result}, format_result(result))
    else:  # a daemon that confirms the hangup without a call record
        _emit(a, reply, "hung up")
    return 0


def register(sub) -> None:
    p = sub.add_parser("call", help="place a call: phone agent mode (--prompt) or relay mode (--relay)")
    p.add_argument("target", help="+E.164 number or sip: URI")
    p.add_argument("--prompt", default=None, help="what this call is for (phone agent mode)")
    p.add_argument("--prompt-file", default=None, help="read the prompt from a file")
    p.add_argument("--relay", action="store_true", help="you hold the conversation with listen/say; returns once answered")
    p.add_argument("--line", default=None)
    p.add_argument("--timeout-sec", type=float, default=30, help="how long to let it ring")
    p.add_argument("--json", action="store_true")
    p.add_argument("--yes", action="store_true", help="call even if the target diverts back to this line")
    p.add_argument("--persona", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_call)

    ls = sub.add_parser("listen", help="wait for the other person to finish speaking, an incoming call, or the end")
    ls.add_argument("--call", default=None)
    ls.add_argument("--timeout-sec", type=float, default=60)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_listen)

    sy = sub.add_parser("say", help="speak on the current relay call")
    sy.add_argument("text", nargs="+")
    sy.add_argument("--call", default=None)
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=cmd_say)

    hu = sub.add_parser("hangup", help="end the current call, optionally saying something first")
    hu.add_argument("--call", default=None)
    hu.add_argument("--say", default=None)
    hu.add_argument("--json", action="store_true")
    hu.set_defaults(func=cmd_hangup)
