import asyncio
import time

import numpy as np
import pytest

from ai_phone.config import ConversationConfig
from ai_phone.pipeline.turn import TurnController
from ai_phone.pipeline.vad import Endpointer, EnergyVad
from ai_phone.services.client import WorkerClient
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME, tag_frame, untag_frame
from ai_phone.telephony.rings import FRAME_BYTES, SILENCE, FrameRing


class StubPort:
    def __init__(self):
        self.first_nonsilent_ns = None
        self.armed = 0

    def arm_t5(self):
        self.armed += 1
        self.first_nonsilent_ns = None


def speech_frames(seconds, amp=0.5):
    n = int(seconds * 16000)
    t = np.arange(n) / 16000.0
    pcm = ((amp * np.sin(2 * np.pi * 300 * t)) * 32767).astype("<i2").tobytes()
    return [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]


async def _drain_tx(tx, port, seconds):
    """Play the role of PJSIP: pop tx every 20 ms, stamping t5 on the first non-silent frame."""
    end = time.monotonic() + seconds
    n = 0
    while time.monotonic() < end:
        f = tx.pop()
        if f is not None:
            n += 1
            if port.first_nonsilent_ns is None and f != SILENCE and port.armed:
                port.first_nonsilent_ns = time.monotonic_ns()
        await asyncio.sleep(0.02)
    return n


async def make_stack(loop, cfg=None, greeting=None):
    rx, tx = FrameRing(None, 100), FrameRing(None, 400)
    stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
    stt = WorkerClient("stt", "ai_phone.services.backends.fake:FakeSTT", loop, stt_in, None, {})
    llm = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {"system_prompt": "sys"})
    tts = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, tts_out, {})
    await asyncio.gather(stt.start(), llm.start(), tts.start())
    port, turns = StubPort(), []
    tc = TurnController(cfg or ConversationConfig(endpoint_silence_ms=300), port, stt, llm, tts, rx, tx, stt_in, tts_out,
                        Endpointer(EnergyVad()), greeting if greeting is not None else [], "sys", turns.append)
    return tc, port, turns, (rx, tx, stt_in, tts_out), (stt, llm, tts)


async def teardown(stop, runner, rings, clients):
    stop.set()
    await runner
    await asyncio.gather(*(c.stop() for c in clients))
    for r in rings:
        r.close()
        r.unlink()


@pytest.mark.asyncio
async def test_full_turn_with_fake_backends():
    loop = asyncio.get_running_loop()
    tc, port, turns, rings, clients = await make_stack(loop, greeting=[b"\x10\x00" * 320] * 10)
    rx, tx = rings[0], rings[1]
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        n = await _drain_tx(tx, port, 0.5)  # greeting plays first
        assert n >= 10 and tc.state == "LISTENING"
        for f in speech_frames(0.6):  # caller speaks 0.6 s then is silent
            rx.push(f)
            await asyncio.sleep(0.02)
        for _ in range(25):
            rx.push(SILENCE)
            await asyncio.sleep(0.02)
        n = await _drain_tx(tx, port, 2.0)
        assert n > 0 and turns, "no turn completed"
        t = turns[0]
        assert t.stt_text.startswith("fake utterance")
        assert t.reply_text == "Hello, this is a fake reply."
        assert t.t0 and t.t1 and t.t2 and t.t3 and t.t4 and t.t5 and t.t0 < t.t1 < t.t2 <= t.t3 < t.t4 < t.t5
        assert tc.history[-2]["role"] == "user" and tc.history[-1]["role"] == "assistant"
        assert tc.state == "LISTENING"
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_bargein_cancels_and_flushes():
    loop = asyncio.get_running_loop()
    tc, port, turns, rings, clients = await make_stack(loop)
    rx, tx = rings[0], rings[1]
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        for f in speech_frames(0.6):
            rx.push(f)
            await asyncio.sleep(0.02)
        for _ in range(20):
            rx.push(SILENCE)
            await asyncio.sleep(0.02)
        for _ in range(100):  # wait until the agent is speaking, without draining tx, so frames pile up
            if tc.state == "SPEAKING":
                break
            await asyncio.sleep(0.02)
        assert tc.state == "SPEAKING" and tx.available() > 0
        for f in speech_frames(0.4, amp=0.8):  # caller talks over it, loudly, for 0.4 s
            rx.push(f)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        assert turns and turns[0].bargein and turns[0].bargein_stop_ns is not None
        assert tx.available() == 0, "tx not flushed"
        assert tc.history[-1]["content"].endswith("[interrupted]")
        assert tc.state in ("LISTENING", "THINKING")
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_speech_during_greeting_stops_it_and_is_heard():
    loop = asyncio.get_running_loop()
    tc, port, turns, rings, clients = await make_stack(loop, greeting=[b"\x10\x00" * 320] * 150)  # 3 s greeting
    rx, tx = rings[0], rings[1]
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        assert tc.state == "GREETING" and tx.available() > 100
        for f in speech_frames(0.6):  # caller talks over the greeting
            rx.push(f)
            await asyncio.sleep(0.02)
        assert tx.available() < 100, "greeting was not cut"
        for _ in range(25):
            rx.push(SILENCE)
            await asyncio.sleep(0.02)
        await _drain_tx(tx, port, 2.0)
        assert turns and turns[0].stt_text.startswith("fake utterance")
        assert turns[0].t0_end and turns[0].t0_end > turns[0].t0
    finally:
        await teardown(stop, runner, rings, clients)
