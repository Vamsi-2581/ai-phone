import asyncio
import subprocess
from pathlib import Path

import pytest

from ai_phone.config import Config, LineConfig
from ai_phone.telephony import events as ev
from ai_phone.telephony.endpoint import LineSpec, Telephony
from ai_phone.telephony.rings import FrameRing

pytestmark = pytest.mark.integration


def _tone(path: Path):
    import math, struct, wave
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(8000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(frames)


def _spec(line_id, user, mode="lan"):
    return LineSpec.from_config(line_id, LineConfig(kind="sip", user=user, aor=f"sip:{user}@127.0.0.1", display_name=user,
                                                    accept_invites_from=mode, trusted_peers=["203.0.113.9"] if mode == "trusted" else [],
                                                    caller_id=f"sip:{user}@127.0.0.1"), None)


def _drive(lines, target_user, tmp_path):
    play, rec = tmp_path / "t.wav", tmp_path / "r.wav"
    _tone(play)
    loop = asyncio.new_event_loop()
    events: asyncio.Queue = asyncio.Queue()
    rx, tx = FrameRing(None, 100), FrameRing(None, 100)
    tel = Telephony(Config(), lines, loop, events, rx, tx, echo_mode=True)
    seen = []

    async def go():
        tel.start()
        peer = subprocess.Popen(["bash", "scripts/pjsua_peer.sh", str(play), str(rec), "3", f"sip:{target_user}@127.0.0.1:5060"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while True:
                e = await asyncio.wait_for(events.get(), timeout=15)
                seen.append(e)
                if isinstance(e, ev.IncomingCall):
                    tel.answer(e.call_id)
                if isinstance(e, (ev.CallRejected,)) or (isinstance(e, ev.CallStateChanged) and e.state == "ended"):
                    break
            while True:  # a rejection's "ended" arrives before its CallRejected; drain the tail
                try:
                    seen.append(await asyncio.wait_for(events.get(), timeout=1.0))
                except asyncio.TimeoutError:
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
    return seen


def test_invite_is_attributed_to_the_dialled_line(tmp_path: Path):
    seen = _drive([_spec("alpha", "alpha"), _spec("beta", "beta")], "beta", tmp_path)
    inc = [e for e in seen if isinstance(e, ev.IncomingCall)]
    assert inc and inc[0].line_id == "beta"
    assert any(isinstance(e, ev.CallStateChanged) and e.line_id == "beta" and e.state == "active" for e in seen)


def test_policy_is_per_line(tmp_path: Path):
    seen = _drive([_spec("alpha", "alpha"), _spec("beta", "beta", mode="trusted")], "beta", tmp_path)
    rej = [e for e in seen if isinstance(e, ev.CallRejected)]
    assert rej and rej[0].code == 403 and rej[0].line_id == "beta"
