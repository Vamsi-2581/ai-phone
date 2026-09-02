import asyncio

import pytest

from ai_phone.services.client import WorkerClient
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
    out = FrameRing(None, 200)
    try:
        c = WorkerClient("tts", "ai_phone.services.backends.fake:FakeTTS", loop, None, out, {})
        await c.start()
        gen = c.next_gen()
        c.send({"op": "tts.synth", "gen": gen, "seq": 0, "text": "Hello there."})
        evs = await _collect(c, "done", gen)
        n = sum(e["nframes"] for e in evs if e["ev"] == "chunk")
        assert n == 25 and out.available() == 25
        assert out.pop() != bytes(FRAME_BYTES)
        await c.stop()
    finally:
        out.close()
        out.unlink()


@pytest.mark.asyncio
async def test_stt_worker_push_finalize():
    loop = asyncio.get_running_loop()
    inp = FrameRing(None, 200, frame_bytes=1024)
    try:
        c = WorkerClient("stt", "ai_phone.services.backends.fake:FakeSTT", loop, inp, None, {})
        await c.start()
        gen = c.next_gen()
        for _ in range(10):
            inp.push(b"\x01\x00" * 512)
        c.send({"op": "stt.push", "gen": gen, "nframes": 10})
        c.send({"op": "stt.finalize", "gen": gen})
        evs = await _collect(c, "final", gen)
        assert evs[-1]["text"].startswith("fake utterance")
        await c.stop()
    finally:
        inp.close()
        inp.unlink()
