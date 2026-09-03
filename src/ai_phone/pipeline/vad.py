"""Voice activity detection backends and the endpointer that turns frames into utterances."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

CHUNK = 512  # Silero chunk at 16 kHz (32 ms)
FRAME = 320  # 20 ms frame


class VadBackend(Protocol):
    def prob(self, chunk_f32: np.ndarray) -> float: ...


class EnergyVad:
    """Test double: probability 1.0 if RMS above threshold else 0.0."""

    def __init__(self, threshold: float = 0.02):
        self.threshold = threshold

    def prob(self, chunk_f32: np.ndarray) -> float:
        return 1.0 if float(np.sqrt(np.mean(chunk_f32 * chunk_f32))) > self.threshold else 0.0


class SileroVad:
    def __init__(self):
        import torch
        from silero_vad import load_silero_vad

        torch.set_num_threads(1)
        self._torch, self._model = torch, load_silero_vad()

    def prob(self, chunk_f32: np.ndarray) -> float:
        return float(self._model(self._torch.from_numpy(np.ascontiguousarray(chunk_f32)), 16000).item())


@dataclass(frozen=True)
class SpeechStart:
    t_ns: int


@dataclass(frozen=True)
class SpeechChunk:
    pcm_f32: np.ndarray
    t_ns: int


@dataclass(frozen=True)
class SpeechEnd:
    t0_ns: int
    utterance_f32: np.ndarray


EndpointEvent = SpeechStart | SpeechChunk | SpeechEnd


class Endpointer:
    """Feeds 20 ms frames, emits SpeechStart / SpeechChunk / SpeechEnd. t0 is the last VAD-positive chunk."""

    def __init__(self, vad: VadBackend, silence_ms: int = 300, start_chunks: int = 2,
                 preroll_ms: int = 200, threshold: float = 0.5):
        self.vad, self.threshold = vad, threshold
        self.silence_chunks = max(1, round(silence_ms / 32))
        self.start_chunks = start_chunks
        self.preroll_chunks = max(1, round(preroll_ms / 32))
        self._carry = np.zeros(0, dtype=np.float32)
        self._preroll: list[np.ndarray] = []
        self._utt: list[np.ndarray] = []
        self._pos = 0
        self._neg = 0
        self.in_speech = False
        self._last_pos_t = 0
        self.noise_rms = 0.0

    def _track_noise(self, rms: float) -> None:
        """Background level from non-speech chunks. It falls fast and rises slowly, so a speech onset the VAD
        has not yet flagged (or a single loud chunk) cannot lift it; digital silence keeps it at the floor."""
        if rms < self.noise_rms:
            self.noise_rms = 0.5 * self.noise_rms + 0.5 * rms
        else:
            self.noise_rms = min(0.98 * self.noise_rms + 0.02 * rms, self.noise_rms + 0.002)

    def feed(self, frame640: bytes, t_ns: int) -> list[EndpointEvent]:
        pcm = np.frombuffer(frame640, dtype="<i2").astype(np.float32) / 32768.0
        self._carry = np.concatenate([self._carry, pcm])
        out: list[EndpointEvent] = []
        while len(self._carry) >= CHUNK:
            chunk, self._carry = self._carry[:CHUNK], self._carry[CHUNK:]
            out += self._step(chunk, t_ns)
        return out

    def _step(self, chunk: np.ndarray, t_ns: int) -> list[EndpointEvent]:
        out: list[EndpointEvent] = []
        speech = self.vad.prob(chunk) >= self.threshold
        if not self.in_speech:
            self._preroll.append(chunk)
            self._preroll = self._preroll[-self.preroll_chunks:]
            if speech:
                self._pos += 1
                if self._pos >= self.start_chunks:
                    self.in_speech = True
                    self._neg = 0
                    self._utt = list(self._preroll)
                    self._last_pos_t = t_ns
                    out.append(SpeechStart(t_ns))
                    out.append(SpeechChunk(chunk, t_ns))
            else:
                self._pos = 0
                self._track_noise(float(np.sqrt(np.mean(chunk * chunk))))
            return out
        self._utt.append(chunk)
        out.append(SpeechChunk(chunk, t_ns))
        if speech:
            self._neg = 0
            self._last_pos_t = t_ns
        else:
            self._neg += 1
            if self._neg >= self.silence_chunks:
                tail = self.silence_chunks
                utter = np.concatenate(self._utt[:-tail]) if len(self._utt) > tail else np.concatenate(self._utt)
                out.append(SpeechEnd(self._last_pos_t, utter))
                self.in_speech = False
                self._pos = 0
                self._neg = 0
                self._utt = []
                self._preroll = []
        return out
