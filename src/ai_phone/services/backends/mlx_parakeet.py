"""parakeet-tdt-0.6b-v2 via mlx-audio. Buffered decode: chunks accumulate; finalize decodes the whole utterance.
Streaming decode is a Milestone 2 optimisation if finalize exceeds its 100 ms budget (spike: ~27 ms p50)."""
from __future__ import annotations

import time

import numpy as np


class ParakeetSTT:
    def __init__(self, model_id: str = "mlx-community/parakeet-tdt-0.6b-v2"):
        self.model_id = model_id
        self._model = None
        self._mx = None
        self._buf: dict[int, list[np.ndarray]] = {}
        self._cancelled: set[int] = set()

    def warmup(self) -> dict:
        import mlx.core as mx
        from mlx_audio.stt.utils import load_model

        self._mx = mx
        t = time.monotonic_ns()
        self._model = load_model(self.model_id)
        self._transcribe(np.zeros(16000, dtype=np.float32))
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6}

    def _transcribe(self, pcm: np.ndarray) -> str:
        out = self._model.generate(self._mx.array(np.asarray(pcm, np.float32)))  # expects an mx.array (numpy input fails)
        return (getattr(out, "text", None) or str(out)).strip()

    def push(self, gen_id: int, pcm16k_f32: np.ndarray) -> str | None:
        if gen_id in self._cancelled:
            return None
        self._buf.setdefault(gen_id, []).append(np.asarray(pcm16k_f32, dtype=np.float32))
        return None  # partials: Milestone 2 (needs the streaming API)

    def finalize(self, gen_id: int) -> tuple[str, dict]:
        chunks = self._buf.pop(gen_id, [])
        if gen_id in self._cancelled or not chunks:
            self._cancelled.discard(gen_id)
            return "", {"stt_ms": 0.0}
        t = time.monotonic_ns()
        text = self._transcribe(np.concatenate(chunks))
        return text, {"stt_ms": (time.monotonic_ns() - t) / 1e6}

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)
        self._buf.pop(gen_id, None)
