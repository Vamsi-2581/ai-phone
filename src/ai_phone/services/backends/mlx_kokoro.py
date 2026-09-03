"""Kokoro-82M via mlx-audio, emitting 16 kHz 640-byte frames (resampled from Kokoro's native 24 kHz)."""
from __future__ import annotations

import time

import numpy as np

from ai_phone.pipeline.audio_out import Framer


class KokoroTTS:
    def __init__(self, model_id: str = "mlx-community/Kokoro-82M-bf16", voice: str = "af_heart"):
        self.model_id, self.voice = model_id, voice
        self._model = None
        self._mx = None
        self._cancelled: set[int] = set()
        self.last_timings: dict = {}

    def warmup(self) -> dict:
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        self._mx = mx
        t = time.monotonic_ns()
        self._model = load_model(self.model_id)
        list(self.stream(0, 0, "Warm up."))
        return {"warm_ms": (time.monotonic_ns() - t) / 1e6}

    def _to_np(self, x) -> np.ndarray:
        return np.array(self._mx.array(x).astype(self._mx.float32)).reshape(-1)  # bf16-safe conversion

    LEAD_IN_THRESHOLD = 200  # int16 units; Kokoro starts every utterance with ~200 ms of digital silence

    def stream(self, gen_id: int, seq: int, text: str):
        framer = Framer(24000)
        t0 = time.monotonic_ns()
        first = None
        speech_started = False
        for res in self._model.generate(text=text, voice=self.voice, speed=1.0, lang_code="a"):
            if gen_id in self._cancelled:
                break
            # Kokoro returns a whole sentence per result, so cancellation is checked per frame, not per result
            for frame in framer.feed(self._to_np(res.audio)):
                if gen_id in self._cancelled:
                    break
                if not speech_started:
                    # drop the leading silence: it is pure latency in the caller's ear
                    if np.abs(np.frombuffer(frame, dtype="<i2")).max() < self.LEAD_IN_THRESHOLD:
                        continue
                    speech_started = True
                if first is None:
                    first = time.monotonic_ns()
                yield frame
        if gen_id not in self._cancelled:
            for frame in framer.flush():
                yield frame
        self.last_timings = {"first_chunk_ms": ((first or time.monotonic_ns()) - t0) / 1e6}
        self._cancelled.discard(gen_id)

    def cancel(self, gen_id: int) -> None:
        self._cancelled.add(gen_id)
