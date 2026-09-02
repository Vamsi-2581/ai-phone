import numpy as np

from ai_phone.pipeline.audio_out import Framer


def test_framer_resamples_24k_to_16k_and_emits_640_byte_frames():
    f = Framer(24000)
    t = np.arange(24000) / 24000.0  # 1 s at 24 kHz
    frames = f.feed((0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)) + f.flush()
    assert all(len(x) == 640 for x in frames)
    assert 49 <= len(frames) <= 51  # 1 s at 16 kHz = 50 frames
    s = np.frombuffer(b"".join(frames), dtype="<i2").astype(np.float32) / 32768
    assert 0.3 < np.sqrt(np.mean(s * s)) < 0.4  # sine RMS of 0.5 amplitude ≈ 0.354


def test_framer_carries_remainder_between_feeds():
    f = Framer(24000)
    a = f.feed(np.zeros(100, np.float32))
    b = f.feed(np.zeros(500, np.float32))
    c = f.flush()
    # 600 samples at 24 kHz -> 400 at 16 kHz -> one full frame plus one padded frame; a streaming
    # resampler may hold samples back (filter delay), so only the total is invariant
    assert a == [] and len(b) + len(c) == 2 and all(len(x) == 640 for x in b + c)
