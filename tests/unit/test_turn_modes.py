import asyncio
import logging
import time

import pytest

from ai_phone.config import CallsConfig, ConversationConfig
from ai_phone.pipeline.prompts import OPENING_CUE
from ai_phone.pipeline.replies import Phrase, RelayPhrases, RelayReplies
from ai_phone.pipeline.turn import TurnController
from ai_phone.pipeline.vad import Endpointer, EnergyVad
from ai_phone.services.client import WorkerClient
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME
from ai_phone.telephony.rings import SILENCE, FrameRing
from tests.unit.test_turn_controller import StubPort, speech_frames

FILLER = b"\x11\x00" * 320
STILL = b"\x22\x00" * 320
BYE = b"\x33\x00" * 320
OPEN = b"\x44\x00" * 320
PROMPT = b"\x55\x00" * 320
SORRY = b"\x66\x00" * 320
PHRASES = RelayPhrases(Phrase("One moment.", (FILLER,) * 3), Phrase("Still with you, one moment.", (STILL,) * 3),
                       Phrase("Sorry, I have to go. Goodbye.", (BYE,) * 3))
F = "ai_phone.services.backends.fake:"
DISCLOSURE = "Hi, this is an AI assistant calling."
MODEL_REPLY = "Hello, this is a fake reply."


async def stack(loop, llm="FakeLLM", stt="FakeSTT", stt_args=None, relay=False, calls=None, conv=None, llm_args=None,
                **kw):
    rx, tx = FrameRing(None, 100), FrameRing(None, 400)
    stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
    sttc = WorkerClient("stt", F + stt, loop, stt_in, None, {}, factory_args=stt_args or {})
    llmc = WorkerClient("llm", F + llm, loop, None, None, {"system_prompt": "s"}, factory_args=llm_args or {})
    tts = WorkerClient("tts", F + "FakeTTS", loop, None, tts_out, {})
    await asyncio.gather(sttc.start(), llmc.start(), tts.start())
    heard, lines = [], []
    tc = TurnController(conv or ConversationConfig(endpoint_silence_ms=300), StubPort(tx), sttc, llmc, tts, rx, tx, stt_in,
                        tts_out, Endpointer(EnergyVad()), kw.pop("greeting", []), "sys", lambda t: None,
                        replies=RelayReplies(heard.append) if relay else None, calls=calls or CallsConfig(),
                        relay_phrases=PHRASES, on_line=lambda who, text, i: lines.append((who, text, i)),
                        rtp_last_rx_ns=time.monotonic_ns, **kw)
    return tc, (rx, tx, stt_in, tts_out), (sttc, llmc, tts), heard, lines


async def teardown(stop, runner, rings, clients):
    stop.set()
    await runner
    await asyncio.gather(*(c.stop() for c in clients))
    for r in rings:
        r.close()
        r.unlink()


async def speak(rx, seconds=0.6, silence_frames=25):
    for f in speech_frames(seconds):
        rx.push(f)
        await asyncio.sleep(0.02)
    for _ in range(silence_frames):
        rx.push(SILENCE)
        await asyncio.sleep(0.02)


async def drain(tx, seconds):
    """Play the part of PJSIP: pop 20 ms frames for `seconds`; return what was played."""
    out, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        f = tx.pop()
        if f is not None:
            out.append(f)
        await asyncio.sleep(0.02)
    return out


