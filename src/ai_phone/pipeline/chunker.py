"""Splits a streamed LLM reply into TTS units at clause boundaries so speech starts early."""
from __future__ import annotations

import re

from ai_phone.pipeline.prompts import END_MARKER

_SENT = set(".!?")
_CLAUSE = set(",;:")
_MARKER = re.compile(re.escape(END_MARKER), re.IGNORECASE)  # the model may write [end] or [End]


def _partial_marker_len(text: str) -> int:
    """Length of the start of the marker that ends `text` ("[", "[E", "[EN", "[END", any case), or 0."""
    low, marker = text.lower(), END_MARKER.lower()
    return next((i for i in range(len(marker) - 1, 0, -1) if low.endswith(marker[:i])), 0)


class ClauseChunker:
    def __init__(self, min_tokens_for_clause: int = 6, max_tokens: int = 12,
                 first_min_tokens_for_clause: int = 4, first_max_tokens: int = 8):
        self.min_clause, self.max_tokens = min_tokens_for_clause, max_tokens
        self.first_min_clause, self.first_max_tokens = first_min_tokens_for_clause, first_max_tokens
        self._buf: list[str] = []
        self._emitted = 0
        self.ended = False  # the model asked to end the call (it wrote END_MARKER); the marker itself is never spoken

    def feed(self, token: str) -> list[str]:
        self._buf.append(token)
        if _partial_marker_len("".join(self._buf)):
            return []  # wait for the rest of a possible marker before cutting a unit
        n = len(self._buf)
        last = token.rstrip()[-1:] if token.strip() else ""
        # the first unit is kept short so the caller hears something sooner; later units are sentence-sized
        min_clause = self.first_min_clause if self._emitted == 0 else self.min_clause
        max_tokens = self.first_max_tokens if self._emitted == 0 else self.max_tokens
        if last in _SENT or (last in _CLAUSE and n >= min_clause) or n >= max_tokens:
            return self.flush()
        return []

    def flush(self) -> list[str]:
        text = "".join(self._buf)
        self._buf = []
        text, found = _MARKER.subn("", text)
        if found:
            self.ended = True
        # a reply cut off inside a marker is not a request to end, and the fragment is never spoken
        text = text[:len(text) - _partial_marker_len(text)].strip()
        if text:
            self._emitted += 1
            return [text]
        return []
