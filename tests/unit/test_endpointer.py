import numpy as np

from ai_phone.pipeline.vad import FRAME, Endpointer, EnergyVad, SpeechEnd, SpeechStart

MS = 1_000_000


def frames(seconds: float, amp: float, hz: float = 300.0):
    n = int(seconds * 16000)
    t = np.arange(n) / 16000.0
    x = (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    pcm = (x * 32767).astype("<i2").tobytes()
    return [pcm[i:i + FRAME * 2] for i in range(0, len(pcm) - FRAME * 2 + 1, FRAME * 2)]


def run(ep, spec):
    """spec: list of (seconds, amplitude). Returns all events with absolute t_ns."""
    t = 0
    out = []
    for sec, amp in spec:
        for f in frames(sec, amp):
            out += ep.feed(f, t)
            t += 20 * MS
    return out


def test_silence_produces_no_events():
    ep = Endpointer(EnergyVad())
    assert run(ep, [(1.0, 0.0)]) == []


def test_speech_then_silence_emits_start_chunks_end_with_t0_at_last_speech():
    ep = Endpointer(EnergyVad(), silence_ms=300)
    ev = run(ep, [(0.5, 0.0), (0.6, 0.5), (0.5, 0.0)])
    kinds = [type(e).__name__ for e in ev]
    assert kinds[0] == "SpeechStart" and kinds[-1] == "SpeechEnd"
    assert "SpeechChunk" in kinds
    end = ev[-1]
    # speech ran 0.5 s..1.1 s; t0 must be within one chunk (32 ms) of 1.1 s, not 1.4 s (end of silence)
    assert abs(end.t0_ns - 1.1 * 1e9) < 40 * MS
    assert 0.55 * 16000 <= len(end.utterance_f32) <= 1.0 * 16000
    assert not ep.in_speech


def test_short_blip_does_not_start_speech():
    ep = Endpointer(EnergyVad(), start_chunks=2)
    ev = run(ep, [(0.2, 0.0), (0.02, 0.5), (0.5, 0.0)])
    assert not any(isinstance(e, SpeechStart) for e in ev)


def test_pause_shorter_than_silence_window_does_not_end():
    ep = Endpointer(EnergyVad(), silence_ms=300)
    ev = run(ep, [(0.3, 0.5), (0.15, 0.0), (0.3, 0.5), (0.5, 0.0)])
    assert sum(isinstance(e, SpeechEnd) for e in ev) == 1


def test_noise_rms_tracks_silence_level():
    ep = Endpointer(EnergyVad())
    run(ep, [(1.0, 0.0)])
    quiet = ep.noise_rms
    run(ep, [(1.0, 0.005)])
    assert ep.noise_rms > quiet


def test_noise_floor_is_not_seeded_by_a_speech_onset():
    """Digital silence, then speech: the floor must stay near zero, not jump to the onset's level."""
    class OnsetBlindVad:
        def __init__(self): self.n = 0
        def prob(self, chunk):
            if not np.any(chunk):
                return 0.0                       # silence is silence
            self.n += 1
            return 0.0 if self.n <= 3 else 1.0    # the VAD misses the first three chunks of speech
    ep = Endpointer(OnsetBlindVad())
    run(ep, [(1.0, 0.0)])                        # exact digital silence
    assert ep.noise_rms == 0.0
    ep2 = Endpointer(OnsetBlindVad())
    run(ep2, [(1.0, 0.0), (0.5, 0.3)])           # then loud speech the VAD flags late
    assert ep2.noise_rms < 0.01, ep2.noise_rms
