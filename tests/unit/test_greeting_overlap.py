import asyncio, numpy as np, pytest, time
from ai_phone.config import ConversationConfig
from ai_phone.pipeline.turn import TurnController
from ai_phone.pipeline.vad import Endpointer, EnergyVad
from ai_phone.services.client import WorkerClient
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME
from ai_phone.telephony.rings import FRAME_BYTES, SILENCE, FrameRing
from tests.unit.test_turn_controller import StubPort, speech_frames, _drain_tx
from ai_phone.pipeline.bargein import BargeInDetector

@pytest.mark.asyncio
async def test_speech_starting_during_greeting_reaches_stt_whole():
    loop = asyncio.get_running_loop()
    rx, tx = FrameRing(None, 100), FrameRing(None, 400)
    stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
    stt = WorkerClient("stt", "ai_phone.services.backends.fake:CountingSTT", loop, stt_in, None, {})
    llm = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {"system_prompt": "s"})
    tts = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, tts_out, {})
    await asyncio.gather(stt.start(), llm.start(), tts.start())
    port, turns = StubPort(tx), []
    tc = TurnController(ConversationConfig(), port, stt, llm, tts, rx, tx, stt_in, tts_out, Endpointer(EnergyVad()),
                        [b"\x10\x00" * 320] * 150, "s", turns.append)   # 3 s greeting
    stop = asyncio.Event(); runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        frames = speech_frames(1.0)            # caller talks for 1.0 s, starting 0.1 s into the greeting
        for f in frames:
            rx.push(f); await asyncio.sleep(0.02)
        for _ in range(25):
            rx.push(SILENCE); await asyncio.sleep(0.02)
        await _drain_tx(tx, port, 2.0)
        assert turns, "no turn"
        n = int(turns[0].stt_text.split()[0])
        print(f"samples that reached STT: {n} of {len(frames) * 320}")
        assert n >= 0.9 * len(frames) * 320, f"STT received {n} samples of {len(frames) * 320}"
    finally:
        stop.set(); await runner; await asyncio.gather(stt.stop(), llm.stop(), tts.stop())
        for r in (rx, tx, stt_in, tts_out): r.close(); r.unlink()


@pytest.mark.asyncio
async def test_speech_during_greeting_is_kept_even_if_the_detector_never_fires():
    loop = asyncio.get_running_loop()
    rx, tx = FrameRing(None, 100), FrameRing(None, 400)
    stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
    stt = WorkerClient("stt", "ai_phone.services.backends.fake:CountingSTT", loop, stt_in, None, {})
    llm = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {"system_prompt": "s"})
    tts = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, tts_out, {})
    await asyncio.gather(stt.start(), llm.start(), tts.start())
    port, turns = StubPort(tx), []
    tc = TurnController(ConversationConfig(), port, stt, llm, tts, rx, tx, stt_in, tts_out, Endpointer(EnergyVad()),
                        [b"\x10\x00" * 320] * 100, "s", turns.append)    # 2 s greeting
    tc.bargein = BargeInDetector(min_ms=250, gain=1e9)                     # a gate that can never open
    stop = asyncio.Event(); runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        frames = speech_frames(1.0)                                        # caller talks 1.0 s inside the greeting
        for f in frames:
            rx.push(f); await asyncio.sleep(0.02)
        for _ in range(25):
            rx.push(SILENCE); await asyncio.sleep(0.02)
        await _drain_tx(tx, port, 3.0)
        assert turns, "no turn"
        n = int(turns[0].stt_text.split()[0])
        assert n >= 0.9 * len(frames) * 320, f"STT received {n} samples of {len(frames) * 320}"
    finally:
        stop.set(); await runner; await asyncio.gather(stt.stop(), llm.stop(), tts.stop())
        for r in (rx, tx, stt_in, tts_out): r.close(); r.unlink()
