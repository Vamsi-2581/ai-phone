"""Single-producer / single-consumer ring of fixed-size audio frames on shared memory.

Layout: [head u64][tail u64][overruns u64][underruns u64][frames × frame_bytes].
head/tail are ever-increasing counters; slot = counter % frames. Aligned 8-byte
stores are atomic on arm64 and x86-64, which is all SPSC needs.
"""
from __future__ import annotations

import struct
from multiprocessing import shared_memory

FRAME_BYTES = 640  # 320 samples × int16 at 16 kHz = 20 ms
SILENCE = bytes(FRAME_BYTES)
_HDR = struct.Struct("<QQQQ")  # head, tail, overruns, underruns


class FrameRing:
    def __init__(self, name: str | None, frames: int, frame_bytes: int = FRAME_BYTES, create: bool = True):
        if frames < 1 or frame_bytes < 1:
            raise ValueError("frames and frame_bytes must be >= 1")
        self.frames, self.frame_bytes = frames, frame_bytes
        size = _HDR.size + frames * frame_bytes
        self._shm = shared_memory.SharedMemory(name=name, create=create, size=size)
        self._buf = self._shm.buf
        if create:
            _HDR.pack_into(self._buf, 0, 0, 0, 0, 0)

    @classmethod
    def attach(cls, name: str, frames: int | None = None, frame_bytes: int = FRAME_BYTES) -> "FrameRing":
        shm = shared_memory.SharedMemory(name=name, create=False)
        n = frames or (len(shm.buf) - _HDR.size) // frame_bytes
        shm.close()
        return cls(name, n, frame_bytes, create=False)

    @property
    def name(self) -> str:
        return self._shm.name

    def _hdr(self):
        return _HDR.unpack_from(self._buf, 0)

    def _set(self, head, tail, over, under):
        _HDR.pack_into(self._buf, 0, head, tail, over, under)

    @property
    def overruns(self) -> int:
        return self._hdr()[2]

    @property
    def underruns(self) -> int:
        return self._hdr()[3]

    def available(self) -> int:
        head, tail, _, _ = self._hdr()
        return head - tail

    def push(self, frame: bytes) -> None:
        if len(frame) != self.frame_bytes:
            raise ValueError(f"frame must be {self.frame_bytes} bytes, got {len(frame)}")
        head, tail, over, under = self._hdr()
        if head - tail >= self.frames:  # full: drop oldest
            tail += 1
            over += 1
        off = _HDR.size + (head % self.frames) * self.frame_bytes
        self._buf[off:off + self.frame_bytes] = frame
        self._set(head + 1, tail, over, under)

    def pop(self) -> bytes | None:
        head, tail, over, under = self._hdr()
        if head == tail:
            return None
        off = _HDR.size + (tail % self.frames) * self.frame_bytes
        frame = bytes(self._buf[off:off + self.frame_bytes])
        self._set(head, tail + 1, over, under)
        return frame

    def pop_or_silence(self) -> bytes:
        f = self.pop()
        if f is None:
            head, tail, over, under = self._hdr()
            self._set(head, tail, over, under + 1)
            return SILENCE if self.frame_bytes == FRAME_BYTES else bytes(self.frame_bytes)
        return f

    def flush(self) -> None:
        head, _, over, under = self._hdr()
        self._set(head, head, over, under)

    def close(self) -> None:
        self._buf = None
        self._shm.close()

    def unlink(self) -> None:
        try:
            self._shm.unlink()
        except FileNotFoundError:
            pass
