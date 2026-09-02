import multiprocessing as mp

import pytest

from ai_phone.telephony.rings import FRAME_BYTES, SILENCE, FrameRing


def test_push_pop_fifo():
    r = FrameRing(None, frames=4)
    try:
        r.push(b"\x01" * FRAME_BYTES)
        r.push(b"\x02" * FRAME_BYTES)
        assert r.available() == 2
        assert r.pop() == b"\x01" * FRAME_BYTES
        assert r.pop() == b"\x02" * FRAME_BYTES
        assert r.pop() is None
    finally:
        r.close()
        r.unlink()


def test_overflow_overwrites_oldest_and_counts():
    r = FrameRing(None, frames=2)
    try:
        for i in range(1, 4):
            r.push(bytes([i]) * FRAME_BYTES)
        assert r.overruns == 1
        assert r.pop() == b"\x02" * FRAME_BYTES
        assert r.pop() == b"\x03" * FRAME_BYTES
    finally:
        r.close()
        r.unlink()


def test_underflow_returns_silence_and_counts():
    r = FrameRing(None, frames=2)
    try:
        assert r.pop_or_silence() == SILENCE
        assert r.underruns == 1
    finally:
        r.close()
        r.unlink()


def test_flush_discards_everything():
    r = FrameRing(None, frames=8)
    try:
        for _ in range(5):
            r.push(b"\x07" * FRAME_BYTES)
        r.flush()
        assert r.available() == 0 and r.pop() is None
    finally:
        r.close()
        r.unlink()


def test_wrong_frame_size_rejected():
    r = FrameRing(None, frames=2)
    try:
        with pytest.raises(ValueError):
            r.push(b"\x00" * 10)
    finally:
        r.close()
        r.unlink()


def _child(name, n):
    r = FrameRing.attach(name)
    for i in range(n):
        r.push(bytes([i % 256]) * FRAME_BYTES)
    r.close()


def test_cross_process_push():
    r = FrameRing(None, frames=16)
    try:
        p = mp.get_context("spawn").Process(target=_child, args=(r.name, 5))
        p.start()
        p.join(10)
        assert p.exitcode == 0
        assert r.available() == 5
        assert r.pop() == b"\x00" * FRAME_BYTES
    finally:
        r.close()
        r.unlink()
