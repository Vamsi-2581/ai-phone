"""Supervisor: spawns the three model workers, starts PJSIP, and runs one TurnController per call."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import socket
from pathlib import Path

from ai_phone.config import Config
from ai_phone.pipeline.timing import TurnLog
from ai_phone.pipeline.turn import TurnController
from ai_phone.pipeline.vad import Endpointer, EnergyVad, SileroVad
from ai_phone.services.client import WorkerClient
from ai_phone.telephony import events as ev
from ai_phone.telephony.endpoint import Telephony
from ai_phone.telephony.rings import FrameRing

log = logging.getLogger("ai-phone")
FACTORIES = {
    "real": {"stt": "ai_phone.services.backends.mlx_parakeet:ParakeetSTT",
             "llm": {"t9": "ai_phone.services.backends.mlx_lm_backend:make_t9",
                     "t4": "ai_phone.services.backends.mlx_lm_backend:make_t4"},
             "tts": "ai_phone.services.backends.mlx_kokoro:KokoroTTS"},
    "fake": {"stt": "ai_phone.services.backends.fake:FakeSTT",
             "llm": {"t9": "ai_phone.services.backends.fake:FakeLLM", "t4": "ai_phone.services.backends.fake:FakeLLM"},
             "tts": "ai_phone.services.backends.fake:FakeTTS"},
}


class Daemon:
    def __init__(self, cfg: Config, tier: str, log_dir: Path, lan: bool, fake: bool = False):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        if lan:
            cfg.sip.accept_invites_from = "lan"
        self.cfg, self.tier, self.log_dir, self.fake = cfg, tier, Path(log_dir).expanduser(), fake
        self.system_prompt = f"{cfg.conversation.system_prompt} You are running on {socket.gethostname()} ({platform.machine()})."

    async def _cache_phrase(self, tts: WorkerClient, ring: FrameRing, text: str) -> list[bytes]:
        gen = tts.next_gen()
        tts.send({"op": "tts.synth", "gen": gen, "seq": 0, "text": text})
        while True:
            m = await tts.events.get()
            if m["ev"] == "done" and m["gen"] == gen:
                break
        frames = []
        while (f := ring.pop()) is not None:
            frames.append(f)
        return frames

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        rx, tx = FrameRing(None, 100), FrameRing(None, 400)
        stt_in, tts_out = FrameRing(None, 400, frame_bytes=1024), FrameRing(None, 400)
        F = FACTORIES["fake" if self.fake else "real"]
        stt = WorkerClient("stt", F["stt"], loop, stt_in, None, {})
        llm = WorkerClient("llm", F["llm"][self.tier], loop, None, None, {"system_prompt": self.system_prompt})
        tts = WorkerClient("tts", F["tts"], loop, None, tts_out, {})
        warm = await asyncio.gather(stt.start(), llm.start(), tts.start())
        c = self.cfg.conversation
        greeting = await self._cache_phrase(tts, tts_out, c.greeting)
        prompt = await self._cache_phrase(tts, tts_out, "Still there?")
        warning = await self._cache_phrase(tts, tts_out, "This call will end in thirty seconds.")
        tel = Telephony(self.cfg, loop, events, rx, tx)
        tel.start()
        turn_log = TurnLog(self.log_dir / "turns.jsonl")
        stop = asyncio.Event()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)
        log.info("ready mode=%s tier=%s warm=%s", self.cfg.sip.accept_invites_from, self.tier, warm)
        print("ready", flush=True)
        try:
            while not stop.is_set():
                try:
                    e = await asyncio.wait_for(events.get(), 0.2)
                except asyncio.TimeoutError:
                    continue
                if isinstance(e, ev.IncomingCall):
                    log.info("incoming from %s (%s)", e.from_uri, e.source_ip)
                    tel.answer(e.call_id)
                elif isinstance(e, ev.CallRejected):
                    log.info("rejected %s with %s", e.source_ip, e.code)
                elif isinstance(e, ev.MediaActive):
                    await self._run_call(e.call_id, tel, stt, llm, tts, rx, tx, stt_in, tts_out,
                                         greeting, prompt, warning, turn_log, events, stop)
                elif isinstance(e, ev.RegState):
                    log.info("registration active=%s code=%s %s", e.active, e.code, e.reason)
        finally:
            tel.stop()
            await asyncio.gather(stt.stop(), llm.stop(), tts.stop())
            for r in (rx, tx, stt_in, tts_out):
                r.close()
                r.unlink()

    async def _run_call(self, call_id, tel, stt, llm, tts, rx, tx, stt_in, tts_out, greeting, prompt, warning, turn_log, events, stop):
        vad = EnergyVad() if self.fake else SileroVad()
        n = [0]

        def on_turn(t):
            turn_log.write(t.to_record(str(call_id), n[0]))
            n[0] += 1
            log.info("turn %s first_audible=%s bargein=%s", n[0], t.first_audible_ms(), t.bargein)

        tc = TurnController(self.cfg.conversation, tel.port, stt, llm, tts, rx, tx, stt_in, tts_out,
                            Endpointer(vad, self.cfg.conversation.endpoint_silence_ms), greeting, self.system_prompt, on_turn,
                            prompt_frames=prompt, warning_frames=warning, rtp_last_rx_ns=lambda: tel.port.last_rx_ns)
        call_stop = asyncio.Event()
        runner = asyncio.create_task(tc.run(call_stop))
        while not call_stop.is_set() and not stop.is_set():
            if tc.hangup_requested.is_set():
                log.info("hangup: %s", tc.end_reason)
                tel.hangup(call_id)
                break
            try:
                e = await asyncio.wait_for(events.get(), 0.1)
            except asyncio.TimeoutError:
                continue
            if isinstance(e, ev.CallStateChanged) and e.state == "ended":
                break
            if isinstance(e, ev.CallRejected):
                log.info("rejected %s with %s", e.source_ip, e.code)
        call_stop.set()
        await runner
        for r in (rx, tx, stt_in, tts_out):
            r.flush()
