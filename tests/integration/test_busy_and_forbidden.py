import asyncio
import subprocess
from pathlib import Path

import pytest

from ai_phone.config import Config, SipConfig
from ai_phone.telephony import events as ev
from ai_phone.telephony.endpoint import Telephony
from ai_phone.telephony.rings import FrameRing

pytestmark = pytest.mark.integration


def _peer(port: int, target="sip:ai@127.0.0.1:5060", dur=8):
    """A pjsua caller that stays up until we send it 'q' (pjsua quits on stdin EOF)."""
    return subprocess.Popen(["pjsua", "--local-port", str(port), "--no-tcp", "--null-audio", "--duration", str(dur),
                             "--log-level", "2", "--app-log-level", "2", target],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _quit(p):
    try:
        p.stdin.write(b"q\n")
        p.stdin.flush()
        p.wait(timeout=10)
    except Exception:
        p.terminate()
        p.wait(timeout=5)


async def _next(events, kind, timeout=10.0):
    while True:
        e = await asyncio.wait_for(events.get(), timeout)
        if isinstance(e, kind):
            return e


def test_second_call_gets_486_and_public_source_gets_403():
    cfg = Config(sip=SipConfig(user="ai", accept_invites_from="lan"))
    loop = asyncio.new_event_loop()
    events: asyncio.Queue = asyncio.Queue()
    rx, tx = FrameRing(None, 100), FrameRing(None, 100)
    tel = Telephony(cfg, loop, events, rx, tx, echo_mode=True)
    tel.start()
    peers = []
    try:
        p1 = _peer(5062)
        peers.append(p1)
        first = loop.run_until_complete(_next(events, ev.IncomingCall))
        assert first.source_ip == "127.0.0.1"
        p2 = _peer(5064)
        peers.append(p2)
        rej = loop.run_until_complete(_next(events, ev.CallRejected))
        assert rej.code == 486, f"second caller should get 486, got {rej.code}"
        _quit(p2)
        _quit(p1)
        loop.run_until_complete(_next(events, ev.CallStateChanged))  # first call ends
        tel.cfg.sip.accept_invites_from = "registered_trunk"  # no trunk configured → every LAN INVITE is disallowed
        p3 = _peer(5066)
        peers.append(p3)
        rej = loop.run_until_complete(_next(events, ev.CallRejected))
        assert rej.code == 403, f"disallowed source should get 403, got {rej.code}"
        _quit(p3)
    finally:
        for p in peers:
            if p.poll() is None:
                _quit(p)
        tel.stop()
        loop.close()
        rx.close()
        rx.unlink()
        tx.close()
        tx.unlink()
