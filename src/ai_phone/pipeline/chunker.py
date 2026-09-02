"""Splits a streamed LLM reply into TTS units at clause boundaries so speech starts early."""
from __future__ import annotations

_SENT = set(".!?")
_CLAUSE = set(",;:")


class ClauseChunker:
    def __init__(self, min_tokens_for_clause: int = 6, max_tokens: int = 12):
        self.min_clause, self.max_tokens = min_tokens_for_clause, max_tokens
        self._buf: list[str] = []

    def feed(self, token: str) -> list[str]:
        self._buf.append(token)
        n = len(self._buf)
        last = token.rstrip()[-1:] if token.strip() else ""
        if last in _SENT or (last in _CLAUSE and n >= self.min_clause) or n >= self.max_tokens:
            return self.flush()
        return []

    def flush(self) -> list[str]:
        text = "".join(self._buf).strip()
        self._buf = []
        return [text] if text else []