async def until(pred, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


# ---- the relay wait on a fake clock: no workers, the timers are driven with explicit timestamps -------------------
class NoWorker:
    kind, current_gen = "none", 0

    def set_gen(self, g):
        self.current_gen = g

    def cancel(self, g):
        pass

    def send(self, m):
        pass


def bare(direction="in"):
    tx, lines = FrameRing(None, 400), []
    w = NoWorker()
    tc = TurnController(ConversationConfig(), StubPort(tx), w, w, w, None, tx, None, None, Endpointer(EnergyVad()), [],
                        "sys", lambda t: None, replies=RelayReplies(lambda text: None), calls=CallsConfig(),
                        relay_phrases=PHRASES, on_line=lambda who, text, i: lines.append(text), direction=direction)
    tc.state = "LISTENING"
    return tc, tx, lines


def test_fillers_and_timeout_on_a_fake_clock():
    tc, tx, lines = bare()
    try:
        tc._relay_wait_from, tc._fillers_armed = 0, True
        tc._relay_timers(int(1.49e9))
        assert tx.available() == 0
        tc._relay_timers(int(1.5e9))
        assert tx.available() == 3 and lines == ["One moment."]
        tc._relay_timers(int(9.9e9))
        assert tx.available() == 3
        tc._relay_timers(int(10e9))
        assert tx.available() == 6 and lines[-1] == "Still with you, one moment."
        tc._relay_timers(int(24.9e9))
        assert tc.state == "LISTENING"
        tc._relay_timers(int(25e9))
        assert tc.state == "ENDING_DRAIN" and tc.end_reason == "relay_timeout" and tx.available() == 9
        assert lines[-1] == "Sorry, I have to go. Goodbye."
    finally:
        tx.close()
        tx.unlink()


def test_outbound_before_the_first_say_has_only_the_timeout():
    tc, tx, lines = bare("out")
    try:
        tc._relay_wait_from, tc._fillers_armed = 0, False
        tc._relay_timers(int(12e9))
        assert tx.available() == 0 and lines == []
        tc._relay_timers(int(25e9))
        assert tc.end_reason == "relay_timeout" and lines == ["Sorry, I have to go. Goodbye."]
    finally:
        tx.close()
        tx.unlink()


def test_say_after_the_call_is_over_resolves_ended():
    async def body():
        tc, tx, lines = bare()
        try:
            tc._closed = True
            assert tc.say("Too late.").result() == "ended"
        finally:
            tx.close()
            tx.unlink()
    asyncio.run(body())


# ---- with the fake workers --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_relay_hands_speech_over_and_does_not_reply_by_itself():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, calls=CallsConfig(relay_filler_after_ms=5000))
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        assert await until(lambda: heard == ["fake utterance 1"])
        assert tc.state == "LISTENING"
        assert await drain(rings[1], 0.5) == []           # no model reply, no filler yet
        assert ("them", "fake utterance 1", False) in lines
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_say_is_spoken_and_resolves_said():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        assert await until(lambda: heard)
        fut = tc.say("Sure. What name is it under?")
        played = await drain(rings[1], 2.0)
        assert len(played) >= 25 and await asyncio.wait_for(fut, 3) == "said"
        assert ("agent", "Sure. What name is it under?", False) in lines
        assert FILLER not in played
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_say_is_interrupted_by_the_other_person():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        fut = tc.say("This is a long sentence. It goes on. And on. And on. And on some more, for a while.")
        assert await until(lambda: tc.state == "SPEAKING")
        await speak(rings[0], seconds=0.8)                  # tx is not drained, so the reply is still playing
        assert await asyncio.wait_for(fut, 3) == "interrupted"
        assert any(who == "agent" and interrupted for who, _, interrupted in lines)
        assert await until(lambda: heard)                   # what they said still reaches the calling AI
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_fillers_then_timeout_when_the_calling_ai_stays_silent():
    loop = asyncio.get_running_loop()
    calls = CallsConfig(relay_filler_after_ms=200, relay_second_filler_sec=1, relay_reply_timeout_sec=2)
    tc, rings, clients, heard, lines = await stack(loop, relay=True, calls=calls)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        assert await until(lambda: heard)
        played = await drain(rings[1], 2.6)
        assert FILLER in played and STILL in played and BYE in played
        assert played.index(FILLER) < played.index(STILL) < played.index(BYE)
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "relay_timeout"
        agent = [t for w, t, _ in lines if w == "agent"]
        assert agent == ["One moment.", "Still with you, one moment.", "Sorry, I have to go. Goodbye."]
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_waiting_for_the_calling_ai_suppresses_the_silence_prompt():
    loop = asyncio.get_running_loop()
    conv = ConversationConfig(endpoint_silence_ms=300, silence_prompt_sec=2, silence_hangup_sec=1)
    tc, rings, clients, heard, lines = await stack(loop, relay=True, conv=conv, prompt_frames=[PROMPT] * 3,
                                                   calls=CallsConfig(relay_filler_after_ms=5000))
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        assert await until(lambda: heard)
        # without the wait flag the prompt would play 2 s after they finished, and the call would end 1 s later
        assert await drain(rings[1], 3.5) == [] and not tc.hangup_requested.is_set()
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_a_prompt_say_means_no_filler():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, calls=CallsConfig(relay_filler_after_ms=800))
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        assert await until(lambda: heard)
        tc.say("Okay.")
        assert FILLER not in await drain(rings[1], 1.5)
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_speech_during_a_slow_transcript_is_kept_not_treated_as_barge_in():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, stt="SlowSTT", stt_args={"delay_s": "2"},
                                                   calls=CallsConfig(relay_filler_after_ms=10000, relay_second_filler_sec=20,
                                                                     relay_reply_timeout_sec=30))
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])                      # utterance 1: its transcript takes 2 s
        await speak(rings[0])                      # utterance 2 starts and ends while utterance 1 is transcribed
        assert await until(lambda: heard == ["fake utterance 1", "fake utterance 2"], 8)
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_speech_over_two_further_utterances_is_kept_and_transcribed_together():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, stt="SlowSTT", stt_args={"delay_s": "4"},
                                                   calls=CallsConfig(relay_filler_after_ms=10000, relay_second_filler_sec=20,
                                                                     relay_reply_timeout_sec=30))
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])                      # utterance 1: its transcript takes 4 s
        await speak(rings[0])                      # utterance 2 ends while utterance 1 is transcribed
        after_two = len(tc._candidate)
        await speak(rings[0])                      # utterance 3 starts and ends too: utterance 2 must not be dropped
        assert heard == [] and tc._held_end is not None
        assert len(tc._candidate) > 1.5 * after_two
        assert await until(lambda: heard == ["fake utterance 1", "fake utterance 2"], 12)
        await asyncio.sleep(0.5)
        assert heard == ["fake utterance 1", "fake utterance 2"]   # 2 and 3 went to the transcriber as one
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_a_held_utterance_gets_its_full_reply_timeout_from_when_it_is_transcribed():
    loop = asyncio.get_running_loop()
    # utterance 1 ends at ~0.9 s and its transcript arrives at ~3.9 s; utterance 2 ended at ~2.0 s and is only sent to
    # the transcriber at ~3.9 s, so its transcript arrives at ~6.9 s. Counted from ~2.0 s, the 4 s timeout would fire
    # at ~6.0 s and lose it.
    conv = ConversationConfig(endpoint_silence_ms=300, reply_timeout_sec=4)
    tc, rings, clients, heard, lines = await stack(loop, relay=True, stt="SlowSTT", stt_args={"delay_s": "3"}, conv=conv,
                                                   calls=CallsConfig(relay_filler_after_ms=20000, relay_second_filler_sec=25,
                                                                     relay_reply_timeout_sec=30))
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        await speak(rings[0])
        assert await until(lambda: heard == ["fake utterance 1", "fake utterance 2"], 10)
        assert tc._brain_errors == 0
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_outbound_relay_timeout_counts_from_answer_when_they_talk_during_the_disclosure():
    loop = asyncio.get_running_loop()
    calls = CallsConfig(relay_filler_after_ms=200, relay_second_filler_sec=1, relay_reply_timeout_sec=2)
    tc, rings, clients, heard, lines = await stack(loop, relay=True, calls=calls, opening_frames=[OPEN] * 50,
                                                   direction="out", opening_text=DISCLOSURE)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        await speak(rings[0], seconds=0.8)         # tx is not drained: they talk while the disclosure is still queued
        assert rings[1].available() == 50 and heard == []
        # the disclosure takes ~1.1 s and the transcript follows at once (~2.4 s after answer): counted from the
        # answer the goodbye is due at once, counted from the transcript it would come ~2 s later, after this window
        played = await drain(rings[1], 2.0)
        assert played[:50] == [OPEN] * 50 and heard == ["fake utterance 1"]
        assert BYE in played and FILLER not in played and STILL not in played
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "relay_timeout"
        agent = [t for w, t, _ in lines if w == "agent"]
        assert agent == [DISCLOSURE, "Sorry, I have to go. Goodbye."]
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_relay_speech_during_the_disclosure_is_heard_after_it():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, opening_frames=[OPEN] * 50, direction="out",
                                                   opening_text=DISCLOSURE)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        await speak(rings[0], seconds=0.8)         # "Hello? Who is this?" while the disclosure is still queued
        assert tc.state == "OUTBOUND_OPENING" and rings[1].available() == 50 and heard == []
        assert tc._held_end is not None            # it ended; it is transcribed once the disclosure has played
        played = await drain(rings[1], 1.5)
        assert played == [OPEN] * 50               # nothing cut, nothing added
        assert await until(lambda: heard == ["fake utterance 1"])
        assert tc._relay_wait_from == tc._start_ns
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_a_short_hello_during_the_disclosure_keeps_the_opening(caplog):
    caplog.set_level(logging.INFO, logger="ai-phone.turn")
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, opening_frames=[OPEN] * 50, direction="out",
                                                   opening_text=DISCLOSURE, generate_opening=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        await speak(rings[0], seconds=0.8)         # "Hello?" at pickup; it has ended before the disclosure has
        assert tc.state == "OUTBOUND_OPENING" and rings[1].available() == 50 and tc._held_end is not None
        played = await drain(rings[1], 3.0)
        assert played[:50] == [OPEN] * 50 and len(played) == 75   # the disclosure, then the opening, no gap
        assert await until(lambda: tc.state == "LISTENING")
        await asyncio.sleep(0.3)
        assert tc.history == [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": MODEL_REPLY}]
        assert lines == [("agent", DISCLOSURE, False), ("agent", MODEL_REPLY, False)]   # the greeting is not transcribed
        notes = [r.getMessage() for r in caplog.records if "treated as a greeting" in r.getMessage()]
        assert len(notes) == 1 and notes[0].startswith("short speech during the disclosure treated as a greeting")
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_still_talking_when_the_disclosure_ends_cancels_the_opening():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, opening_frames=[OPEN] * 25, direction="out",
                                                   opening_text=DISCLOSURE, generate_opening=True)
    rx, tx = rings[0], rings[1]
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.3)                   # the model's opening is ready and held
        played = []
        frames = speech_frames(1.2) + [SILENCE] * 25
        for i, f in enumerate(frames):             # the disclosure plays out from the 6th frame on, while they talk
            rx.push(f)
            if i >= 5 and (p := tx.pop()) is not None:
                played.append(p)
            if i == 40:
                assert played == [OPEN] * 25 and tc._held_end is None and tc.history[-1]["content"] == "[interrupted]"
            await asyncio.sleep(0.02)
        played += await drain(tx, 2.5)
        assert played[:25] == [OPEN] * 25 and len(played) == 50   # the disclosure, then one reply: the opening was dropped
        assert await until(lambda: tc.state == "LISTENING" and len(tc.history) == 4)
        assert tc.history == [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": "[interrupted]"},
                              {"role": "user", "content": "fake utterance 1"},
                              {"role": "assistant", "content": MODEL_REPLY}]
        assert lines == [("agent", DISCLOSURE, False), ("them", "fake utterance 1", False), ("agent", MODEL_REPLY, False)]
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_long_speech_during_the_disclosure_cancels_the_opening_then_the_model_replies():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, opening_frames=[OPEN] * 50, direction="out",
                                                   opening_text=DISCLOSURE, generate_opening=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        await speak(rings[0], seconds=2.0)         # more than a greeting; the model's opening is ready and held meanwhile
        assert tc.state == "OUTBOUND_OPENING" and rings[1].available() == 50 and tc._held_end is not None
        played = await drain(rings[1], 3.0)
        assert played[:50] == [OPEN] * 50 and len(played) == 75   # the disclosure, then one reply: the opening was dropped
        assert await until(lambda: tc.state == "LISTENING" and len(tc.history) == 4)
        assert tc.history == [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": "[interrupted]"},
                              {"role": "user", "content": "fake utterance 1"},
                              {"role": "assistant", "content": MODEL_REPLY}]
        assert lines == [("agent", DISCLOSURE, False), ("them", "fake utterance 1", False), ("agent", MODEL_REPLY, False)]
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_inbound_greeting_is_still_cut_by_barge_in():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, greeting=[OPEN] * 100)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.1)
        await speak(rings[0], seconds=0.8)
        assert rings[1].available() < 100          # the greeting was flushed
        played = await drain(rings[1], 2.0)
        assert OPEN not in played and len(played) >= 25   # the model answered instead
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_brain_error_during_the_opening_keeps_the_disclosure():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, llm="FlakyLLM", llm_args={"fail_calls": "1"},
                                                   opening_frames=[OPEN] * 50, direction="out", opening_text=DISCLOSURE,
                                                   generate_opening=True, apology_frames=[SORRY] * 3)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        assert await until(lambda: tc._brain_errors == 1)   # the opening failed while the disclosure is queued
        assert tc.state == "OUTBOUND_OPENING" and rings[1].available() == 53
        played = await drain(rings[1], 1.5)
        assert played == [OPEN] * 50 + [SORRY] * 3
        assert await until(lambda: tc.state == "LISTENING")
        assert lines == [("agent", DISCLOSURE, False)]
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_a_brain_error_mid_reply_logs_what_was_said_without_the_marker():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, llm="PartialFailLLM", apology_frames=[SORRY] * 3)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        assert await until(lambda: ("agent", "Sure, one moment.", True) in lines)
        assert tc.history[-1] == {"role": "assistant", "content": "Sure, one moment. [interrupted]"}
        assert all("[end]" not in text.lower() for _, text, _ in lines)
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_an_interrupted_hangup_say_still_ends_the_call():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        fut = tc.say("Thanks for your time. I will let you go now, and have a very good evening.",
                     end_reason="hangup_command")
        assert await until(lambda: tc.state == "SPEAKING")
        await speak(rings[0], seconds=0.8)         # tx is not drained, so the goodbye is still playing
        assert await asyncio.wait_for(fut, 3) == "interrupted"
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "hangup_command"
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_a_lowercase_end_marker_ends_the_call_and_stays_out_of_the_transcript():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, llm_args={"reply": "Goodbye, thanks. [end]"})
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        played = await drain(rings[1], 2.0)
        assert len(played) >= 25
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "agent"
        assert ("agent", "Goodbye, thanks.", False) in lines
        assert all("[end]" not in text.lower() for _, text, _ in lines)
        assert all("[end]" not in m["content"].lower() for m in tc.history)
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_model_end_marker_ends_the_call_after_the_goodbye():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, llm="EndingLLM")
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        played = await drain(rings[1], 2.0)
        assert len(played) >= 25                            # the goodbye played before the end
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "agent"
        assert ("agent", "Goodbye, thanks.", False) in lines
        assert all("[END]" not in text for _, text, _ in lines)
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_outbound_agent_opening_follows_the_disclosure():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, opening_frames=[OPEN] * 5, direction="out",
                                                   opening_text="Hi, this is an AI assistant calling.", generate_opening=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        played = await drain(rings[1], 1.5)
        assert played[:5] == [OPEN] * 5 and len(played) > 5 and OPEN not in played[5:]
        assert tc.history[0] == {"role": "user", "content": OPENING_CUE}
        assert lines[0] == ("agent", "Hi, this is an AI assistant calling.", False)
        assert await until(lambda: ("agent", "Hello, this is a fake reply.", False) in lines)
        assert await until(lambda: tc.state == "LISTENING")
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_opening_audio_waits_for_a_long_disclosure():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, opening_frames=[OPEN] * 50, direction="out",
                                                   opening_text="Hi, this is an AI assistant calling.", generate_opening=True)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.5)                            # the model's sentence is ready long before the disclosure ends
        assert tc.state == "OUTBOUND_OPENING"
        played = await drain(rings[1], 2.5)
        assert played[:50] == [OPEN] * 50 and len(played) >= 75 and OPEN not in played[50:]
        assert await until(lambda: tc.state == "LISTENING")
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_say_with_end_reason_hangs_up_after_speaking_and_waits_for_the_opening():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, opening_frames=[OPEN] * 10, direction="out",
                                                   opening_text="Hi, this is an AI assistant calling.")
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        fut = tc.say("Bye now.", end_reason="hangup_command")
        played = await drain(rings[1], 1.5)
        assert played[:10] == [OPEN] * 10 and len(played) > 10
        assert await asyncio.wait_for(fut, 3) == "said"
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "hangup_command"
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_pending_says_resolve_ended_when_the_call_stops():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, relay=True, opening_frames=[OPEN] * 200, direction="out")
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    fut = tc.say("You will never hear this.")
    await asyncio.sleep(0.1)
    await teardown(stop, runner, rings, clients)
    assert fut.result() == "ended"
    assert tc.say("Too late.").result() == "ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply, spoken", [("Thanks, bye [END].", "Thanks, bye."), ("Goodbye. [END].", "Goodbye.")])
