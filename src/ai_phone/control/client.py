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


def request(path: Path, msg: dict, on_event: Callable[[dict], None] | None = None, timeout: float | None = None,
            socket_holder: dict | None = None) -> dict:
    """Send one request; stream events to on_event; return the terminal message (`ended`, or a reply carrying `ok`).
    Anything less than a terminal message (EOF, reset, a closed socket) is DaemonGone. `socket_holder`, if given,
    receives the live socket under "socket" so a signal handler can shut it down."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    if socket_holder is not None:
        socket_holder["socket"] = s
    try:
        try:
            s.connect(str(path))
        except OSError as e:
            raise DaemonGone(f"daemon not running ({path}: {e.strerror or e})") from None
        try:
            s.sendall(json.dumps(msg).encode() + b"\n")
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    m = json.loads(line.decode())
                    if "event" in m and on_event is not None:
                        on_event(m)
                    if m.get("event") == "ended" or "ok" in m:
                        return m
        except OSError as e:
            raise DaemonGone(f"connection to the daemon failed: {e.strerror or e}") from None
        raise DaemonGone("the daemon closed the connection before the call ended")
    finally:
        if socket_holder is not None:
            socket_holder.pop("socket", None)
        s.close()
