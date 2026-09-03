"""TurnController: owns one call's conversation. GREETING → LISTENING → THINKING → SPEAKING → LISTENING.

Runs a 5 ms asyncio loop that drains caller audio into the endpointer, pumps TTS frames to the
outbound ring, and applies worker events. Every generation carries a gen_id shared by all three
workers; anything tagged with a stale gen_id is dropped on receipt by the WorkerClient.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Protocol

import numpy as np

from ai_phone.config import ConversationConfig
from ai_phone.pipeline.bargein import BargeInDetector
from ai_phone.pipeline.chunker import ClauseChunker
from ai_phone.pipeline.timing import TurnTimer
from ai_phone.pipeline.vad import Endpointer, SpeechChunk, SpeechEnd, SpeechStart
from ai_phone.services.client import WorkerClient
from ai_phone.services.ipc import tag_frame, untag_frame
from ai_phone.telephony.rings import FrameRing


class PortLike(Protocol):
    first_nonsilent_ns: int | None

    def arm_t5(self) -> None: ...


class TurnController:
    def __init__(self, cfg: ConversationConfig, port: PortLike, stt: WorkerClient, llm: WorkerClient, tts: WorkerClient,
                 rx: FrameRing, tx: FrameRing, stt_in: FrameRing, tts_out: FrameRing, endpointer: Endpointer,
                 greeting_frames: list[bytes], system_prompt: str, on_turn: Callable[[TurnTimer], None],
                 prompt_frames: list[bytes] = (), warning_frames: list[bytes] = (),
                 rtp_last_rx_ns: Callable[[], int] | None = None):
        self.cfg, self.port, self.stt, self.llm, self.tts = cfg, port, stt, llm, tts
        self.rx, self.tx, self.stt_in, self.tts_out, self.ep = rx, tx, stt_in, tts_out, endpointer
        self.greeting, self.system_prompt, self.on_turn = greeting_frames, system_prompt, on_turn
        self.state = "GREETING"
        self.history: list[dict] = []
        self.timer: TurnTimer | None = None
        self.chunker = ClauseChunker()
        self._pending_stt = 0
        self._seq = 0
        self._tts_done_seq = -1
        self._llm_done = False
        self._reply_parts: list[str] = []
        self.end_reason: str | None = None
        self.last_activity_ns = time.monotonic_ns()
        self.hangup_requested = asyncio.Event()
        self.bargein = BargeInDetector(cfg.bargein_min_speech_ms, cfg.bargein_playback_gain)
        self._interrupt_t: int | None = None
        self.prompt_frames, self.warning_frames, self.rtp_last_rx_ns = list(prompt_frames), list(warning_frames), rtp_last_rx_ns
        self._prompted = False
        self._warned = False
        self._start_ns = 0
        self._candidate: list[np.ndarray] = []  # speech chunks seen while not listening; replayed on barge-in

    # ---- generation bookkeeping ---------------------------------------------------------------
    def _new_gen(self) -> int:
        """One shared generation across all three workers (their counters may differ: cached phrases use TTS alone)."""
        g = max(self.stt.current_gen, self.llm.current_gen, self.tts.current_gen) + 1
        for c in (self.stt, self.llm, self.tts):
            c.set_gen(g)
        self._seq, self._tts_done_seq, self._llm_done, self._reply_parts = 0, -1, False, []
        self._pending_stt = 0
        return g

    def request_end(self, reason: str) -> None:
        self.end_reason = reason
        self.state = "ENDING"
        self.hangup_requested.set()

    # ---- main loop ----------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        for f in self.greeting:
            self.tx.push(f)
        self._new_gen()
        self._start_ns = time.monotonic_ns()
        self._rtp_baseline = self._start_ns
        while not stop.is_set() and self.state != "ENDING":
            now = time.monotonic_ns()
            while (f := self.rx.pop()) is not None:
                self._on_frame(f, now)
            self._pump_tts_out()
            self._drain_worker_events()
            if self.state == "GREETING" and self.tx.available() == 0:
                self.state = "LISTENING"
                self.last_activity_ns = now
            playing = getattr(self.port, "playing", None)
            if playing is not None:
                self.port.playing = self.state in ("GREETING", "SPEAKING")
            if self.state == "SPEAKING" and self._llm_done and self._tts_done_seq >= self._seq - 1 and self.tx.available() == 0:
                self._finish_turn()
            self._check_timers(now)
            await asyncio.sleep(0.005)

    def _check_timers(self, now: int) -> None:
        """All call timers live here, none in PJSIP. Warning fires 30 s before the cap (0.5 s for caps <= 30 s)."""
        idle_s = (now - self.last_activity_ns) / 1e9
        if self.state == "LISTENING" and not self._prompted and idle_s >= self.cfg.silence_prompt_sec:
            for f in self.prompt_frames:
                self.tx.push(f)
            self._prompted = True
            self.last_activity_ns = now
        elif self.state == "LISTENING" and self._prompted and idle_s >= self.cfg.silence_hangup_sec:
            self.request_end("silence")
        elapsed_s = (now - self._start_ns) / 1e9
        warn_at = self.cfg.max_call_sec - (30 if self.cfg.max_call_sec > 30 else 0.5)
        if not self._warned and elapsed_s >= warn_at:
            for f in self.warning_frames:
                self.tx.push(f)
            self._warned = True
        if elapsed_s >= self.cfg.max_call_sec:
            self.request_end("max_duration")
        if self.rtp_last_rx_ns is not None:
            last_rx = max(self.rtp_last_rx_ns(), self._rtp_baseline)  # a stale or zero stamp must not fire the timeout
            if (now - last_rx) / 1e9 >= self.cfg.rtp_timeout_sec:
                self.request_end("rtp_timeout")

    # ---- inbound audio ------------------------------------------------------------------------
    def _on_frame(self, frame: bytes, t_ns: int) -> None:
        for e in self.ep.feed(frame, t_ns):
            if isinstance(e, SpeechChunk):
                rms = float(np.sqrt(np.mean(e.pcm_f32 * e.pcm_f32)))
                playing = self.tx.available() > 0 or self.state in ("GREETING", "SPEAKING")
                if self.state in ("GREETING", "THINKING", "SPEAKING"):
                    # not listening: remember the audio so the interrupting utterance keeps its onset
                    if self._interrupt_t is None:
                        self._interrupt_t = e.t_ns
                    self._candidate.append(e.pcm_f32)
                    if self.bargein.update(True, rms, self.ep.noise_rms, playing):
                        self._interrupt(t_ns)  # replays the candidate audio, including this chunk
                    continue
                self.bargein.update(True, rms, self.ep.noise_rms, playing)
            else:
                self.bargein.update(False, 0.0, self.ep.noise_rms, False)
                self._interrupt_t = None
                self._candidate = []
            if isinstance(e, SpeechStart):
                self.last_activity_ns = t_ns
                self._prompted = False  # the caller is back
                if self.state == "LISTENING":
                    self.timer = TurnTimer(gen=self.stt.current_gen)
            elif isinstance(e, SpeechChunk) and self.state == "LISTENING":
                self._push_stt(e.pcm_f32)
            elif isinstance(e, SpeechEnd) and self.state == "LISTENING":
                self._flush_stt_push()
                if self.timer is None:
                    self.timer = TurnTimer(gen=self.stt.current_gen)
                self.timer.t0 = e.t0_ns
                self.timer.t0_end = t_ns
                self.stt.send({"op": "stt.finalize", "gen": self.stt.current_gen})
                self.state = "THINKING"

    def _push_stt(self, pcm_f32: np.ndarray) -> None:
        self.stt_in.push(tag_frame(self.stt.current_gen, (np.clip(pcm_f32, -1, 1) * 32767).astype("<i2").tobytes()))
        self._pending_stt += 1
        if self._pending_stt >= 10:
            self._flush_stt_push()

    def _interrupt(self, now: int) -> None:
        """Caller talked over us (greeting or reply): cancel this generation, flush audio, keep the partial reply,
        and replay the interrupting speech into the new generation so its first 250 ms are not lost."""
        was_greeting = self.state == "GREETING"
        t = self.timer
        if t is not None and not was_greeting:
            for c in (self.stt, self.llm, self.tts):
                c.cancel(t.gen)
        self._flush_tx()
        self.tts_out.flush()  # we are its consumer
        if t is not None and not was_greeting:
            t.bargein = True
            t.bargein_stop_ns = now - (self._interrupt_t or now)
            t.reply_text = "".join(self._reply_parts).strip()
            self.history.append({"role": "assistant", "content": (t.reply_text + " [interrupted]").strip()})
            t.rx_overruns, t.tx_underruns = self.rx.overruns, self.tx.underruns
            self.on_turn(t)
        candidate, self._candidate = self._candidate, []
        self._new_gen()
        self.bargein.reset()
        self._interrupt_t = None
        self.timer = TurnTimer(gen=self.stt.current_gen)
        self.state = "LISTENING"
        for pcm in candidate:
            self._push_stt(pcm)

    def _flush_tx(self) -> None:
        """The PJSIP thread is the tx ring's consumer, so it performs the flush; a stub port flushes directly."""
        req = getattr(self.port, "request_flush", None)
        if req is not None:
            req()
        else:
            self.tx.flush()

    def _flush_stt_push(self) -> None:
        if self._pending_stt:
            self.stt.send({"op": "stt.push", "gen": self.stt.current_gen, "nframes": self._pending_stt})
            self._pending_stt = 0

    # ---- worker events ------------------------------------------------------------------------
    def _drain_worker_events(self) -> None:
        for client in (self.stt, self.llm, self.tts):
            while not client.events.empty():
                self._on_event(client.events.get_nowait())

    def _on_event(self, m: dict) -> None:
        ev, now = m["ev"], time.monotonic_ns()
        if ev == "error":
            self.request_end(f"worker error: {m['message'][:200]}")
            return
        if self.timer is None or m.get("gen") != self.timer.gen:
            return
        if ev == "final":
            self.timer.t1 = now
            self.timer.stt_text = m["text"]
            self.timer.worker_timings.update(m.get("timings", {}))
            if not m["text"].strip():
                self.state = "LISTENING"
                self.timer = None
                return
            self.history.append({"role": "user", "content": m["text"]})
            msgs = [{"role": "system", "content": self.system_prompt}] + self.history[-2 * self.cfg.history_turns:]
            self.chunker = ClauseChunker()
            self.port.arm_t5()
            self.llm.send({"op": "llm.generate", "gen": self.timer.gen, "messages": msgs, "max_tokens": self.cfg.max_tokens})
        elif ev == "token":
            if self.timer.t2 is None:
                self.timer.t2 = now
            self._reply_parts.append(m["text"])
            for unit in self.chunker.feed(m["text"]):
                self._send_tts(unit, now)
        elif ev == "done" and m["kind"] == "llm":
            self.timer.worker_timings.update({f"llm_{k}": v for k, v in m.get("timings", {}).items()})
            for unit in self.chunker.flush():
                self._send_tts(unit, now)
            self._llm_done = True
            if self._seq == 0:
                self._finish_turn()  # the model said nothing
        elif ev == "chunk":
            if self.timer.t4 is None:
                self.timer.t4 = now
            self.state = "SPEAKING"
        elif ev == "done" and m["kind"] == "tts":
            self._tts_done_seq = max(self._tts_done_seq, m["seq"])

    def _send_tts(self, unit: str, now: int) -> None:
        if self.timer.t3 is None:
            self.timer.t3 = now
        self.tts.send({"op": "tts.synth", "gen": self.timer.gen, "seq": self._seq, "text": unit})
        self._seq += 1

    def _pump_tts_out(self) -> None:
        cur = self.tts.current_gen & 0xFFFFFFFF
        while (f := self.tts_out.pop()) is not None:
            g, payload = untag_frame(f)
            if g == cur:  # audio from a cancelled generation is dropped here, whatever its timing
                self.tx.push(payload)
        # accept the port's stamp only once this turn has sent TTS (t3), so a previous turn's stamp is never reused
        if (self.timer is not None and self.timer.t5 is None and self.timer.t3 is not None
                and self.port.first_nonsilent_ns and self.port.first_nonsilent_ns >= self.timer.t3):
            self.timer.t5 = self.port.first_nonsilent_ns

    def _finish_turn(self) -> None:
        t = self.timer
        t.reply_text = "".join(self._reply_parts).strip()
        t.rx_overruns, t.tx_underruns = self.rx.overruns, self.tx.underruns
        if t.reply_text:
            self.history.append({"role": "assistant", "content": t.reply_text})
        self.on_turn(t)
        self.timer = None
        self.last_activity_ns = time.monotonic_ns()
        self._new_gen()
        self.state = "LISTENING"
