"""Per-turn latency record (architecture.md §9). t0 = last VAD-positive chunk; t5 = first non-silent RTP frame."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def _ms(a, b):
    return None if a is None or b is None else (b - a) / 1e6


@dataclass
class TurnTimer:
    gen: int
    t0: int | None = None  # last VAD-positive chunk
    t0_end: int | None = None  # endpointer fired (t0 + silence window); STT finalize sent here
    t1: int | None = None  # STT final received
    t2: int | None = None  # first LLM token
    t3: int | None = None  # first clause sent to TTS
    t4: int | None = None  # first TTS frames available
    t5: int | None = None  # first non-silent frame handed to PJSIP
    bargein: bool = False
    bargein_stop_ns: int | None = None
    stt_text: str = ""
    reply_text: str = ""
    rx_overruns: int = 0
    tx_underruns: int = 0
    worker_timings: dict = field(default_factory=dict)

    def first_audible_ms(self) -> float | None:
        return None if self.t0 is None or self.t5 is None else (self.t5 - self.t0) / 1e6

    def to_record(self, call_id: str, turn: int) -> dict:
        wt = self.worker_timings
        return {
            "call_id": call_id, "turn": turn, "gen": self.gen,
            "t0_speech_end_ns": self.t0, "t1_stt_final_ns": self.t1, "t2_llm_first_token_ns": self.t2,
            "t3_first_clause_ns": self.t3, "t4_tts_first_chunk_ns": self.t4, "t5_rtp_first_nonsilent_ns": self.t5,
            "endpoint_ms": _ms(self.t0, self.t0_end), "stt_ms": _ms(self.t0_end or self.t0, self.t1),
            "llm_prompt_eval_ms": wt.get("llm_prompt_eval_ms"),
            "llm_first_clause_ms": _ms(self.t1, self.t3), "tts_first_chunk_ms": _ms(self.t3, self.t4),
            "egress_ms": _ms(self.t4, self.t5), "first_audible_ms": self.first_audible_ms(),
            "bargein": self.bargein,
            "bargein_stop_ms": None if self.bargein_stop_ns is None else self.bargein_stop_ns / 1e6,
            "rx_overruns": self.rx_overruns, "tx_underruns": self.tx_underruns, "tokens": wt.get("llm_tokens"),
            "stt_text": self.stt_text, "reply_text": self.reply_text,
        }


class TurnLog:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
