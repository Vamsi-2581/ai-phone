"""Newline-free JSON messages over a multiprocessing Connection (one message per send_bytes)."""
from __future__ import annotations

import json
from multiprocessing.connection import Connection


def send(conn: Connection, msg: dict) -> None:
    conn.send_bytes(json.dumps(msg, separators=(",", ":")).encode())


def recv(conn: Connection) -> dict:
    return json.loads(conn.recv_bytes().decode())


# Audio frames that cross a generation boundary carry the gen_id in front of the payload, so a
# frame pushed just before a cancel can never be mistaken for the next generation's audio.
import struct as _struct

_TAG = _struct.Struct("<I")
TAG_BYTES = _TAG.size
STT_IN_FRAME = TAG_BYTES + 1024   # one 512-sample int16 VAD chunk
TTS_OUT_FRAME = TAG_BYTES + 640   # one 20 ms 16 kHz frame


def tag_frame(gen: int, payload: bytes) -> bytes:
    return _TAG.pack(gen & 0xFFFFFFFF) + payload


def untag_frame(frame: bytes) -> tuple[int, bytes]:
    return _TAG.unpack_from(frame, 0)[0], frame[TAG_BYTES:]
