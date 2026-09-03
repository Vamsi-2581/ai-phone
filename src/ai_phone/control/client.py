from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Callable


class DaemonGone(RuntimeError):
    pass


def probe(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def request(path: Path, msg: dict, on_event: Callable[[dict], None] | None = None, timeout: float | None = None) -> dict:
    """Send one request; stream events to on_event; return the final message (`ended` or a reply)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(path))
        except OSError as e:
            raise DaemonGone(f"daemon not running ({path}: {e.strerror or e})") from None
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        last: dict | None = None
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                m = json.loads(line.decode())
                last = m
                if "event" in m and on_event is not None:
                    on_event(m)
                if m.get("event") == "ended" or "ok" in m:
                    return m
        if last is None:
            raise DaemonGone("daemon went away before replying")
        return last
    finally:
        s.close()
