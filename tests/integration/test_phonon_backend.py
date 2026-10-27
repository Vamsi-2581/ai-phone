"""Real, offline Phonon synthesis and generation cancellation on Apple Silicon."""
import os

import numpy as np
import pytest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def phonon():
    from ai_phone import models
    from ai_phone.services.backends.mlx_phonon import PhononTTS

    if models.local_path(models.PHONON) is None:
        pytest.skip("Phonon model is not cached; select Phonon and run ai-phone models pull first")
    tts = PhononTTS()
    assert tts.warmup()["warm_ms"] > 0
    return tts


def test_phonon_streams_usable_phone_frames(phonon):
    frames = list(phonon.stream(20, 0, "Sure, I can help with that."))
    assert len(frames) > 40
    assert all(isinstance(frame, bytes) and len(frame) == 640 for frame in frames)
    samples = np.frombuffer(b"".join(frames), dtype="<i2").astype(np.int32)
    assert 2000 < np.abs(samples).max() < 32767
    assert phonon.last_timings["first_chunk_ms"] < 300


def test_phonon_cancel_cuts_stream_and_next_generation_speaks(phonon):
    stream = phonon.stream(21, 0, "This is a fairly long sentence that should be cut off well before it finishes.")
    first_frames = [next(stream) for _ in range(5)]
    assert all(len(frame) == 640 for frame in first_frames)
    phonon.cancel(21)
    assert list(stream) == []  # neither the next native chunk nor a resampler tail escapes cancellation

    fresh = list(phonon.stream(22, 0, "The new generation is speaking now."))
    assert len(fresh) > 40 and all(len(frame) == 640 for frame in fresh)
    assert np.abs(np.frombuffer(b"".join(fresh), dtype="<i2").astype(np.int32)).max() > 2000
