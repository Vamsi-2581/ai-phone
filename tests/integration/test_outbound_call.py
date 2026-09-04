import asyncio
import subprocess

import pytest

from ai_phone.config import Config, LineConfig
from ai_phone.telephony import events as ev
from ai_phone.telephony.endpoint import LineSpec, Telephony
from ai_phone.telephony.rings import FrameRing
from tests.integration.ports import PEER_PORT, SIP_PORT  # noqa: E402
from tests.integration import ports  # noqa: E402

pytestmark = pytest.mark.integration


def _callee(auto_answer: str):
    """pjsua as the far end on port 5062: 200 answers, 180 rings forever, 486 is busy."""
    return subprocess.Popen(["pjsua", "--local-port", str(PEER_PORT), "--null-audio", "--auto-answer", auto_answer,
                             "--dis-codec", "speex", "--dis-codec", "ilbc", "--dis-codec", "gsm", "--dis-codec", "g722", "--dis-codec", "opus",
                             "--duration", "4", "--app-log-level", "2", "--log-level", "2"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run(auto_answer: str, timeout_sec: float):
    loop = asyncio.new_event_loop()
    events: asyncio.Queue = asyncio.Queue()
    rx, tx = FrameRing(None, 100), FrameRing(None, 100)
    spec = LineSpec.from_config("agents", LineConfig(kind="sip", user="agents", aor="sip:agents@127.0.0.1", display_name="agents",
                                                     accept_invites_from="lan", caller_id="sip:agents@127.0.0.1"), None)
    tel = Telephony(ports.config(), [spec], loop, events, rx, tx, echo_mode=True)
    seen = []

    async def go():
        tel.start()
        peer = _callee(auto_answer)
        await asyncio.sleep(1.0)
        try:
            tel.make_call("agents", f"sip:peer@127.0.0.1:{PEER_PORT}", timeout_sec=timeout_sec)
            while True:
                e = await asyncio.wait_for(events.get(), timeout=15)
                seen.append(e)
                if isinstance(e, ev.CallStateChanged) and e.state == "ended":
                    break
        finally:
            try:
                peer.stdin.write(b"q\n"); peer.stdin.flush(); peer.wait(timeout=10)
            except Exception:
                peer.terminate(); peer.wait(timeout=5)
            tel.stop()

    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
        for r in (rx, tx):
            r.close(); r.unlink()
    return [e for e in seen if isinstance(e, ev.OutboundProgress)]


def test_answered_call_reports_dialling_then_answered():
    states = [e.state for e in _run("200", 10)]
    assert states[0] == "dialling" and "answered" in states


def test_busy_callee():
    states = [e.state for e in _run("486", 10)]
    assert states[-1] == "busy"


def test_no_answer_times_out_with_cancel():
    states = [e.state for e in _run("180", 2)]
    assert "ringing" in states and states[-1] == "no_answer"
