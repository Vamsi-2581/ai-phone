"""float32 audio at any rate → 16 kHz int16 640-byte frames, carrying partial frames across calls."""
from __future__ import annotations

import numpy as np
import soxr

FRAME_BYTES = 640  # 320 int16 samples at 16 kHz = 20 ms; must equal telephony.rings.FRAME_BYTES


class Framer:
    def __init__(self, src_rate: int):
        self.src_rate = src_rate
        self._rs = soxr.ResampleStream(src_rate, 16000, 1, dtype="float32", quality="VHQ") if src_rate != 16000 else None
        self._carry = b""

    @staticmethod
    def _to_bytes(f32: np.ndarray) -> bytes:
        return (np.clip(np.asarray(f32, np.float32).reshape(-1), -1.0, 1.0) * 32767).astype("<i2").tobytes()

    def _frames_from_carry(self) -> list[bytes]:
        n = len(self._carry) // FRAME_BYTES
        out = [self._carry[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(n)]
        self._carry = self._carry[n * FRAME_BYTES:]
        return out

    def feed(self, f32: np.ndarray) -> list[bytes]:
        x = np.asarray(f32, np.float32).reshape(-1)
        if self._rs is not None:
            x = self._rs.resample_chunk(x)
        self._carry += self._to_bytes(x)
        return self._frames_from_carry()

    def flush(self) -> list[bytes]:
        """Drain the resampler tail (without re-feeding it) and pad the final partial frame with silence."""
        if self._rs is not None:
            self._carry += self._to_bytes(self._rs.resample_chunk(np.zeros(0, np.float32), last=True))
        out = self._frames_from_carry()
        if self._carry:
            out.append(self._carry + bytes(FRAME_BYTES - len(self._carry)))
            self._carry = b""
        return out
