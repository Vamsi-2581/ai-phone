"""Qwen3.5 via mlx-lm, in the worker's own process. Thinking disabled through the chat template."""
from __future__ import annotations

import time

from ai_phone.services.base import Message

T9 = "mlx-community/Qwen3.5-9B-MLX-4bit"
T4 = "mlx-community/Qwen3.5-4B-MLX-4bit"


class MlxLmLLM:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self._model = self._tok = None
        self._system = ""
        self._cancelled: set[int] = set()
        self.last_timings: dict = {}

    def warmup(self, system_prompt: str = "") -> dict:
        from mlx_lm import load

        t = time.monotonic_ns()
        self._model, self._tok = load(self.model_id)
        self._system = system_prompt
        list(self.stream(0, [{"role": "system", "content": system_prompt or "You are a phone assistant."},
                             {"role": "user", "content": "Hello?"}], 8))
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6}

    def stream(self, gen_id: int, messages: list[Message], max_tokens: int):
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import make_prompt_cache

        prompt = self._tok.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False)
        cache = make_prompt_cache(self._model)  # per-turn cache; prefix reuse across turns is the next optimisation
        t0 = time.monotonic_ns()
        t_first = None
        n = 0
        for r in stream_generate(self._model, self._tok, prompt, max_tokens=max_tokens, prompt_cache=cache):
            if gen_id in self._cancelled:
                break
            if t_first is None:
                t_first = time.monotonic_ns()
            n += 1
            yield r.text
        t_end = time.monotonic_ns()
        first = t_first or t_end
        self.last_timings = {"prompt_eval_ms": (first - t0) / 1e6, "first_token_ms": (first - t0) / 1e6,
                             "gen_ms": (t_end - first) / 1e6, "tokens": n}
        self._cancelled.discard(gen_id)

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)


def make_t9() -> MlxLmLLM:
    return MlxLmLLM(T9)


def make_t4() -> MlxLmLLM:
    return MlxLmLLM(T4)
