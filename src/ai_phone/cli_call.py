"""`ai-phone call <+E.164 | sip:…>`: ask the daemon to dial, stream progress, exit with the outcome."""
from __future__ import annotations

import signal

from ai_phone import paths
from ai_phone.control.client import DaemonGone, probe, request
from ai_phone.control.protocol import EXIT


def cmd_call(a) -> int:
    to = a.target
    if paths.config_path().exists() and not a.yes:
        from ai_phone.config import load_config
        cfg = load_config(paths.config_path())
        line = cfg.lines.get(a.line) if a.line else next(iter(cfg.lines.values()), None)
        if line is not None and to in line.diverted_from:
            print(f"{to} diverts to this line ({line.number}); calling it would ring the agent itself. Pass --yes to do it anyway.")
            return EXIT["rejected"]
    if not probe(paths.socket_path()):
        print("ai-phone is not running (start it with `ai-phone run`).")
        return EXIT["daemon_gone"]

    def on_event(e: dict) -> None:
        kind = e.get("event")
        if kind == "turn":
            print(f"turn {e.get('turn')}: first audible {e.get('first_audible_ms')} ms", flush=True)
        elif kind == "ended":
            print(f"ended: {e.get('reason')}", flush=True)
        else:
            print(f"{kind} {e.get('uri', '')}".strip(), flush=True)

    def on_sigint(*_):
        try:
            request(paths.socket_path(), {"cmd": "hangup"}, timeout=2)
        except DaemonGone:
            pass

    signal.signal(signal.SIGINT, on_sigint)
    try:
        last = request(paths.socket_path(), {"cmd": "call", "to": to, "line": a.line, "persona": a.persona, "timeout_sec": a.timeout_sec}, on_event)
    except DaemonGone as e:
        print(f"daemon went away: {e}")
        return EXIT["daemon_gone"]
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
