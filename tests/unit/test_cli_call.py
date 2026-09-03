import asyncio
import threading
from pathlib import Path

import pytest

from ai_phone.cli import main
from ai_phone.control.server import ControlServer


@pytest.fixture
def sock_dir():
    """AF_UNIX paths are limited to ~100 bytes on macOS; pytest's tmp_path is longer than that."""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="aip-", dir="/private/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _serve(sock: Path, handler):
    loop = asyncio.new_event_loop()
    srv = ControlServer(sock, handler)
    ready = threading.Event()

    async def run():
        await srv.start()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.02)
        await srv.stop()

    stop = threading.Event()
    t = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
    t.start()
    ready.wait(2)
    return stop, t


@pytest.mark.parametrize("outcome, code", [("answered", 0), ("busy", 5), ("no_answer", 6), ("rejected", 7), ("redirected", 8)])
def test_call_exit_codes(sock_dir: Path, monkeypatch, capsys, outcome, code):
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))

    async def handler(msg, send):
        assert msg["cmd"] == "call" and msg["to"] == "+447700900123" and msg["line"] == "main"
        await send({"event": "dialling", "uri": "sip:+447700900123@sip.telnyx.com"})
        if outcome == "answered":
            await send({"event": "answered"})
            await send({"event": "turn", "turn": 0, "first_audible_ms": 812.0})
        await send({"event": "ended", "reason": outcome, "exit_code": code})

    stop, t = _serve(sock_dir / "control.sock", handler)
    try:
        assert main(["call", "+447700900123", "--line", "main", "--yes"]) == code
        out = capsys.readouterr().out
        assert "dialling" in out and outcome in out
    finally:
        stop.set(); t.join(2)


def test_call_without_daemon(sock_dir: Path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))
    assert main(["call", "+447700900123"]) == 9
    assert "not running" in capsys.readouterr().out


def test_call_to_diverted_number_needs_yes(sock_dir: Path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))
    (sock_dir / "config.yaml").write_text(
        'lines:\n  main:\n    kind: trunk\n    number: "+442079460000"\n    registrar: "sip:sip.telnyx.com"\n    dial_domain: "sip.telnyx.com"\n'
        '    user: aimain1234\n    trusted_cidrs: ["192.0.2.0/24"]\n    diverted_from: ["+447700900123"]\n')
    assert main(["call", "+447700900123", "--line", "main"]) == 7
    assert "diverts" in capsys.readouterr().out


def test_daemon_vanishing_mid_call_exits_9(sock_dir: Path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))

    async def handler(msg, send):
        await send({"event": "dialling", "uri": "sip:x"})   # and then nothing: the connection closes

    stop, t = _serve(sock_dir / "control.sock", handler)
    try:
        assert main(["call", "sip:x@127.0.0.1", "--yes"]) == 9
    finally:
        stop.set(); t.join(2)


def test_sigint_handler_is_restored(sock_dir: Path, monkeypatch):
    import signal
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))
    before = signal.getsignal(signal.SIGINT)

    async def handler(msg, send):
        await send({"event": "ended", "reason": "busy", "exit_code": 5})

    stop, t = _serve(sock_dir / "control.sock", handler)
    try:
        assert main(["call", "sip:x@127.0.0.1", "--yes"]) == 5
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        stop.set(); t.join(2)
