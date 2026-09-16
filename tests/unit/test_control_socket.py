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
async def test_busy_is_the_daemons_answer_and_hangup_runs_alongside_a_call(sock_dir: Path):
    """The server no longer tracks calls itself: the daemon (here a stub) refuses a second one."""
    sock = sock_dir / "control.sock"
    active = {"call": False}

    async def handler(msg, send):
        if msg["cmd"] == "call":
            if active["call"]:
                await send({"ok": False, "error": "busy", "code": 5})
                return
            active["call"] = True
            try:
                await _handler(msg, send)
            finally:
                active["call"] = False
        else:
            await _handler(msg, send)

    srv = ControlServer(sock, handler)
    await srv.start()
    try:
        loop = asyncio.get_running_loop()
        first = loop.run_in_executor(None, lambda: request(sock, {"cmd": "call", "to": "sip:x"}, lambda e: None))
        await asyncio.sleep(0.05)
        second = await loop.run_in_executor(None, request, sock, {"cmd": "call", "to": "sip:y"})
        assert second == {"ok": False, "error": "busy", "code": 5}
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


async def _raw(sock: Path, payload: bytes) -> dict:
    import json
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(payload)
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 5)
    writer.close()
    return json.loads(line)


@pytest.mark.asyncio
async def test_non_object_and_malformed_call_payloads_get_an_error_reply(sock_dir: Path):
    sock = sock_dir / "control.sock"
    srv = ControlServer(sock, _handler)
    await srv.start()
    try:
        assert (await _raw(sock, b"1\n"))["error"] == "expected a JSON object"
        assert (await _raw(sock, b'"x"\n'))["ok"] is False
        assert "to" in (await _raw(sock, b'{"cmd": "call"}\n'))["error"]
        assert "timeout_sec" in (await _raw(sock, b'{"cmd": "call", "to": "+15550000000", "timeout_sec": "soon"}\n'))["error"]
        assert (await _raw(sock, b'{"cmd": "call", "to": "", "timeout_sec": 5}\n'))["ok"] is False
        # 'line' and 'persona' reach a dict lookup in the daemon: an unhashable value must not escape as a task exception
        assert "line" in (await _raw(sock, b'{"cmd": "call", "to": "+15550000000", "line": ["a"]}\n'))["error"]
        assert "persona" in (await _raw(sock, b'{"cmd": "call", "to": "+15550000000", "persona": "x"}\n'))["error"]
        assert "relay" in (await _raw(sock, b'{"cmd": "call", "to": "+15550000000", "relay": true, "prompt": "x"}\n'))["error"]
        assert "prompt" in (await _raw(sock, b'{"cmd": "call", "to": "+15550000000", "prompt": 5}\n'))["error"]
        assert "text" in (await _raw(sock, b'{"cmd": "say"}\n'))["error"]
        assert "call_id" in (await _raw(sock, b'{"cmd": "say", "text": "hi", "call_id": "XYZ"}\n'))["error"]
        assert "timeout_sec" in (await _raw(sock, b'{"cmd": "listen", "timeout_sec": 0}\n'))["error"]
        assert (await _raw(sock, b'{"cmd": "hangup", "say": ""}\n'))["code"] == 2
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_a_listen_whose_client_leaves_is_cancelled(sock_dir: Path):
    cancelled = asyncio.Event()

    async def handler(msg, send):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    sock = sock_dir / "control.sock"
    srv = ControlServer(sock, handler)
    await srv.start()
    try:
        _, w = await asyncio.open_unix_connection(str(sock))
        w.write(b'{"cmd": "listen", "timeout_sec": 30}\n')
        await w.drain()
        await asyncio.sleep(0.1)
        w.close()
        await asyncio.wait_for(cancelled.wait(), 2)
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_stop_ends_waiting_handlers_promptly(sock_dir: Path):
    async def handler(msg, send):
        await asyncio.sleep(30)

    sock = sock_dir / "control.sock"
    srv = ControlServer(sock, handler)
    await srv.start()
    w = None
    try:
        _, w = await asyncio.open_unix_connection(str(sock))
        w.write(b'{"cmd": "listen", "timeout_sec": 30}\n')
        await w.drain()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(srv.stop(), 2)
    finally:
        if w is not None:
            w.close()
        await srv.stop()  # a no-op once stopped; frees the socket if the assertion above failed


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd, exc", [("call", ValueError), ("say", TimeoutError),
                                      ("listen", ValueError), ("listen", TimeoutError)])
async def test_a_handler_error_closes_the_connection_and_reaches_the_loop(sock_dir: Path, cmd, exc):
    """A handler bug is never swallowed by the server: asyncio reports it as an unhandled client_connected_cb error
    (the daemon's log shows it) and the client sees the connection close. `listen` covers the wrapper's re-raise."""
    import json

    async def handler(msg, send):
        if exc is TimeoutError:
            await asyncio.wait_for(asyncio.sleep(5), 0.05)  # a timeout the handler forgot to catch
        raise exc("handler bug")

    payload = {"call": {"cmd": "call", "to": "sip:x"}, "say": {"cmd": "say", "text": "hi"},
               "listen": {"cmd": "listen", "timeout_sec": 5}}[cmd]
    loop = asyncio.get_running_loop()
    reported: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    sock = sock_dir / "control.sock"
    srv = ControlServer(sock, handler)
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.readline(), 2) == b""   # closed, no reply
        writer.close()
        for _ in range(100):
            if reported:
                break
            await asyncio.sleep(0.02)
        assert [type(c.get("exception")) for c in reported] == [exc]
        assert "client_connected_cb" in reported[0]["message"]
    finally:
        loop.set_exception_handler(previous)
        await srv.stop()
