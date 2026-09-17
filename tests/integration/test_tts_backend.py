import os

import numpy as np
import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
pytestmark = pytest.mark.integration


def test_kokoro_yields_16k_frames_fast():
    from ai_phone.services.backends.mlx_kokoro import KokoroTTS

    tts = KokoroTTS()
    assert tts.warmup()["warm_ms"] > 0
    frames = list(tts.stream(9, 0, "Sure, I can help with that."))
    assert len(frames) > 40 and all(len(f) == 640 for f in frames)  # > 0.8 s of audio
    s = np.frombuffer(b"".join(frames), dtype="<i2")
    assert np.abs(s).max() > 2000
    assert tts.last_timings["first_chunk_ms"] < 300


def test_cancel_stops_early():
    from ai_phone.services.backends.mlx_kokoro import KokoroTTS

    tts = KokoroTTS()
    tts.warmup()
    n = 0
    for _ in tts.stream(10, 0, "This is a fairly long sentence that should be cut off well before it finishes."):
        n += 1
        if n == 5:
            tts.cancel(10)
    assert n < 40


def test_british_voice_speaks():
    from ai_phone.services.backends.mlx_kokoro import KokoroTTS

    tts = KokoroTTS(voice="bf_emma", speed=1.1)
    tts.warmup()
    assert tts._voice_ref.endswith("/voices/bf_emma.safetensors")  # a file path: mlx-audio must not look it up online
    frames = list(tts.stream(11, 0, "Hello, this is a British voice."))
    assert len(frames) > 20 and all(len(f) == 640 for f in frames)
    assert np.abs(np.frombuffer(b"".join(frames), dtype="<i2")).max() > 2000
