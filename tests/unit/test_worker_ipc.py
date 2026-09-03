import asyncio
import os

import pytest

from ai_phone.services.client import WorkerClient
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME, tag_frame, untag_frame
from ai_phone.telephony.rings import FRAME_BYTES, FrameRing


async def _collect(client, until_ev, gen, timeout=10.0):
    out = []
    while True:
        e = await asyncio.wait_for(client.events.get(), timeout)
        out.append(e)
        if e.get("ev") == until_ev and e.get("gen") == gen:
            return out
        if e.get("ev") == "error":
            raise AssertionError(e)


@pytest.mark.asyncio
async def test_llm_worker_streams_tokens_and_done():
    loop = asyncio.get_running_loop()
    c = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {})
    t = await c.start()
    assert "warm_ms" in t
    gen = c.next_gen()
    c.send({"op": "llm.generate", "gen": gen, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50})
    evs = await _collect(c, "done", gen)
    text = "".join(e["text"] for e in evs if e["ev"] == "token")
    assert text == "Hello, this is a fake reply."
    await c.stop()


@pytest.mark.asyncio
async def test_cancel_drops_stale_generation():
    loop = asyncio.get_running_loop()
    c = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {})
    await c.start()
    g1 = c.next_gen()
    c.send({"op": "llm.generate", "gen": g1, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50})
    await asyncio.sleep(0.05)
    g2 = c.next_gen()
    c.cancel(g1)
    # tokens that arrived before next_gen() are legitimately g1 and already queued; from now on the
    # client drops anything tagged g1 on receipt, so only events after this drain matter
    while not c.events.empty():
        c.events.get_nowait()
    c.send({"op": "llm.generate", "gen": g2, "messages": [{"role": "user", "content": "again"}], "max_tokens": 50})
    evs = await _collect(c, "done", g2)
    assert all(e["gen"] == g2 for e in evs if e["ev"] in ("token", "done")), "stale g1 events leaked"
    assert not c.gen_is_current(g1) and c.gen_is_current(g2)
    await c.stop()


@pytest.mark.asyncio
async def test_tts_worker_writes_frames_to_ring():
    loop = asyncio.get_running_loop()
    out = FrameRing(None, 200, frame_bytes=TTS_OUT_FRAME)
    try:
        c = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, out, {})
        await c.start()
        gen = c.next_gen()
        c.send({"op": "tts.synth", "gen": gen, "seq": 0, "text": "Hello there."})
        evs = await _collect(c, "done", gen)
        n = sum(e["nframes"] for e in evs if e["ev"] == "chunk")
        assert n == 25 and out.available() == 25
        g, payload = untag_frame(out.pop())
        assert g == gen and len(payload) == FRAME_BYTES and payload != bytes(FRAME_BYTES)
        await c.stop()
    finally:
        out.close()
        out.unlink()


@pytest.mark.asyncio
async def test_stt_worker_push_finalize():
    loop = asyncio.get_running_loop()
    inp = FrameRing(None, 200, frame_bytes=STT_IN_FRAME)
    try:
        c = WorkerClient("stt", "ai_phone.services.backends.fake:FakeSTT", loop, inp, None, {})
        await c.start()
        gen = c.next_gen()
        for _ in range(10):
            inp.push(tag_frame(gen, b"\x01\x00" * 512))
        c.send({"op": "stt.push", "gen": gen, "nframes": 10})
        c.send({"op": "stt.finalize", "gen": gen})
        evs = await _collect(c, "final", gen)
        assert evs[-1]["text"].startswith("fake utterance")
        await c.stop()
    finally:
        inp.close()
        inp.unlink()


@pytest.mark.asyncio
async def test_worker_death_is_reported_once_as_an_error():
    loop = asyncio.get_running_loop()
    c = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {})
    await c.start()
    c._proc.kill()
    e = await asyncio.wait_for(c.events.get(), 10)
    assert e["ev"] == "error" and "disconnected" in e["message"]
    # deterministic: the error path must already have unregistered the reader, so nothing can be enqueued again
    assert not loop.remove_reader(c._parent.fileno()), "reader still registered on a dead pipe"
    assert c.events.empty()
    await c.stop()


@pytest.mark.asyncio
async def test_llm_worker_reports_brain_error_and_survives():
    loop = asyncio.get_running_loop()
    c = WorkerClient("llm", "ai_phone.services.backends.fake:FlakyLLM", loop, None, None, {})
    await c.start()
    g1 = c.next_gen()
    c.send({"op": "llm.generate", "gen": g1, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    evs = await _collect(c, "done", g1)
    assert "".join(e["text"] for e in evs if e["ev"] == "token")
    g2 = c.next_gen()
    c.send({"op": "llm.generate", "gen": g2, "messages": [{"role": "user", "content": "again"}], "max_tokens": 10})
    evs = await _collect(c, "brain_error", g2)
    assert evs[-1]["kind"] == "http_503" and not any(e["ev"] == "done" and e["gen"] == g2 for e in evs)
    g3 = c.next_gen()
    c.send({"op": "llm.generate", "gen": g3, "messages": [{"role": "user", "content": "third"}], "max_tokens": 10})
    evs = await _collect(c, "done", g3)   # the worker is still alive
    assert any(e["ev"] == "token" for e in evs)
    await c.stop()


@pytest.mark.asyncio
async def test_factory_args_reach_the_backend():
    loop = asyncio.get_running_loop()
    c = WorkerClient("llm", "ai_phone.services.backends.fake:FakeLLM", loop, None, None, {}, factory_args={"reply": "Custom."})
    await c.start()
    g = c.next_gen()
    c.send({"op": "llm.generate", "gen": g, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    evs = await _collect(c, "done", g)
    assert "".join(e["text"] for e in evs if e["ev"] == "token") == "Custom."
    await c.stop()


@pytest.mark.asyncio
async def test_workers_never_see_line_passwords(monkeypatch):
    monkeypatch.setenv("AI_PHONE_LINE_MAIN_PASSWORD", "hunter2")
    monkeypatch.setenv("TELNYX_API_KEY", "KEYabc")           # the README tells the user to export exactly this
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok")
    loop = asyncio.get_running_loop()
    c = WorkerClient("llm", "ai_phone.services.backends.fake:EnvProbeLLM", loop, None, None, {})
    await c.start()
    g = c.next_gen()
    c.send({"op": "llm.generate", "gen": g, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    evs = await _collect(c, "done", g)
    assert "".join(e["text"] for e in evs if e["ev"] == "token") == "clean"
    assert os.environ.get("TELNYX_API_KEY") == "KEYabc"  # the parent keeps its own environment
    await c.stop()


@pytest.mark.asyncio
async def test_idle_worker_keeps_warm_and_still_serves(tmp_path):
    loop = asyncio.get_running_loop()
    probe = tmp_path / "ticks"
    ring = FrameRing(None, 400, frame_bytes=STT_IN_FRAME)
    c = WorkerClient("stt", "ai_phone.services.backends.fake:KeepWarmProbeSTT", loop, ring, None, {},
                     factory_args={"path": str(probe)}, keep_warm_sec=0.2)
    await c.start()
    try:
        await asyncio.sleep(0.9)
        assert probe.exists() and probe.read_text().count("tick") >= 2, "no keep-warm ticks while idle"
        gen = c.next_gen()
        c.send({"op": "stt.finalize", "gen": gen})  # a request after ticks is served normally
        evs = await _collect(c, "final", gen)
        assert evs[-1]["ev"] == "final"
    finally:
        await c.stop()
        ring.close()
        ring.unlink()
