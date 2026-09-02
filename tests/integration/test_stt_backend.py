import os

import numpy as np
import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
pytestmark = pytest.mark.integration


def _speech_f32():
    """Synthesise a known sentence with Kokoro so the test needs no recorded fixture."""
    import mlx.core as mx
    import soxr
    from mlx_audio.tts.utils import load_model

    tts = load_model("mlx-community/Kokoro-82M-bf16")
    audio = np.concatenate([np.array(mx.array(r.audio).astype(mx.float32)).reshape(-1)
                            for r in tts.generate(text="Please book a table for two on Friday.", voice="af_heart", lang_code="a")])
    return soxr.resample(audio, 24000, 16000)


def test_parakeet_push_finalize_transcribes():
    from ai_phone.services.backends.mlx_parakeet import ParakeetSTT

    stt = ParakeetSTT()
    t = stt.warmup()
    assert t["warm_ms"] > 0
    pcm = _speech_f32()
    step = 5120  # 320 ms
    for i in range(0, len(pcm), step):
        stt.push(7, pcm[i:i + step])
    text, timings = stt.finalize(7)
    assert "table" in text.lower() and "friday" in text.lower(), text
    assert timings["stt_ms"] < 400


def test_cancel_clears_buffer():
    from ai_phone.services.backends.mlx_parakeet import ParakeetSTT

    stt = ParakeetSTT()
    stt.warmup()
    stt.push(1, np.zeros(5120, np.float32))
    stt.cancel(1)
    text, _ = stt.finalize(1)
    assert text == ""
