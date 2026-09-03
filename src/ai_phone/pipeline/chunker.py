"""Splits a streamed LLM reply into TTS units at clause boundaries so speech starts early."""
from __future__ import annotations

_SENT = set(".!?")
_CLAUSE = set(",;:")


class ClauseChunker:
    def __init__(self, min_tokens_for_clause: int = 6, max_tokens: int = 12,
                 first_min_tokens_for_clause: int = 4, first_max_tokens: int = 8):
        self.min_clause, self.max_tokens = min_tokens_for_clause, max_tokens
        self.first_min_clause, self.first_max_tokens = first_min_tokens_for_clause, first_max_tokens
        self._buf: list[str] = []
        self._emitted = 0

    def feed(self, token: str) -> list[str]:
        self._buf.append(token)
        n = len(self._buf)
        last = token.rstrip()[-1:] if token.strip() else ""
        # the first unit is kept short so the caller hears something sooner; later units are sentence-sized
        min_clause = self.first_min_clause if self._emitted == 0 else self.min_clause
        max_tokens = self.first_max_tokens if self._emitted == 0 else self.max_tokens
        if last in _SENT or (last in _CLAUSE and n >= min_clause) or n >= max_tokens:
            return self.flush()
        return []

    def flush(self) -> list[str]:
        text = "".join(self._buf).strip()
        self._buf = []
        if text:
            self._emitted += 1
            return [text]
        return []
