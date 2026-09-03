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
        self._server = await asyncio.start_unix_server(self._client, path=str(self.path))
        os.chmod(self.path, 0o600)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def send(msg: dict) -> None:
            try:
                writer.write(json.dumps(msg).encode() + b"\n")
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                raise ConnectionError(str(e)) from None

        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            msg = json.loads(line.decode() or "{}")
            if msg.get("cmd") not in COMMANDS:
                await send({"ok": False, "error": f"unknown command {msg.get('cmd')!r}"})
                return
            if msg["cmd"] == "call":
                if self._call_active:
                    await send({"ok": False, "error": "busy"})
                    return
                self._call_active = True
                try:
                    await self.handler(msg, send)
                finally:
                    self._call_active = False
            else:
                await self.handler(msg, send)
        except (asyncio.TimeoutError, ValueError, ConnectionError):
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
