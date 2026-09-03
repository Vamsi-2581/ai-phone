"""Any OpenAI-compatible /v1/chat/completions endpoint as the brain (LM Studio, llama.cpp, vLLM, your own agent).

Streams `delta.content` tokens into the same clause chunker as the local model. Failures raise BrainError with a
kind the controller can log; there are no retries because the caller is waiting on the line.
"""
from __future__ import annotations

import json
import queue
import threading
import time

import httpx

from ai_phone.services.base import Message
from ai_phone.services.errors import BrainError


class OpenAICompatLLM:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, connect_timeout_ms: int = 2000,
                 first_token_timeout_ms: int = 4000, stream_idle_timeout_ms: int = 4000, max_response_chars: int = 2000,
                 http: httpx.Client | None = None):
        self.base_url, self.model, self.max_chars = base_url.rstrip("/"), model, max_response_chars
        self.first_token_s = first_token_timeout_ms / 1000
        self.timeout = httpx.Timeout(connect=connect_timeout_ms / 1000, read=stream_idle_timeout_ms / 1000, write=5.0, pool=5.0)
        self.http = http or httpx.Client(timeout=self.timeout, follow_redirects=False)
        self.http.follow_redirects = False
        if api_key:
            self.http.headers["Authorization"] = f"Bearer {api_key}"
        self._cancelled: set[int] = set()
        self.last_timings: dict = {}

    def warmup(self, system_prompt: str = "") -> dict:
        t = time.monotonic_ns()
        list(self.stream(0, [{"role": "system", "content": system_prompt or "You are a phone assistant."},
                            {"role": "user", "content": "Say hi."}], 4))
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6, "endpoint": self.base_url}

    def stream(self, gen_id: int, messages: list[Message], max_tokens: int):
        """The HTTP stream runs in a helper thread that hands lines over a queue; this generator owns the clock:
        the first token must arrive within first_token_timeout (headers, keepalives and silence all count),
        later tokens within stream_idle_timeout. On a deadline or cancel the response is closed from here."""
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "stream": True}
        t0 = time.monotonic_ns()
        t_first = None
        n = chars = 0
        q: queue.Queue = queue.Queue()
        holder: dict = {}

        def pump():
            try:
                with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body, timeout=self.timeout) as r:
                    holder["response"] = r
                    q.put(("status", r.status_code))
                    for line in r.iter_lines():
                        q.put(("line", line))
                q.put(("eof", None))
            except BaseException as e:  # reported to the consumer, which decides what it means
                q.put(("error", e))

        worker = threading.Thread(target=pump, name="brain-stream", daemon=True)
        worker.start()

        def close_response():
            r = holder.get("response")
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass

        def next_item(timeout_s: float, kind_on_timeout: str):
            try:
                return q.get(timeout=timeout_s)
            except queue.Empty:
                close_response()
                raise BrainError(kind_on_timeout) from None

        deadline_first = time.monotonic() + self.first_token_s
        idle_s = self.timeout.read or self.first_token_s
        finished = False
        try:
            while True:
                if gen_id in self._cancelled:
                    close_response()
                    self._cancelled.discard(gen_id)
                    return
                if t_first is None:
                    remaining = deadline_first - time.monotonic()
                    if remaining <= 0:
                        close_response()
                        raise BrainError("first_token_timeout")
                    kind, payload = next_item(remaining, "first_token_timeout")
                else:
                    kind, payload = next_item(idle_s, "idle_timeout")
                if kind == "status":
                    if payload >= 300:
                        close_response()
                        raise BrainError(f"http_{payload}")
                    continue
                if kind == "error":
                    e = payload
                    if isinstance(e, httpx.ConnectError):
                        raise BrainError("connect", str(e)[:80]) from None
                    if isinstance(e, httpx.ReadTimeout):
                        raise BrainError("first_token_timeout" if t_first is None else "idle_timeout") from None
                    if isinstance(e, httpx.HTTPError):
                        raise BrainError("connect", type(e).__name__) from None
                    raise BrainError("malformed", type(e).__name__) from None
                if kind == "eof":
                    if not finished:
                        raise BrainError("malformed", "stream ended without [DONE]")
                    return
                line = payload
                if not line.startswith("data:"):
                    continue  # SSE comments / keepalives
                data = line[5:].strip()
                if data == "[DONE]":
                    finished = True
                    continue
                try:
                    piece = json.loads(data)["choices"][0].get("delta", {}).get("content")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
                    raise BrainError("malformed", str(e)[:80]) from None
                if piece is None or piece == "":
                    continue
                if not isinstance(piece, str):
                    raise BrainError("malformed", f"delta.content is {type(piece).__name__}, not text")
                if t_first is None:
                    t_first = time.monotonic_ns()
                n += 1
                chars += len(piece)
                if chars > self.max_chars:
                    close_response()
                    raise BrainError("too_long", f"> {self.max_chars} chars")
                yield piece
        finally:
            close_response()
            t_end = time.monotonic_ns()
            first = t_first or t_end
            self.last_timings = {"prompt_eval_ms": (first - t0) / 1e6, "first_token_ms": (first - t0) / 1e6,
                                 "gen_ms": (t_end - first) / 1e6, "tokens": n}
            self._cancelled.discard(gen_id)

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)


def make_openai(**kw) -> OpenAICompatLLM:
    return OpenAICompatLLM(**kw)
