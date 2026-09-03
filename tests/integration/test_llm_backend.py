import os

import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
pytestmark = pytest.mark.integration
SYS = "You are ai-phone, a phone assistant. Answer in one short sentence."


def test_streams_reply_without_thinking_and_reports_timings():
    from ai_phone.services.backends.mlx_lm_backend import make_t9

    llm = make_t9()
    t = llm.warmup(SYS)
    assert t["warm_ms"] > 0
    toks = list(llm.stream(3, [{"role": "system", "content": SYS}, {"role": "user", "content": "Say hello in five words."}], 60))
    text = "".join(toks)
    assert text and "<think>" not in text
    assert llm.last_timings["tokens"] >= 3 and llm.last_timings["first_token_ms"] > 0


def test_cancel_stops_stream_early():
    from ai_phone.services.backends.mlx_lm_backend import make_t9

    llm = make_t9()
    llm.warmup(SYS)
    out = []
    for tok in llm.stream(4, [{"role": "system", "content": SYS}, {"role": "user", "content": "Count slowly from one to fifty."}], 200):
        out.append(tok)
        if len(out) == 3:
            llm.cancel(4)
    assert len(out) <= 4


def test_prompt_cache_reuses_the_system_prefix():
    from ai_phone.services.backends.mlx_lm_backend import make_t4

    llm = make_t4()
    llm.warmup(SYS)
    hist = [{"role": "system", "content": SYS}, {"role": "user", "content": "What time is it right now?"}]
    reply = "".join(llm.stream(5, hist, 40))
    t1 = dict(llm.last_timings)
    hist += [{"role": "assistant", "content": reply}, {"role": "user", "content": "And what day is it?"}]
    reply2 = "".join(llm.stream(6, hist, 40))
    t2 = dict(llm.last_timings)
    assert reply2.strip()
    assert t1["prompt_tokens_cached"] > 0 and t2["prompt_tokens_cached"] == t1["prompt_tokens_cached"], (t1, t2)
    assert t1["prompt_tokens_new"] < t1["prompt_tokens_cached"] + t1["prompt_tokens_new"], "system prefix must come from the snapshot"
