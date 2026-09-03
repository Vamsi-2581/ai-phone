"""Barge-in detection: sustained caller speech, loud enough to beat speakerphone leak while we are playing."""
from __future__ import annotations


class BargeInDetector:
    def __init__(self, min_ms: int = 250, gain: float = 3.0, chunk_ms: int = 32, gap_tolerance: int = 1):
        self.need = max(1, round(min_ms / chunk_ms))
        self.gain = gain
        self.gap_tolerance = gap_tolerance  # a single quiet chunk inside a word must not restart the count
        self._run = 0
        self._gap = 0
        self._fired = False

    def reset(self) -> None:
        self._run = 0
        self._gap = 0
        self._fired = False

    def update(self, speech: bool, rms: float, noise_rms: float, playing: bool) -> bool:
        """Returns True exactly once per interruption, on the chunk that crosses min_ms."""
        loud = (rms > max(noise_rms, 1e-3) * self.gain) if playing else True
        if speech and loud:
            self._run += 1
            self._gap = 0
            if self._run >= self.need and not self._fired:
                self._fired = True
                return True
        else:
            self._gap += 1
            if self._gap > self.gap_tolerance or self._run == 0:
                self._run = 0
                self._gap = 0
                self._fired = False
        return False
