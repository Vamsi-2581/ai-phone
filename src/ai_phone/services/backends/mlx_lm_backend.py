"""Qwen3.5 via mlx-lm, in the worker's own process. Thinking disabled through the chat template.

Prompt-cache reuse: Qwen3.5's cache is a hybrid (linear-attention state plus KV) and cannot be trimmed,
so the reusable part is the stable system prefix. It is evaluated once at warm-up into a snapshot;
each turn starts from a copy of that snapshot (0.2 ms) and evaluates only the history and the new
user turn. The prefix is found as the common token prefix of two probe prompts, so template quirks
(thinking blocks, generation markers) cannot desynchronise it.
"""
from __future__ import annotations

import copy
import time

from ai_phone.services.base import Message

T9 = "mlx-community/Qwen3.5-9B-MLX-4bit"
T4 = "mlx-community/Qwen3.5-4B-MLX-4bit"


class MlxLmLLM:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self._model = self._tok = None
        self._prefix_ids: list[int] = []
        self._snap = None
        self._cancelled: set[int] = set()
        self.last_timings: dict = {}
        self._system = "You are a phone assistant."

    def _ids(self, messages: list[Message]) -> list[int]:
        out = self._tok.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False)
        return list(out) if not isinstance(out, str) else self._tok.encode(out)

    def warmup(self, system_prompt: str = "", wire_memory: bool = True) -> dict:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache

        from ai_phone.services.backends.mlx_memory import begin_wiring, wire_resident

        t = time.monotonic_ns()
        begin_wiring(wire_memory)  # before the load: MLX wires buffers as they are allocated
        self._model, self._tok = load(self.model_id)
        wired = wire_resident(self._model, wire_memory)  # the weights stay resident through idle spells
        system = self._system = system_prompt or "You are a phone assistant."
        a = self._ids([{"role": "system", "content": system}, {"role": "user", "content": "a"}])
        b = self._ids([{"role": "system", "content": system}, {"role": "user", "content": "b"}])
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        self._prefix_ids = a[:n]
        cache = make_prompt_cache(self._model)
        if self._prefix_ids:
            mx.eval(self._model(mx.array(self._prefix_ids)[None], cache=cache))
        self._snap = cache
        list(self.stream(0, [{"role": "system", "content": system}, {"role": "user", "content": "Hello?"}], 8))
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6, "prefix_tokens": float(len(self._prefix_ids)), **wired}

    def stream(self, gen_id: int, messages: list[Message], max_tokens: int):
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import make_prompt_cache

        full = self._ids(messages)
        k = len(self._prefix_ids)
        if k and full[:k] == self._prefix_ids and len(full) > k:
            cache, new_ids, cached = copy.deepcopy(self._snap), full[k:], k
        else:
            cache, new_ids, cached = make_prompt_cache(self._model), full, 0
        t0 = time.monotonic_ns()
        t_first = None
        n = 0
        for r in stream_generate(self._model, self._tok, new_ids, max_tokens=max_tokens, prompt_cache=cache):
            if gen_id in self._cancelled:
                break
            if t_first is None:
                t_first = time.monotonic_ns()
            n += 1
            yield r.text
        t_end = time.monotonic_ns()
        first = t_first or t_end
        self.last_timings = {"prompt_eval_ms": (first - t0) / 1e6, "first_token_ms": (first - t0) / 1e6,
                             "gen_ms": (t_end - first) / 1e6, "tokens": n,
                             "prompt_tokens_new": len(new_ids), "prompt_tokens_cached": cached}
        self._cancelled.discard(gen_id)

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)

    def keep_warm(self) -> None:
        """One token from the cached prefix: reads every weight once, leaves last_timings alone."""
        saved = self.last_timings
        list(self.stream(-1, [{"role": "system", "content": self._system}, {"role": "user", "content": "Hi."}], 1))
        self.last_timings = saved


def make_t9() -> MlxLmLLM:
    return MlxLmLLM(T9)


def make_t4() -> MlxLmLLM:
    return MlxLmLLM(T4)
