"""Generic model-worker process loop: one backend, JSON control over a pipe, audio over shared-memory rings.

Cancellation is exact: every request carries a gen_id; a cancelled gen_id is remembered and any
later work for it is dropped, mid-stream checks included.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import traceback

import numpy as np

from ai_phone.services import ipc
from ai_phone.services.errors import BrainError
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME, tag_frame, untag_frame
from ai_phone.telephony.rings import FrameRing


def _load(factory: str, factory_args: dict | None = None):
    mod, _, name = factory.partition(":")
    return getattr(importlib.import_module(mod), name)(**(factory_args or {}))


def _drain_stt(ring: FrameRing, gen: int) -> list[bytes]:
    """Pop every queued STT frame; keep the payloads tagged with `gen`, discard the rest."""
    out = []
    want = gen & 0xFFFFFFFF
    seen: dict[int, int] = {}
    while (f := ring.pop()) is not None:
        g, payload = untag_frame(f)
        seen[g] = seen.get(g, 0) + 1
        if g == want:
            out.append(payload)
    if os.environ.get("AI_PHONE_DEBUG_RX"):
        print(f"[stt-worker] drain gen={want} kept={len(out)} seen_by_tag={seen} ring_over={ring.overruns}", file=sys.stderr, flush=True)
    return out


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


def _keep_warm(kind: str, backend) -> float:
    """One tiny inference on an idle worker. On a host that is short of memory macOS compresses whatever was used least
    recently; a model that answers a call every few hours is exactly that, and the first turn then waits seconds for
    page-ins (measured 9-10 s to the first transcript). Touching the weights every keep_warm_sec keeps them recent."""
    fn = getattr(backend, "keep_warm", None)
    if fn is None:
        return 0.0
    t = time.monotonic_ns()
    try:
        fn()
    except Exception as e:  # a failed keep-warm is not a failed worker
        print(f"[{kind}-worker] keep_warm failed: {e!r}", file=sys.stderr, flush=True)
        return 0.0
    _trim_memory()
    took = (time.monotonic_ns() - t) / 1e9
    if took > 1.0 or os.environ.get("AI_PHONE_DEBUG_RX"):
        print(f"[{kind}-worker] keep_warm {took * 1e3:.0f} ms", file=sys.stderr, flush=True)
    return took


def _trim_memory(first: bool = False) -> None:
    """Hand MLX's cached scratch buffers back to the OS. After warm-up the STT worker still holds the float32 copy of
    its checkpoint (2.5 GB) in that cache and the LLM worker its warm-up KV scratch; on a 24 GB host that is the
    difference between the models staying resident and the pager compressing them between calls. Idle workers must
    hold the weights and little else, so we cap the cache and clear it after every request."""
    try:
        import mlx.core as mx
    except Exception:  # Linux backends: nothing to trim
        return
    if first:
        mx.set_cache_limit(64 * 1024 * 1024)
    mx.clear_cache()


def run_worker(kind: str, backend_factory: str, conn, audio_in_name, audio_out_name, warm_args: dict, factory_args: dict | None = None,
               keep_warm_sec: float = 0.0) -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    from ai_phone.services.client import is_secret_env
    for name in [n for n in os.environ if is_secret_env(n)]:
        del os.environ[name]  # belt and braces: a worker never holds a key, token or password, whatever the parent exported
    ain = FrameRing.attach(audio_in_name, frame_bytes=STT_IN_FRAME if kind == "stt" else 640) if audio_in_name else None
    aout = FrameRing.attach(audio_out_name, frame_bytes=TTS_OUT_FRAME if kind == "tts" else 640) if audio_out_name else None
    cancelled: set[int] = set()
    pending: list[dict] = []  # messages read during a mid-stream cancel check, processed next
    interval = float(keep_warm_sec)  # current keep-warm cadence (backs off while ticks are slow)
    try:
        backend = _load(backend_factory, factory_args)
        timings = backend.warmup(**warm_args) if warm_args else backend.warmup()
        _trim_memory(first=True)
        ipc.send(conn, {"ev": "ready", "timings": timings})
        while True:
            if pending:
                msg = pending.pop(0)
            else:
                while keep_warm_sec > 0 and not conn.poll(interval):
                    took = _keep_warm(kind, backend)  # idle: touch the model so the pager evicts something else first
                    # a tick that took seconds means the host is thrashing; a request arriving mid-tick waits for it,
                    # so back off (up to 10 min) and return to the configured cadence once ticks are quick again
                    interval = min(interval * 2, 600.0) if took > 1.0 else keep_warm_sec
                msg = ipc.recv(conn)
            op = msg.get("op")
            if op == "shutdown":
                break
            if op == "cancel":
                cancelled.add(msg["gen"])
                backend.cancel(msg["gen"])
                continue
            gen = msg.get("gen")
            if op == "stt.push":
                # drain everything queued (counts cannot desynchronise); the tag decides which frames are ours
                frames = b"".join(_drain_stt(ain, gen))
                if gen in cancelled or not frames:
                    if os.environ.get("AI_PHONE_DEBUG_RX"):
                        print(f"[stt-worker] push gen={gen} skipped (cancelled={gen in cancelled}, frames={len(frames)})", file=sys.stderr, flush=True)
                    continue
                pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
                partial = backend.push(gen, pcm)
                if partial:
                    ipc.send(conn, {"ev": "partial", "gen": gen, "text": partial})
                continue
            if gen in cancelled:
                continue
            if op == "stt.finalize":
                tail = b"".join(_drain_stt(ain, gen))  # frames pushed after the last announcement
                if tail:
                    backend.push(gen, np.frombuffer(tail, dtype="<i2").astype(np.float32) / 32768.0)
                text, t = backend.finalize(gen)
                ipc.send(conn, {"ev": "final", "gen": gen, "text": text, "timings": t})
                _trim_memory()
            elif op == "llm.generate":
                try:
                    for tok in backend.stream(gen, msg["messages"], msg["max_tokens"]):
                        if gen in cancelled or (conn.poll(0) and _drain_cancel(conn, cancelled, backend, gen, pending)):
                            break
                        ipc.send(conn, {"ev": "token", "gen": gen, "text": tok})
                except BrainError as e:  # recoverable: tell the controller, keep serving
                    ipc.send(conn, {"ev": "brain_error", "gen": gen, "kind": e.kind})
                    continue
                ipc.send(conn, {"ev": "done", "gen": gen, "timings": getattr(backend, "last_timings", {})})
                _trim_memory()
            elif op == "tts.synth":
                n = 0
                for frame in backend.stream(gen, msg["seq"], msg["text"]):
                    if gen in cancelled or (conn.poll(0) and _drain_cancel(conn, cancelled, backend, gen, pending)):
                        break
                    aout.push(tag_frame(gen, frame))
                    n += 1
                    if n % 5 == 0:
                        ipc.send(conn, {"ev": "chunk", "gen": gen, "seq": msg["seq"], "nframes": 5})
                if n % 5:
                    ipc.send(conn, {"ev": "chunk", "gen": gen, "seq": msg["seq"], "nframes": n % 5})
                ipc.send(conn, {"ev": "done", "gen": gen, "seq": msg["seq"], "timings": getattr(backend, "last_timings", {})})
                _trim_memory()
    except Exception as e:  # report, then exit non-zero so the supervisor restarts us
        ipc.send(conn, {"ev": "error", "message": f"{e}\n{traceback.format_exc()}"})
        raise
