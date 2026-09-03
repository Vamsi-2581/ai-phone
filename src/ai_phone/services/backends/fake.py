"""Deterministic fake backends for tests: no models, no network."""
from __future__ import annotations

import math
import time

import numpy as np


class FakeSTT:
    def __init__(self):
        self._n = 0
        self._buf: dict[int, list] = {}
        self._cancelled: set[int] = set()

    def warmup(self):
        return {"warm_ms": 1.0}

    def push(self, gen_id, pcm):
        self._buf.setdefault(gen_id, []).append(pcm)
        return "fake"

    def finalize(self, gen_id):
        self._n += 1
        self._buf.pop(gen_id, None)
        return f"fake utterance {self._n}", {"stt_ms": 1.0}

    def cancel(self, gen_id):
        self._cancelled.add(gen_id)
        self._buf.pop(gen_id, None)


class FakeLLM:
    REPLY = "Hello, this is a fake reply."
    TOKENS = ["Hello", ",", " this", " is", " a", " fake", " reply", "."]

    def __init__(self):
        self._cancelled: set[int] = set()

    def warmup(self, system_prompt: str = ""):
        return {"warm_ms": 1.0}

    def stream(self, gen_id, messages, max_tokens):
        for tok in self.TOKENS:
            if gen_id in self._cancelled:
                return
            time.sleep(0.01)
            yield tok

    def cancel(self, gen_id):
        self._cancelled.add(gen_id)


class FakeTTS:
    def __init__(self):
        self._cancelled: set[int] = set()

    def warmup(self):
        return {"warm_ms": 1.0}

    def stream(self, gen_id, seq, text):
        for i in range(25):
            if gen_id in self._cancelled:
                return
            t = (np.arange(320) + i * 320) / 16000.0
            yield (np.sin(2 * math.pi * 440 * t) * 8000).astype("<i2").tobytes()

    def cancel(self, gen_id):
        self._cancelled.add(gen_id)


class CountingSTT(FakeSTT):
    """Reports how many samples it received, as text: "<n> samples"."""

    def __init__(self):
        super().__init__()
        self._count: dict[int, int] = {}

    def push(self, gen_id, pcm):
        self._count[gen_id] = self._count.get(gen_id, 0) + len(pcm)
        return None

    def finalize(self, gen_id):
        n = self._count.pop(gen_id, 0)
        return f"{n} samples", {"stt_ms": 1.0}
