"""Any OpenAI-compatible /v1/chat/completions endpoint as the brain (LM Studio, llama.cpp, vLLM, your own agent).

Streams `delta.content` tokens into the same clause chunker as the local model. Failures raise BrainError with a
kind the controller can log; there are no retries because the caller is waiting on the line.
"""
from __future__ import annotations

import json
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
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "stream": True}
        t0 = time.monotonic_ns()
        deadline = time.monotonic() + self.first_token_s
        t_first = None
        n = chars = 0
        finished = False
        first_token_timer: threading.Timer | None = None
        timed_out = threading.Event()
        try:
            with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body, timeout=self.timeout) as r:
                if r.status_code >= 300:
                    raise BrainError(f"http_{r.status_code}")
                # the idle read timeout governs silence between reads; the first token has its own, shorter deadline,
                # enforced by closing the response from a timer so neither silence nor SSE keepalives can stretch it
                def _expire():
                    timed_out.set()
                    r.close()
                first_token_timer = threading.Timer(self.first_token_s, _expire)
                first_token_timer.daemon = True
                first_token_timer.start()
                try:
                    for line in r.iter_lines():
                        if gen_id in self._cancelled:
                            raise BrainError("cancelled")
                        if t_first is None and time.monotonic() > deadline:
                            raise BrainError("first_token_timeout")
                        if not line.startswith("data:"):
                            continue  # SSE comments / keepalives
                        data = line[5:].strip()
                        if data == "[DONE]":
                            finished = True
                            break
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
                            first_token_timer.cancel()
                        n += 1
                        chars += len(piece)
                        if chars > self.max_chars:
                            raise BrainError("too_long", f"> {self.max_chars} chars")
                        yield piece
                except BrainError:
                    raise  # our own verdicts pass through (BrainError is a RuntimeError, which the next clause catches)
                except (httpx.StreamError, httpx.TransportError, RuntimeError) as e:
                    if timed_out.is_set():  # the first-token timer closed the response
                        raise BrainError("first_token_timeout") from None
                    raise BrainError("malformed", type(e).__name__) from None
            if not finished:
                raise BrainError("malformed", "stream ended without [DONE]")
        except httpx.ConnectError as e:
            raise BrainError("connect", str(e)[:80]) from None
        except httpx.ReadTimeout:
            raise BrainError("first_token_timeout" if t_first is None else "idle_timeout") from None
        except httpx.HTTPError as e:
            raise BrainError("connect", type(e).__name__) from None
        except BrainError as e:
            if e.kind == "cancelled":
                self._cancelled.discard(gen_id)
                return
            raise
        finally:
            if first_token_timer is not None:
                first_token_timer.cancel()
            t_end = time.monotonic_ns()
            first = t_first or t_end
            self.last_timings = {"prompt_eval_ms": (first - t0) / 1e6, "first_token_ms": (first - t0) / 1e6,
                                 "gen_ms": (t_end - first) / 1e6, "tokens": n}
        self._cancelled.discard(gen_id)

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)


def make_openai(**kw) -> OpenAICompatLLM:
    return OpenAICompatLLM(**kw)
