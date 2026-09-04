from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Awaitable, Callable

from ai_phone.control.protocol import COMMANDS

Handler = Callable[[dict, Callable[[dict], Awaitable[None]]], Awaitable[None]]


class ControlServer:
    def __init__(self, path: Path, handler: Handler):
        self.path, self.handler = Path(path), handler
        self._server: asyncio.AbstractServer | None = None
        self._call_active = False

    async def start(self) -> None:
        if len(str(self.path).encode()) > 100:  # AF_UNIX paths are capped at 104 bytes on macOS
            raise RuntimeError(f"control socket path is too long ({len(str(self.path))} chars): {self.path}; set AI_PHONE_HOME to a shorter path")
        if self.path.exists():
            s = socket.socket(socket.AF_UNIX)
            try:
                s.connect(str(self.path))
                s.close()
                raise RuntimeError(f"another daemon is listening on {self.path}")
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink()  # stale socket from a crashed daemon
            finally:
                s.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o077)  # the socket is 0600 from the instant it exists: no connect-before-chmod window
        try:
            self._server = await asyncio.start_unix_server(self._client, path=str(self.path))
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        gone = {"client": False}

        async def send(msg: dict) -> None:
            """Deliver to the client if it is still there. A client that went away (Ctrl-C, crash) is remembered
            and every later send is a no-op, so the handler — and the call it drives — carries on regardless."""
            if gone["client"] or writer.is_closing():
                gone["client"] = True
                return
            try:
                writer.write(json.dumps(msg).encode() + b"\n")
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                gone["client"] = True

        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            msg = json.loads(line.decode() or "{}")
            if not isinstance(msg, dict):
                await send({"ok": False, "error": "expected a JSON object"})
                return
            if msg.get("cmd") not in COMMANDS:
                await send({"ok": False, "error": f"unknown command {msg.get('cmd')!r}"})
                return
            if msg["cmd"] == "call":
                if self._call_active:
                    await send({"ok": False, "error": "busy"})
                    return
                to, timeout = msg.get("to"), msg.get("timeout_sec", 30)
                if not isinstance(to, str) or not to.strip() or isinstance(timeout, bool) \
                        or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
                    await send({"ok": False, "error": "call needs a non-empty 'to' and a 'timeout_sec' between 0 and 3600"})
                    return
                if any(msg.get(k) is not None and not isinstance(msg.get(k), str) for k in ("line", "persona")):
                    await send({"ok": False, "error": "'line' and 'persona' must be strings"})
                    return  # the daemon looks both up in a dict; an unhashable value would escape as an unhandled task exception
                self._call_active = True
                try:
                    await self.handler(msg, send)
                finally:
                    self._call_active = False
            else:
                await self.handler(msg, send)
        except (asyncio.TimeoutError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.path.exists():
            self.path.unlink()
