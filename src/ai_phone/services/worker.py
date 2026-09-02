"""Generic model-worker process loop: one backend, JSON control over a pipe, audio over shared-memory rings.

Cancellation is exact: every request carries a gen_id; a cancelled gen_id is remembered and any
later work for it is dropped, mid-stream checks included.
"""
from __future__ import annotations

import importlib
import os
import traceback

import numpy as np

from ai_phone.services import ipc
from ai_phone.telephony.rings import FrameRing

STT_FRAME_BYTES = 1024  # 512 int16 samples = one VAD chunk


def _load(factory: str):
    mod, _, name = factory.partition(":")
    return getattr(importlib.import_module(mod), name)()


def _drain_cancel(conn, cancelled: set[int], backend, gen: int, pending: list[dict]) -> bool:
    """Apply any cancel messages waiting on the pipe; queue every other message for the main loop.
    Returns True if `gen` is now cancelled."""
    while conn.poll(0):
        m = ipc.recv(conn)
        if m.get("op") == "cancel":
            cancelled.add(m["gen"])
            backend.cancel(m["gen"])
        else:
            pending.append(m)
    return gen in cancelled


def run_worker(kind: str, backend_factory: str, conn, audio_in_name, audio_out_name, warm_args: dict) -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    ain = FrameRing.attach(audio_in_name, frame_bytes=STT_FRAME_BYTES if kind == "stt" else 640) if audio_in_name else None
    aout = FrameRing.attach(audio_out_name) if audio_out_name else None
    cancelled: set[int] = set()
    pending: list[dict] = []  # messages read during a mid-stream cancel check, processed next
    try:
        backend = _load(backend_factory)
        timings = backend.warmup(**warm_args) if warm_args else backend.warmup()
        ipc.send(conn, {"ev": "ready", "timings": timings})
        while True:
            msg = pending.pop(0) if pending else ipc.recv(conn)
            op = msg.get("op")
            if op == "shutdown":
                break
            if op == "cancel":
                cancelled.add(msg["gen"])
                backend.cancel(msg["gen"])
                continue
            gen = msg.get("gen")
            if gen in cancelled:
                continue
            if op == "stt.push":
                frames = b"".join(ain.pop() or b"" for _ in range(msg["nframes"]))
                pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
                partial = backend.push(gen, pcm)
                if partial:
                    ipc.send(conn, {"ev": "partial", "gen": gen, "text": partial})
            elif op == "stt.finalize":
                text, t = backend.finalize(gen)
                ipc.send(conn, {"ev": "final", "gen": gen, "text": text, "timings": t})
            elif op == "llm.generate":
                for tok in backend.stream(gen, msg["messages"], msg["max_tokens"]):
                    if gen in cancelled or (conn.poll(0) and _drain_cancel(conn, cancelled, backend, gen, pending)):
                        break
                    ipc.send(conn, {"ev": "token", "gen": gen, "text": tok})
                ipc.send(conn, {"ev": "done", "gen": gen, "timings": getattr(backend, "last_timings", {})})
            elif op == "tts.synth":
                n = 0
                for frame in backend.stream(gen, msg["seq"], msg["text"]):
                    if gen in cancelled or (conn.poll(0) and _drain_cancel(conn, cancelled, backend, gen, pending)):
                        break
                    aout.push(frame)
                    n += 1
                    if n % 5 == 0:
                        ipc.send(conn, {"ev": "chunk", "gen": gen, "seq": msg["seq"], "nframes": 5})
                if n % 5:
                    ipc.send(conn, {"ev": "chunk", "gen": gen, "seq": msg["seq"], "nframes": n % 5})
                ipc.send(conn, {"ev": "done", "gen": gen, "seq": msg["seq"], "timings": getattr(backend, "last_timings", {})})
    except Exception as e:  # report, then exit non-zero so the supervisor restarts us
        ipc.send(conn, {"ev": "error", "message": f"{e}\n{traceback.format_exc()}"})
        raise
