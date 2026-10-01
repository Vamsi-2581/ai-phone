"""`ai-phone status [--last-call]`: registration per line from state.json; the daemon is only probed for liveness."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from ai_phone import paths
from ai_phone.control.client import probe
from ai_phone.telephony.state import StateError, StateFile


def render_status(state: dict, alive: bool) -> str:
    rows = [f"daemon {'running' if alive else 'not running'} (state written {time.strftime('%H:%M:%S', time.localtime(state.get('written_at', 0)))})"]
    b = state.get("brain") or {}
    rows.append(f"brain: {b.get('kind', 'local')}" + (f" ({b['model']})" if b.get("model") else "")
                + (f" at {b['base_url']}" if b.get("base_url") else ""))
    rows.append(f"{'line':12s} {'kind':5s} {'state':12s} {'address':28s} registrar / error")
    for lid, l in (state.get("lines") or {}).items():
        extra = l.get("last_error") or (l.get("registrar") or "")
        rows.append(f"{lid:12s} {l.get('kind', '?'):5s} {l.get('state', '?'):12s} {l.get('address', ''):28s} {extra}")
    ac = state.get("active_call")
    if not ac:
        rows.append("active call: none")
    elif ac.get("call") and ac.get("mode"):
        rows.append(f"active call: {ac.get('direction', '?')} {ac['mode']} call {ac['call']} on {ac.get('line_id', '?')} "
                    f"with {ac.get('remote', '?')}")
    else:
        rows.append(f"active call: {ac.get('direction', '?')} on {ac.get('line_id', '?')} with {ac.get('remote', '?')}")
    return "\n".join(rows)


def render_last_call(turns_path: Path) -> str:
    if not turns_path.exists():
        return "no turns recorded yet"
    rows = []
    for l in turns_path.read_text().splitlines():
        if not l.strip():
            continue
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue  # a record cut short by an interrupted write must not hide the rest
    if not rows:
        return "no turns recorded yet"
    # PJSIP reuses its call IDs, so records that carry the call record's own ID are grouped by that
    key = "call" if rows[-1].get("call") else "call_id"
    last_id = rows[-1][key]
    call = [r for r in rows if r.get(key) == last_id]
    lat = [r["first_audible_ms"] for r in call if r.get("first_audible_ms") is not None]
    p50 = f"{statistics.median(lat):.0f} ms" if lat else "n/a"
    return (f"call {last_id}: {len(call)} turns, first audible p50 {p50}, "
            f"rx overruns: {sum(r.get('rx_overruns', 0) for r in call)}, tx underruns: {sum(r.get('tx_underruns', 0) for r in call)}, "
            f"brain errors: {sum(1 for r in call if r.get('brain_error'))}, direction: {call[0].get('direction', 'in')}")


def cmd_status(a) -> int:
    try:
        state = StateFile.read(paths.state_path())
    except StateError:
        print("ai-phone has never started on this machine (no state file). Run `ai-phone run`.")
        return 1
    print(render_status(state, probe(paths.socket_path())))
    if a.last_call:
        from ai_phone.config import load_config
        cfg = load_config(paths.config_path()) if paths.config_path().exists() else None
        log_dir = Path(cfg.log.dir).expanduser() if cfg else Path("~/Library/Logs/ai-phone").expanduser()
        print(render_last_call(log_dir / "turns.jsonl"))
    return 0


def register(sub) -> None:
    p = sub.add_parser("status", help="registration state per line, the active call, the brain in use")
    p.add_argument("--last-call", action="store_true", help="summarise the last call from the turn log")
    p.set_defaults(func=cmd_status)
