from ai_phone.pipeline.bargein import BargeInDetector


def feed(d, n, **kw):
    return [d.update(**kw) for _ in range(n)]


def test_needs_min_duration_of_speech_while_playing():
    d = BargeInDetector(min_ms=250, gain=3.0)
    r = feed(d, 7, speech=True, rms=0.2, noise_rms=0.01, playing=True)  # 7 × 32 = 224 ms
    assert not any(r)
    assert d.update(speech=True, rms=0.2, noise_rms=0.01, playing=True)  # 256 ms → trigger
    assert not d.update(speech=True, rms=0.2, noise_rms=0.01, playing=True)  # only once


def test_quiet_speech_under_raised_floor_does_not_trigger():
    d = BargeInDetector(gain=3.0)
    assert not any(feed(d, 20, speech=True, rms=0.02, noise_rms=0.01, playing=True))


def test_one_quiet_chunk_is_tolerated_but_two_reset_the_count():
    d = BargeInDetector()
    feed(d, 5, speech=True, rms=0.2, noise_rms=0.01, playing=True)
    d.update(speech=False, rms=0.0, noise_rms=0.01, playing=True)          # one gap: count kept (5)
    assert any(feed(d, 3, speech=True, rms=0.2, noise_rms=0.01, playing=True))  # 8 → trigger
    d = BargeInDetector()
    feed(d, 5, speech=True, rms=0.2, noise_rms=0.01, playing=True)
    feed(d, 2, speech=False, rms=0.0, noise_rms=0.01, playing=True)       # two gaps: reset
    assert not any(feed(d, 5, speech=True, rms=0.2, noise_rms=0.01, playing=True))


def test_rms_gate_off_when_not_playing():
    d = BargeInDetector()
    assert any(feed(d, 9, speech=True, rms=0.02, noise_rms=0.01, playing=False))
