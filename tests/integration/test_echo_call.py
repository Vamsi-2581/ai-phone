import asyncio
import math
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from ai_phone.config import Config, LineConfig
from ai_phone.telephony import events as ev
from ai_phone.telephony.endpoint import LineSpec, Telephony
from ai_phone.telephony.rings import FrameRing
from tests.integration import ports  # noqa: E402

pytestmark = pytest.mark.integration


def _tone_wav(path: Path, seconds=3.0, hz=440, rate=8000):
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(n))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def _rms(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def test_echo_call_round_trips_audio(tmp_path: Path):
    play, rec = tmp_path / "tone.wav", tmp_path / "rec.wav"
    _tone_wav(play)
    spec = LineSpec.from_config("main", LineConfig(kind="sip", user="ai", aor="sip:ai@127.0.0.1", display_name="ai",
                                                   accept_invites_from="lan", caller_id="sip:ai@127.0.0.1"), None)
    loop = asyncio.new_event_loop()
    events: asyncio.Queue = asyncio.Queue()
    rx, tx = FrameRing(None, 100), FrameRing(None, 100)
    tel = Telephony(ports.config(), [spec], loop, events, rx, tx, echo_mode=True)
    seen = []
    stats = {}

    async def drive():
        tel.start()
        # pjsua is interactive: it quits on stdin EOF, so keep stdin open and send "q" when the call ends
        peer = subprocess.Popen(["bash", "scripts/pjsua_peer.sh", str(play), str(rec), "6"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while True:
                e = await asyncio.wait_for(events.get(), timeout=15)
                seen.append(e)
                if isinstance(e, ev.IncomingCall):
                    tel.answer(e.call_id)
                if isinstance(e, ev.CallStateChanged) and e.state == "ended":
                    break
        finally:
            try:
                peer.stdin.write(b"q\n")
                peer.stdin.flush()
                peer.wait(timeout=10)
            except Exception:
                peer.terminate()
                peer.wait(timeout=5)
            stats["last_rx_ns"] = tel.port.last_rx_ns  # read before stop() releases the port
            tel.stop()

    try:
        loop.run_until_complete(drive())
    finally:
        loop.close()
        rx.close()
        rx.unlink()
        tx.close()
        tx.unlink()

    assert any(isinstance(e, ev.IncomingCall) for e in seen)
    assert any(isinstance(e, ev.MediaActive) for e in seen)
    assert rec.exists() and _rms(rec) > 500, "peer recorded silence: echo path is broken"
    assert stats["last_rx_ns"] > 0
