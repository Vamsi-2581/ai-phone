"""Any mlx-lm model (the catalog in models.py, or a repo named by hand) in the worker's own process.
Thinking disabled through the chat template where the model has it.

Prompt-cache reuse: a hybrid cache (linear-attention state plus KV, as Qwen3.5 has) cannot be trimmed,
so the reusable part is the prefix every call shares: the fixed phone instructions plus the call-section
header. It is evaluated once at warm-up into a snapshot; each turn starts from a copy of that snapshot
(0.2 ms) and evaluates only the call section, the history and the new user turn. The prefix is found as
the common token prefix of two probe prompts whose call sections differ from their first character, so
template quirks (thinking blocks, generation markers) cannot desynchronise it.
"""
from __future__ import annotations

import copy
import logging
import time

from ai_phone.pipeline.prompts import cache_probe_sections, system_prompt as compose_prompt
from ai_phone.services.base import Message

log = logging.getLogger(__name__)
T9 = "mlx-community/Qwen3.5-9B-MLX-4bit"  # models.LLM["t9"]
T4 = "mlx-community/Qwen3.5-4B-MLX-4bit"  # models.LLM["t4"]


class MlxLmLLM:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self._model = self._tok = None
        self._prefix_ids: list[int] = []
        self._snap = None
        self._cancelled: set[int] = set()
        self._fold_system = False  # this model's chat template has no system role
        self.last_timings: dict = {}
        self._system = "You are a phone assistant."

    def _ids(self, messages: list[Message]) -> list[int]:
        if self._fold_system:
            messages = fold_system(messages)
        out = self._tok.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=False)
        return list(out) if not isinstance(out, str) else self._tok.encode(out)

    def _system_role_supported(self) -> bool:
        """Whether this model's chat template accepts a standalone system message; the Gemma and Mistral instruct
        families are the well-known ones that refuse it. A template that fails without the system message too is
        broken for some other reason, and saying so here would only hide the real error."""
        probe = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        try:
            self._tok.apply_chat_template(probe, add_generation_prompt=True, enable_thinking=False)
            return True
        except Exception:
            pass
        try:
            self._tok.apply_chat_template(probe[1:], add_generation_prompt=True, enable_thinking=False)
        except Exception:
            return True
        return False

    def _note_system_role(self) -> None:
        """Checked once, at warm-up: every prompt afterwards is shaped for this template."""
        self._fold_system = not self._system_role_supported()
        if self._fold_system:
            log.info("%s: the chat template has no system role; the phone instructions go at the top of the first "
                     "user turn", self.model_id)

    def warmup(self, system_prompt: str = "", wire_memory: bool = True) -> dict:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache

        from ai_phone.services.backends.mlx_memory import begin_wiring, wire_resident

        t = time.monotonic_ns()
        begin_wiring(wire_memory)  # before the load: MLX wires buffers as they are allocated
        self._model, self._tok = load(self.model_id)
        self._note_system_role()  # before the first _ids: it decides how every prompt is shaped
        wired = wire_resident(self._model, wire_memory)  # the weights stay resident through idle spells
        fixed = self._system = system_prompt or "You are a phone assistant."  # the part every call shares
        pa, pb = cache_probe_sections()
        a = self._ids([{"role": "system", "content": compose_prompt(fixed, pa)}, {"role": "user", "content": "Hello?"}])
        b = self._ids([{"role": "system", "content": compose_prompt(fixed, pb)}, {"role": "user", "content": "Hello?"}])
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        self._prefix_ids = a[:n]  # the fixed instructions and the call-section header: every call's prompt starts so
        cache = make_prompt_cache(self._model)
        if self._prefix_ids:
            mx.eval(self._model(mx.array(self._prefix_ids)[None], cache=cache))
        self._snap = cache
        list(self.stream(0, [{"role": "system", "content": compose_prompt(fixed, None)}, {"role": "user", "content": "Hello?"}], 8))
        # system_role_folded travels in the dict because the log line above runs in the worker child, where
        # logging is not configured; the daemon's ready line prints this dict
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6, "prefix_tokens": float(len(self._prefix_ids)),
                "system_role_folded": self._fold_system, **wired}

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
        list(self.stream(-1, [{"role": "system", "content": compose_prompt(self._system, None)}, {"role": "user", "content": "Hi."}], 1))
        self.last_timings = saved


def fold_system(messages: list[Message]) -> list[Message]:
    """The system text moved to the top of the first user turn, for a template with no system role."""
    system = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    if not system:
        return rest
    for i, m in enumerate(rest):
        if m["role"] == "user":
            return [*rest[:i], {"role": "user", "content": f"{system}\n\n{m['content']}"}, *rest[i + 1:]]
    return [{"role": "user", "content": system}, *rest]


def make_llm(model_id: str) -> MlxLmLLM:
    return MlxLmLLM(model_id)


def make_t9() -> MlxLmLLM:
    return make_llm(T9)


def make_t4() -> MlxLmLLM:
    return make_llm(T4)
