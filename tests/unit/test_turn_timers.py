import asyncio
import time

import pytest

from ai_phone.config import ConversationConfig
from ai_phone.pipeline.turn import TurnController
from ai_phone.pipeline.vad import Endpointer, EnergyVad
from ai_phone.services.client import WorkerClient
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME, tag_frame, untag_frame
from ai_phone.telephony.rings import FrameRing
from tests.unit.test_turn_controller import StubPort


async def _tc(loop, cfg, rx_last):
    rx, tx = FrameRing(None, 100), FrameRing(None, 400)
    stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
    stt = WorkerClient("stt", "ai_phone.services.backends.fake:FakeSTT", loop, stt_in, None, {})
    llm = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {"system_prompt": "s"})
    tts = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, tts_out, {})
    await asyncio.gather(stt.start(), llm.start(), tts.start())
    tc = TurnController(cfg, StubPort(), stt, llm, tts, rx, tx, stt_in, tts_out, Endpointer(EnergyVad()), [], "s", lambda t: None,
                        prompt_frames=[b"\x05\x00" * 320] * 5, warning_frames=[b"\x06\x00" * 320] * 5, rtp_last_rx_ns=rx_last)
    return tc, (rx, tx, stt_in, tts_out), (stt, llm, tts)


async def _cleanup(stop, runner, rings, clients):
    stop.set()
    await runner
    await asyncio.gather(*(c.stop() for c in clients))
    for r in rings:
        r.close()
        r.unlink()


@pytest.mark.asyncio
async def test_silence_prompt_then_hangup():
    loop = asyncio.get_running_loop()
    cfg = ConversationConfig(silence_prompt_sec=1, silence_hangup_sec=1)
    tc, rings, clients = await _tc(loop, cfg, lambda: time.monotonic_ns())
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(1.3)
        assert rings[1].available() == 5, "prompt frames not queued after silence_prompt_sec"
        while rings[1].pop():
            pass
        await asyncio.wait_for(tc.hangup_requested.wait(), 2.0)
        assert tc.state == "ENDING" and tc.end_reason == "silence"
    finally:
        await _cleanup(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_max_duration_warns_then_hangs_up():
    loop = asyncio.get_running_loop()
    cfg = ConversationConfig(max_call_sec=2, silence_prompt_sec=100, silence_hangup_sec=100)
    tc, rings, clients = await _tc(loop, cfg, lambda: time.monotonic_ns())
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(1.7)  # warning at max - 0.5 s = 1.5 s
        assert rings[1].available() == 5
        await asyncio.wait_for(tc.hangup_requested.wait(), 1.0)
        assert tc.end_reason == "max_duration"
    finally:
        await _cleanup(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_rtp_timeout_hangs_up():
    loop = asyncio.get_running_loop()
    cfg = ConversationConfig(rtp_timeout_sec=1, silence_prompt_sec=100, silence_hangup_sec=100)
    frozen = time.monotonic_ns()
    tc, rings, clients = await _tc(loop, cfg, lambda: frozen)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.wait_for(tc.hangup_requested.wait(), 2.0)
        assert tc.end_reason == "rtp_timeout"
    finally:
        await _cleanup(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_stalled_brain_gets_the_apology_not_silence():
    """A paged-out or hung model must not leave the caller in silence: reply_timeout_sec ends the wait like a brain error."""
    from tests.unit.test_turn_controller import speech_frames
    from ai_phone.telephony.rings import SILENCE
    loop = asyncio.get_running_loop()
    cfg = ConversationConfig(reply_timeout_sec=1, silence_prompt_sec=100, silence_hangup_sec=100)
    rx, tx = FrameRing(None, 100), FrameRing(None, 400)
    stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
    stt = WorkerClient("stt", "ai_phone.services.backends.fake:FakeSTT", loop, stt_in, None, {})
    llm = WorkerClient("llm", "ai_phone.services.backends.fake:StallLLM", loop, None, None, {"system_prompt": "s"},
                       factory_args={"stall_s": "4"})
    tts = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, tts_out, {})
    await asyncio.gather(stt.start(), llm.start(), tts.start())
    turns = []
    apology = [b"\x07\x00" * 320] * 5
    tc = TurnController(cfg, StubPort(), stt, llm, tts, rx, tx, stt_in, tts_out, Endpointer(EnergyVad()), [], "s", turns.append,
                        apology_frames=apology, goodbye_frames=[b"\x08\x00" * 320] * 5, rtp_last_rx_ns=lambda: time.monotonic_ns())
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        for f in speech_frames(0.6):
            rx.push(f)
            await asyncio.sleep(0.02)
        for _ in range(25):
            rx.push(SILENCE)
            await asyncio.sleep(0.02)
        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end and not turns:
            await asyncio.sleep(0.05)
        assert turns and turns[0].brain_error == "timeout", "reply_timeout_sec did not fire"
        assert tx.available() >= len(apology), "the apology was not queued"
        assert tc.state == "LISTENING"
    finally:
        await _cleanup(stop, runner, (rx, tx, stt_in, tts_out), (stt, llm, tts))
