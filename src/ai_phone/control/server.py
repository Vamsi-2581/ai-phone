from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Awaitable, Callable

from ai_phone.control.protocol import CALL_ID_RE, COMMANDS, EXIT, MAX_PROMPT_CHARS, MAX_SAY_CHARS

Handler = Callable[[dict, Callable[[dict], Awaitable[None]]], Awaitable[None]]


class ControlServer:
    def __init__(self, path: Path, handler: Handler):
        self.path, self.handler = Path(path), handler
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()

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
        task = asyncio.current_task()
        self._clients.add(task)
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
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
                msg = json.loads(line.decode() or "{}")
            except (asyncio.TimeoutError, ValueError):
                return  # a client that sent nothing, or not JSON, is dropped
            # From here on an exception is a handler bug: it propagates, the connection is closed below, and the
            # loop's exception handler reports it ("Unhandled exception in client_connected_cb", in the daemon log).
            if not isinstance(msg, dict):
                await send({"ok": False, "error": "expected a JSON object", "code": EXIT["usage"]})
                return
            if msg.get("cmd") not in COMMANDS:
                await send({"ok": False, "error": f"unknown command {msg.get('cmd')!r}", "code": EXIT["usage"]})
                return
            problem = _problem(msg)
            if problem:
                await send({"ok": False, "error": problem, "code": EXIT["usage"]})
                return
            if msg["cmd"] == "listen":
                await _until_done_or_gone(self.handler(msg, send), reader)
            else:
                await self.handler(msg, send)  # a call carries on when its client leaves (send becomes a no-op)
        finally:
            self._clients.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._clients):
                t.cancel()
            await asyncio.gather(*self._clients, return_exceptions=True)
            await self._server.wait_closed()
            self._server = None
        if self.path.exists():
            self.path.unlink()


async def _until_done_or_gone(work, reader: asyncio.StreamReader) -> None:
    """`listen` holds the relay-listener slot: a client that disconnects must give it back at once."""
    job = asyncio.ensure_future(work)
    eof = asyncio.ensure_future(reader.read(1))
    try:
        await asyncio.wait({job, eof}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (job, eof):
            if not t.done():
                t.cancel()
        results = await asyncio.gather(job, eof, return_exceptions=True)
    if isinstance(results[0], Exception):
        raise results[0]  # a handler bug is not hidden by the wrapper


def _number(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float))


def _problem(msg: dict) -> str | None:
    """A request the daemon must never see: wrong types, empty text, over-long text. None when it is fine."""
    cmd = msg["cmd"]
    cid = msg.get("call_id")
    if cid is not None and not (isinstance(cid, str) and CALL_ID_RE.fullmatch(cid)):
        return "call_id must be 6 lowercase hex characters"
    if cmd == "call":
        to, timeout = msg.get("to"), msg.get("timeout_sec", 30)
        if not isinstance(to, str) or not to.strip():
            return "call needs a non-empty 'to'"
        if not _number(timeout) or not 0 < timeout <= 3600:
            return "call needs a 'timeout_sec' between 0 and 3600"
        if msg.get("line") is not None and not isinstance(msg.get("line"), str):
            return "'line' must be a string"
        if msg.get("persona") is not None:
            return "persona is no longer supported; describe the call with prompt"
        prompt, relay = msg.get("prompt"), msg.get("relay", False)
        if not isinstance(relay, bool):
            return "'relay' must be true or false"
        if prompt is not None and (not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS):
            return f"'prompt' must be text of at most {MAX_PROMPT_CHARS} characters"
        if relay and prompt:
            return "a prompt has no effect with relay"
    elif cmd == "listen":
        timeout = msg.get("timeout_sec", 60)
        if not _number(timeout) or not 0 < timeout <= 3600:
            return "listen needs a 'timeout_sec' between 0 and 3600"
    elif cmd == "say":
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SAY_CHARS:
            return f"say needs 'text' of 1 to {MAX_SAY_CHARS} characters"
    elif cmd == "hangup":
        say = msg.get("say")
        if say is not None and (not isinstance(say, str) or not say.strip() or len(say) > MAX_SAY_CHARS):
            return f"'say' must be 1 to {MAX_SAY_CHARS} characters"
    return None
