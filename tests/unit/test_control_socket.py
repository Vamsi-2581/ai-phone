import asyncio
from pathlib import Path

import pytest

from ai_phone.control.client import DaemonGone, probe, request
from ai_phone.control.server import ControlServer


@pytest.fixture
def sock_dir():
    """AF_UNIX paths are limited to ~100 bytes on macOS; pytest's tmp_path is longer than that."""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="aip-", dir="/private/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def _handler(msg, send):
    if msg["cmd"] == "status":
        await send({"ok": True, "state": "fine"})
    elif msg["cmd"] == "call":
        await send({"event": "dialling"})
        await asyncio.sleep(0.2)
        await send({"event": "answered"})
        await send({"event": "ended", "reason": "answered", "exit_code": 0})
    elif msg["cmd"] == "hangup":
        await send({"ok": True})


@pytest.mark.asyncio
async def test_status_and_streamed_call(sock_dir: Path):
    sock = sock_dir / "control.sock"
    srv = ControlServer(sock, _handler)
    await srv.start()
    try:
        loop = asyncio.get_running_loop()
        assert probe(sock)
        assert (await loop.run_in_executor(None, request, sock, {"cmd": "status"}))["state"] == "fine"
        events = []
        last = await loop.run_in_executor(None, lambda: request(sock, {"cmd": "call", "to": "sip:x"}, events.append))
        assert [e["event"] for e in events] == ["dialling", "answered", "ended"] and last["exit_code"] == 0
    finally:
        await srv.stop()
    assert not sock.exists() and not probe(sock)


@pytest.mark.asyncio
async def test_second_call_is_refused_but_hangup_is_not(sock_dir: Path):
    sock = sock_dir / "control.sock"
    srv = ControlServer(sock, _handler)
    await srv.start()
    try:
        loop = asyncio.get_running_loop()
        first = loop.run_in_executor(None, lambda: request(sock, {"cmd": "call", "to": "sip:x"}, lambda e: None))
        await asyncio.sleep(0.05)
        second = await loop.run_in_executor(None, request, sock, {"cmd": "call", "to": "sip:y"})
        assert second == {"ok": False, "error": "busy"}
        assert (await loop.run_in_executor(None, request, sock, {"cmd": "hangup"}))["ok"] is True
        assert (await first)["exit_code"] == 0
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_client_disconnect_does_not_kill_the_handler(sock_dir: Path):
    sock = sock_dir / "control.sock"
    finished = asyncio.Event()

    async def handler(msg, send):  # no try/except: the server itself must shield the call from a vanished client
        await send({"event": "dialling"})
        await asyncio.sleep(0.3)
        await send({"event": "answered"})
        await send({"event": "ended", "reason": "answered", "exit_code": 0})
        finished.set()

    srv = ControlServer(sock, handler)
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))  # non-blocking: the server shares this loop
        writer.write(b'{"cmd":"call","to":"sip:x"}\n')
        await writer.drain()
        assert b"dialling" in await reader.readline()
        writer.close()                  # Ctrl-C without hangup: the call goes on
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        await srv.stop()


def test_daemon_gone(sock_dir: Path):
    with pytest.raises(DaemonGone):
        request(sock_dir / "missing.sock", {"cmd": "status"})


@pytest.mark.asyncio
async def test_eof_before_a_terminal_message_is_daemon_gone(sock_dir: Path):
    sock = sock_dir / "control.sock"

    async def handler(msg, send):
        await send({"event": "dialling"})   # then the daemon dies: the connection just closes

    srv = ControlServer(sock, handler)
    await srv.start()
    try:
        loop = asyncio.get_running_loop()
        with pytest.raises(DaemonGone):
            await loop.run_in_executor(None, lambda: request(sock, {"cmd": "call", "to": "sip:x"}, lambda e: None))
    finally:
        await srv.stop()