async def test_the_transcript_has_no_space_or_stray_punctuation_where_the_marker_was(reply, spoken):
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, llm_args={"reply": reply})
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await speak(rings[0])
        await drain(rings[1], 2.0)
        assert await until(lambda: tc.hangup_requested.is_set()) and tc.end_reason == "agent"
        assert [(w, t) for w, t, _ in lines if w == "agent"] == [("agent", spoken)]
        assert tc.history[-1] == {"role": "assistant", "content": spoken}
    finally:
        await teardown(stop, runner, rings, clients)


def test_a_say_turn_adds_no_speech_or_model_stage_to_the_latency_record():
    async def body():
        tc, tx, lines = bare()
        try:
            tc.say("Sure. What name is it under?")
            tc._pump_says(int(5e9))
            assert tc.state == "THINKING" and tc.timer.t0_end == int(5e9)   # the reply timeout still has its start
            r = tc.timer.to_record("c", 0)
            assert r["stt_ms"] is None and r["llm_first_clause_ms"] is None and r["first_audible_ms"] is None
        finally:
            tx.close()
            tx.unlink()
    asyncio.run(body())


@pytest.mark.asyncio
async def test_a_brain_error_mid_opening_writes_nothing_that_was_never_spoken():
    loop = asyncio.get_running_loop()
    tc, rings, clients, heard, lines = await stack(loop, llm="PartialFailLLM", opening_frames=[OPEN] * 50,
                                                   direction="out", opening_text=DISCLOSURE, generate_opening=True,
                                                   apology_frames=[SORRY] * 3)
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        assert await until(lambda: tc._brain_errors == 1)   # "Sure, one moment." was written, then the brain failed
        assert tc.state == "OUTBOUND_OPENING"
        played = await drain(rings[1], 1.5)
        assert played == [OPEN] * 50 + [SORRY] * 3          # the opening's audio was held, then dropped
        assert tc.history == [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": "[interrupted]"}]
        assert lines == [("agent", DISCLOSURE, False)]
    finally:
        await teardown(stop, runner, rings, clients)


@pytest.mark.asyncio
async def test_a_worker_that_dies_during_the_disclosure_lets_it_play_then_the_goodbye():
    loop = asyncio.get_running_loop()
    goodbye = b"\x77\x00" * 320
    tc, rings, clients, heard, lines = await stack(loop, opening_frames=[OPEN] * 150, direction="out",
                                                   opening_text=DISCLOSURE, generate_opening=True,
                                                   goodbye_frames=[goodbye] * 5)
    tx = rings[1]
    stop = asyncio.Event()
    runner = asyncio.create_task(tc.run(stop))
    try:
        await asyncio.sleep(0.2)
        clients[1]._proc.kill()                             # the model worker dies 0.2 s into a 3 s disclosure
        assert await until(lambda: tc.state == "ENDING_DRAIN")
        assert tx.available() == 155                        # nothing flushed: the disclosure, then the goodbye
        # 155 frames at 20 ms each, plus 1 s: the old fixed 3 s would hang up before the goodbye had played
        assert tc._drain_deadline_ns - time.monotonic_ns() > 3.5e9
        played = []
        while not tc.hangup_requested.is_set() and len(played) < 200:
            for _ in range(2):                              # faster than real time: the deadline is checked above
                f = tx.pop()
                if f is not None:
                    played.append(f)
            await asyncio.sleep(0.02)
        assert await until(lambda: tc.hangup_requested.is_set())
        assert played == [OPEN] * 150 + [goodbye] * 5 and tc.end_reason == "worker error: llm"
    finally:
        await teardown(stop, runner, rings, clients)
