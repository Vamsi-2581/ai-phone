import json
import time

import httpx
import pytest

from ai_phone.services.backends.openai_compat import OpenAICompatLLM
from ai_phone.services.errors import BrainError


def _sse(chunks, done=True, delay=0.0):
    def gen():
        for c in chunks:
            if delay:
                time.sleep(delay)
            yield f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n".encode()
        if done:
            yield b"data: [DONE]\n\n"
    return gen()


def _backend(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:1234/v1")
    return OpenAICompatLLM("http://127.0.0.1:1234/v1", "m", http=client, **kw)


def test_streams_delta_content_tokens():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(_sse(["Hel", "lo", "."]))), headers={"content-type": "text/event-stream"})

    b = _backend(handler)
    toks = list(b.stream(1, [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], 50))
    assert toks == ["Hel", "lo", "."]
    assert seen["body"]["stream"] is True and seen["body"]["model"] == "m" and seen["body"]["max_tokens"] == 50
    assert b.last_timings["tokens"] == 3 and "first_token_ms" in b.last_timings


def test_non_2xx_is_a_brain_error_with_status_kind():
    b = _backend(lambda req: httpx.Response(503, text="down"))
    with pytest.raises(BrainError) as e:
        list(b.stream(1, [{"role": "user", "content": "hi"}], 10))
    assert e.value.kind == "http_503"


def test_malformed_stream():
    b = _backend(lambda req: httpx.Response(200, content=b"data: {not json\n\n"))
    with pytest.raises(BrainError) as e:
        list(b.stream(1, [{"role": "user", "content": "hi"}], 10))
    assert e.value.kind == "malformed"


def test_too_long_is_cut_off():
    b = _backend(lambda req: httpx.Response(200, content=b"".join(_sse(["x" * 40] * 10))), max_response_chars=100)
    with pytest.raises(BrainError) as e:
        list(b.stream(1, [{"role": "user", "content": "hi"}], 10))
    assert e.value.kind == "too_long"


def test_connect_failure():
    def handler(req):
        raise httpx.ConnectError("refused")
    b = _backend(handler)
    with pytest.raises(BrainError) as e:
        list(b.stream(1, [{"role": "user", "content": "hi"}], 10))
    assert e.value.kind == "connect"


def test_api_key_goes_in_the_header_and_redirects_are_off():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, content=b"".join(_sse(["ok"])))

    b = _backend(handler, api_key="sk-test")
    list(b.stream(1, [{"role": "user", "content": "hi"}], 10))
    assert seen["auth"] == "Bearer sk-test" and b.http.follow_redirects is False


def test_cancel_stops_the_stream():
    b = _backend(lambda req: httpx.Response(200, content=b"".join(_sse(["a", "b", "c", "d"]))))
    out = []
    for t in b.stream(7, [{"role": "user", "content": "hi"}], 10):
        out.append(t)
        b.cancel(7)
    assert out == ["a"]
