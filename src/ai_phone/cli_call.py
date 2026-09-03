"""`ai-phone call <+E.164 | sip:…>`: ask the daemon to dial, stream progress, exit with the outcome."""
from __future__ import annotations

import signal
import socket
import threading

from ai_phone import paths
from ai_phone.control.client import DaemonGone, probe, request
from ai_phone.control.protocol import EXIT


def cmd_call(a) -> int:
    to = a.target
    if paths.config_path().exists() and not a.yes and not to.startswith("sip:"):
        from ai_phone.config import load_config
        cfg = load_config(paths.config_path())
        # a diverted number rings the agent whichever line dials it, so check the named line or, without --line, every line
        lines = [cfg.lines[a.line]] if a.line and a.line in cfg.lines else list(cfg.lines.values())
        hit = next((ln for ln in lines if to in ln.diverted_from), None)
        if hit is not None:
            print(f"{to} diverts to this agent ({hit.number}); calling it would ring the agent itself. Pass --yes to do it anyway.")
            return EXIT["rejected"]
    if not probe(paths.socket_path()):
        print("ai-phone is not running (start it with `ai-phone run`).")
        return EXIT["daemon_gone"]

    def on_event(e: dict) -> None:
        kind = e.get("event")
        if kind == "turn":
            print(f"turn {e.get('turn')}: first audible {e.get('first_audible_ms')} ms", flush=True)
        elif kind == "ended":
            detail = e.get("detail")
            print(f"ended: {e.get('reason')}" + (f" ({detail})" if detail else ""), flush=True)
        else:
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
        last = request(paths.socket_path(), {"cmd": "call", "to": to, "line": a.line, "persona": a.persona, "timeout_sec": a.timeout_sec}, on_event,
                       socket_holder=holder)
    except DaemonGone as e:
        if interrupted["flag"]:
            print("hung up; the daemon did not confirm within 5 s")
            return EXIT["daemon_gone"]
        print(f"daemon went away: {e}")
        return EXIT["daemon_gone"]
    finally:
        signal.signal(signal.SIGINT, previous)
        timer = holder.get("timer")
        if timer is not None:
            timer.cancel()  # `ended` arrived (or the request failed): nothing left to cut
    if last.get("ok") is False:
        print(f"refused: {last.get('error')}")
        return EXIT["line_busy"] if last.get("error") == "busy" else EXIT["rejected"]
    return int(last.get("exit_code", EXIT["rejected"]))


def register(sub) -> None:
    p = sub.add_parser("call", help="place an outbound call and run the conversation")
    p.add_argument("target", help="+E.164 number or sip: URI")
    p.add_argument("--line", default=None)
    p.add_argument("--persona", default=None)
    p.add_argument("--timeout-sec", type=float, default=30)
    p.add_argument("--yes", action="store_true", help="call even if the target diverts back to this line")
    p.set_defaults(func=cmd_call)
