"""float32 audio at any rate → 16 kHz int16 640-byte frames, carrying partial frames across calls."""
from __future__ import annotations

import numpy as np
import soxr

from ai_phone.telephony.rings import FRAME_BYTES


class Framer:
    def __init__(self, src_rate: int):
        self.src_rate = src_rate
        self._rs = soxr.ResampleStream(src_rate, 16000, 1, dtype="float32", quality="VHQ") if src_rate != 16000 else None
        self._carry = b""

    @staticmethod
    def _to_bytes(f32: np.ndarray) -> bytes:
        return (np.clip(f32, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    def feed(self, f32: np.ndarray) -> list[bytes]:
        x = np.asarray(f32, np.float32).reshape(-1)
        if self._rs is not None:
            x = self._rs.resample_chunk(x)
        self._carry += self._to_bytes(x)
        out = []
        n = len(self._carry) // FRAME_BYTES
        for i in range(n):
            out.append(self._carry[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])
        self._carry = self._carry[n * FRAME_BYTES:]
        return out

    def flush(self) -> list[bytes]:
        tail = self._rs.resample_chunk(np.zeros(0, np.float32), last=True) if self._rs is not None else np.zeros(0, np.float32)
        out = self.feed(tail) if len(tail) else []
        if self._carry:
            out.append(self._carry + bytes(FRAME_BYTES - len(self._carry)))
            self._carry = b""
        return out
